"""
desk_mode.py - talk through glasses that are paired to THIS PC by Bluetooth.

  1. Press the hotkey (default Ctrl + Alt + Space) - or tap your glasses if
     they send "play/pause" to the PC.
  2. You hear a short beep: talk.
  3. Pause for a moment: a second beep means "got it".
  4. You hear "Got it: ..." and, when Claude is done, the answer.

Press the hotkey again while recording to stop early. Press it while an
answer is playing to interrupt the answer and talk again.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .config import DATA

log = logging.getLogger("glassesclaude")

_SYNONYMS = {
    "control": "ctrl", "ctl": "ctrl", "option": "alt", "win": "cmd", "windows": "cmd", "super": "cmd",
    "spacebar": "space", "escape": "esc", "return": "enter", "pgup": "page_up", "pgdn": "page_down",
    "del": "delete", "ins": "insert",
}


def to_pynput_hotkey(text: str) -> str:
    """'Ctrl + Alt + Space' -> '<ctrl>+<alt>+<space>'  (the format pynput wants)."""
    parts = [p.strip().lower() for p in (text or "").replace(" ", "").split("+") if p.strip()]
    out = []
    for p in parts:
        p = p.strip("<>")
        p = _SYNONYMS.get(p, p)
        out.append(p if len(p) == 1 else f"<{p}>")
    return "+".join(out)


def pretty_hotkey(text: str) -> str:
    """'ctrl+alt+space' -> 'Ctrl + Alt + Space' (for messages)."""
    return " + ".join(p.strip("<>").replace("_", " ").title() if len(p.strip("<>")) > 1 else p.upper()
                      for p in to_pynput_hotkey(text).split("+"))


class DeskMode:
    def __init__(self, settings: dict, brain):
        self.cfg = settings["desk_mode"]
        self.brain = brain
        self.player = None
        self.mic = self.mic_name = self.speaker_name = None
        self.mic_rate = 16000
        self.hotkey_text = pretty_hotkey(self.cfg["hotkey"])
        self._recording = threading.Event()
        self._stop_recording = threading.Event()
        self._last_trigger = 0.0
        self._listener = None
        self.problem = ""

    def start(self) -> bool:
        """Open mic/speaker and listen for the hotkey. False if not possible."""
        try:
            import sounddevice  # noqa: F401  (checks the audio library works)
            from pynput import keyboard
        except Exception as exc:
            self.problem = f"audio/keyboard libraries not available ({exc.__class__.__name__}: {exc})"
            return False
        from .audio_io import Player, pick_device

        try:
            self.mic, self.mic_name, self.mic_rate, mic_matched = pick_device(self.cfg["microphone"], "input")
            spk, self.speaker_name, spk_rate, spk_matched = pick_device(self.cfg["speaker"], "output")
        except Exception as exc:
            self.problem = f"could not read audio devices ({exc})"
            return False
        if self.mic is None:
            self.problem = "no microphone found"
            return False
        self.mic_matched, self.speaker_matched = mic_matched, spk_matched
        self.player = Player(spk, spk_rate)
        self.brain.local_player = self.player

        spec = to_pynput_hotkey(self.cfg["hotkey"])
        try:
            hotkey = keyboard.HotKey(keyboard.HotKey.parse(spec), self._trigger)
        except Exception:
            log.warning("Hotkey '%s' isn't valid, using Ctrl + Alt + Space.", self.cfg["hotkey"])
            self.hotkey_text = "Ctrl + Alt + Space"
            hotkey = keyboard.HotKey(keyboard.HotKey.parse("<ctrl>+<alt>+<space>"), self._trigger)
        use_media = bool(self.cfg["use_media_key"])
        media_keys = {keyboard.Key.media_play_pause}

        def on_press(key):
            if use_media and key in media_keys:
                self._trigger()
                return
            if self._listener is not None:
                hotkey.press(self._listener.canonical(key))

        def on_release(key):
            if self._listener is not None:
                hotkey.release(self._listener.canonical(key))

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()
        return True

    # ------------------------------------------------------------------
    def _trigger(self) -> None:
        now = time.monotonic()
        if now - self._last_trigger < 0.4:          # ignore key bounce / double events
            return
        self._last_trigger = now
        if self._recording.is_set():                 # 2nd press = stop recording now
            self._stop_recording.set()
            return
        if self.player is not None and self.player.playing.is_set():
            self.player.stop()                       # interrupt the answer, then listen
        threading.Thread(target=self._record_once, name="record", daemon=True).start()

    def _record_once(self) -> None:
        from .audio_io import SilenceDetector, record_until_silence, save_wav

        self._recording.set()
        self._stop_recording.clear()
        try:
            self.player.beep("start", wait=True)     # finish the beep before listening
            time.sleep(0.08)
            detector = SilenceDetector(silence_seconds=float(self.cfg["silence_seconds"]),
                                       max_seconds=float(self.cfg["max_record_seconds"]))
            log.info("[listening] talk now ...")
            data, reason = record_until_silence(self.mic, self.mic_rate, detector, self._stop_recording)
            log.debug("recording ended (%s): loudest %.4f, threshold %.4f", reason, detector.max_rms, detector.threshold)
            # A very quiet microphone may never cross the threshold. If it picked up *something*,
            # let Whisper decide instead of throwing the recording away.
            heard_something = detector.speech_started or detector.max_rms > 0.004
            if len(data) < self.mic_rate * 0.3 or (reason == "no_speech" and not heard_something):
                log.info("[listening] didn't hear anything")
                self.player.beep("cancel")
                return
            self.player.beep("stop")
            path = DATA / "recordings" / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".wav")
            save_wav(path, data, self.mic_rate)
            self.brain.submit_audio(path, "desk")
        except Exception as exc:
            log.warning("Recording failed: %s", exc)
            if self.player is not None:
                self.player.beep("cancel")
        finally:
            self._recording.clear()
