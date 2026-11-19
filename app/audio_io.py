"""
audio_io.py - microphone + speaker on this PC (used by desk mode).

When your glasses are paired to the PC by Bluetooth, Windows shows them as a
microphone ("Headset ...") and a speaker. This file:
  * picks those devices by name (settings: microphone / speaker),
  * records until you stop talking (SilenceDetector),
  * plays answers and little beeps through the glasses.
"""
from __future__ import annotations

import logging
import queue
import statistics
import threading
import wave
from pathlib import Path

log = logging.getLogger("glassesclaude")

# Windows offers the same device through several "host APIs". MME accepts any
# sample rate, so it is the most forgiving choice for Bluetooth headsets.
_HOSTAPI_PREFERENCE = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}


# ----------------------------------------------------------------- devices
def list_devices() -> list[dict]:
    """Every audio device Windows reports (used for the startup printout)."""
    import sounddevice as sd

    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        out.append({
            "index": i, "name": d["name"], "api": apis[d["hostapi"]]["name"],
            "inputs": d["max_input_channels"], "outputs": d["max_output_channels"],
            "rate": int(d["default_samplerate"]),
        })
    return out


def pick_device(hint: str, kind: str) -> tuple[int | None, str, int, bool]:
    """Find a device whose name contains `hint` (for example "rokid").

    Returns (index, name, sample_rate, matched_hint). If nothing matches we use
    the Windows default device and matched_hint is False.
    """
    import sounddevice as sd

    devices = list_devices()
    has = (lambda d: d["inputs"] > 0) if kind == "input" else (lambda d: d["outputs"] > 0)
    usable = [d for d in devices if has(d)]
    hint = (hint or "").strip().lower()
    if hint:
        matches = [d for d in usable if hint in d["name"].lower()]
        if matches:
            def score(d):
                name = d["name"].lower()
                # The "Headset"/"Hands-Free" version carries the glasses' microphone.
                # Using it for BOTH mic and speaker avoids Bluetooth profile switching.
                bonus = -10 if ("headset" in name or "hands-free" in name or "hands free" in name) else 0
                return bonus + _HOSTAPI_PREFERENCE.get(d["api"], 3)
            best = sorted(matches, key=score)[0]
            return best["index"], best["name"], best["rate"], True
    default = sd.default.device[0 if kind == "input" else 1]
    if default is None or default < 0:
        if not usable:
            return None, "(none)", 16000, False
        default = usable[0]["index"]
    d = devices[default]
    return d["index"], d["name"], d["rate"], False


# ------------------------------------------------------- silence detection
class SilenceDetector:
    """Decides when you have finished talking.

    Feed it the loudness (RMS) of each small chunk of audio. It returns:
      None        -> keep recording
      "silence"   -> you talked, then paused long enough: stop
      "no_speech" -> nothing was said for a while: give up
      "max"       -> the recording got too long: stop
    """

    def __init__(self, silence_seconds: float = 1.6, max_seconds: float = 60.0,
                 no_speech_seconds: float = 6.0, calibrate_seconds: float = 0.3,
                 min_threshold: float = 0.008, max_threshold: float = 0.06):
        self.silence_seconds = silence_seconds
        self.max_seconds = max_seconds
        self.no_speech_seconds = no_speech_seconds
        self.calibrate_seconds = calibrate_seconds
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.threshold = min_threshold
        self._noise: list[float] = []
        self._t = 0.0
        self._loud_for = 0.0
        self._quiet_for = 0.0
        self.speech_started = False
        self.max_rms = 0.0          # loudest moment (helps decide if a quiet mic heard something)

    def feed(self, rms: float, seconds: float) -> str | None:
        self._t += seconds
        if self._t > self.calibrate_seconds:
            self.max_rms = max(self.max_rms, rms)
        if self._t <= self.calibrate_seconds:
            # Listen to the room for a moment to learn how loud "quiet" is.
            self._noise.append(rms)
            floor = statistics.median(self._noise)
            self.threshold = min(max(self.min_threshold, floor * 3.0), self.max_threshold)
            return None
        if not self.speech_started:
            if rms > self.threshold:
                self._loud_for += seconds
                if self._loud_for >= 0.12:          # a real word, not a click
                    self.speech_started = True
                    self._quiet_for = 0.0
            else:
                self._loud_for = max(0.0, self._loud_for - seconds / 2)
            if not self.speech_started and self._t >= self.no_speech_seconds:
                return "no_speech"
        else:
            if rms > self.threshold * 0.8:
                self._quiet_for = 0.0
            else:
                self._quiet_for += seconds
                if self._quiet_for >= self.silence_seconds:
                    return "silence"
        if self._t >= self.max_seconds:
            return "max"
        return None


# --------------------------------------------------------------- recording
def record_until_silence(device: int | None, samplerate: int, detector: SilenceDetector,
                         stop: threading.Event):
    """Record from `device` until the detector (or a key press) says stop."""
    import numpy as np
    import sounddevice as sd

    chunks: "queue.Queue" = queue.Queue()

    def callback(indata, frames, time_info, status):  # runs on the audio thread
        chunks.put(indata[:, 0].copy())

    blocks = []
    reason = None
    block = max(256, int(samplerate * 0.03))       # 30 ms pieces
    with sd.InputStream(device=device, channels=1, samplerate=samplerate, dtype="float32",
                        blocksize=block, callback=callback):
        while reason is None:
            if stop.is_set():
                reason = "stopped"
                break
            try:
                piece = chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            blocks.append(piece)
            rms = float(np.sqrt(np.mean(np.square(piece)))) if len(piece) else 0.0
            reason = detector.feed(rms, len(piece) / samplerate)
    data = np.concatenate(blocks) if blocks else np.zeros(0, dtype="float32")
    return data, reason


def save_wav(path: Path, data, samplerate: int) -> Path:
    """Save float audio (-1..1) as a normal 16-bit WAV file."""
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(samplerate))
        w.writeframes(pcm.tobytes())
    return path


# ---------------------------------------------------------------- playback
def decode_file(path: Path, samplerate: int):
    """Read any audio file (mp3, wav, m4a ...) as mono float samples."""
    import av
    import numpy as np

    pieces = []
    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.AudioResampler(format="flt", layout="mono", rate=int(samplerate))
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                pieces.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):        # flush the last bit
            pieces.append(out.to_ndarray().reshape(-1))
    return np.concatenate(pieces).astype("float32") if pieces else np.zeros(0, dtype="float32")


def tone(kind: str, samplerate: int):
    """Short beeps: "start" (listening), "stop" (got it), "cancel" (nothing heard)."""
    import numpy as np

    freq, ms = {"start": (880, 110), "stop": (660, 110), "cancel": (330, 260)}.get(kind, (500, 120))
    n = int(samplerate * ms / 1000)
    t = np.arange(n) / samplerate
    wavef = 0.25 * np.sin(2 * np.pi * freq * t)
    fade = min(n // 4, int(samplerate * 0.01))
    if fade:
        ramp = np.linspace(0, 1, fade)
        wavef[:fade] *= ramp
        wavef[-fade:] *= ramp[::-1]
    return wavef.astype("float32")


class Player:
    """Plays sounds one after another on the chosen speaker (your glasses)."""

    def __init__(self, device: int | None, samplerate: int):
        self.device = device
        self.samplerate = int(samplerate) or 48000
        self.playing = threading.Event()
        self._q: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._loop, name="player", daemon=True).start()

    def play(self, path: Path) -> None:
        self._q.put(("file", Path(path), None))

    def beep(self, kind: str, wait: bool = False) -> None:
        done = threading.Event() if wait else None
        self._q.put(("beep", kind, done))
        if done:
            done.wait(3)

    def stop(self) -> None:
        """Stop what is playing now and forget anything waiting."""
        import sounddevice as sd

        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item[2] is not None:
                item[2].set()
        sd.stop()

    def _loop(self) -> None:
        import sounddevice as sd

        while True:
            what, arg, done = self._q.get()
            try:
                data = tone(arg, self.samplerate) if what == "beep" else decode_file(arg, self.samplerate)
                if len(data):
                    self.playing.set()
                    sd.play(data, self.samplerate, device=self.device)
                    sd.wait()
            except Exception as exc:
                log.warning("Could not play %s (%s)", arg, exc)
            finally:
                self.playing.clear()
                if done is not None:
                    done.set()
