"""
tts.py - Text-To-Speech: turns Claude's answer into a voice file.

1st choice: Microsoft's natural "neural" voices through the free edge-tts
            library (needs internet, sounds human).
2nd choice: the voice built into Windows (works offline, sounds robotic).
If both fail, the answer is still shown in the window and saved in the log.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("glassesclaude")

# Windows-only flag that stops a black console window from flashing.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# PowerShell snippet for the built-in Windows voice. The text and the output
# path come in through environment variables, so no quoting problems.
_WINDOWS_TTS = (
    "Add-Type -AssemblyName System.Speech; "
    "$t = Get-Content -LiteralPath $env:GC_TTS_IN -Raw -Encoding UTF8; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Rate = 1; "
    "$s.SetOutputToWaveFile($env:GC_TTS_OUT); "
    "$s.Speak($t); "
    "$s.Dispose()"
)


class TextToSpeech:
    def __init__(self, settings: dict, out_dir: Path):
        cfg = settings["text_to_voice"]
        self.voice = cfg["voice"] or "en-US-EmmaMultilingualNeural"
        self.speed = cfg["speed"] or "+0%"
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._online_down_until = 0.0   # after a failure, skip online voice for a minute

    def to_file(self, text: str, name: str) -> Path | None:
        """Make a voice file for `text`. Returns its path, or None."""
        text = (text or "").strip()
        if not text:
            return None
        path = self._online(text, self.out_dir / f"{name}.mp3")
        if path is None:
            path = self._windows(text, self.out_dir / f"{name}.wav")
        return path

    # -- natural online voice ------------------------------------------------
    def _online(self, text: str, path: Path) -> Path | None:
        if time.monotonic() < self._online_down_until:
            return None
        try:
            import edge_tts

            async def run():
                comm = edge_tts.Communicate(
                    text, self.voice, rate=self.speed, connect_timeout=8, receive_timeout=30
                )
                await comm.save(str(path))

            asyncio.run(asyncio.wait_for(run(), timeout=45))
            if path.exists() and path.stat().st_size > 0:
                return path
        except Exception as exc:  # no internet, voice name typo, service hiccup ...
            log.warning("Online voice not available (%s). Using the Windows voice.", exc.__class__.__name__)
            self._online_down_until = time.monotonic() + 60
        path.unlink(missing_ok=True)
        return None

    # -- built-in Windows voice ------------------------------------------------
    def _windows(self, text: str, path: Path) -> Path | None:
        if os.name != "nt":
            return None
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
                fh.write(text)
                tmp = fh.name
            env = dict(os.environ, GC_TTS_IN=tmp, GC_TTS_OUT=str(path))
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", _WINDOWS_TTS],
                env=env, capture_output=True, timeout=90, creationflags=_NO_WINDOW,
            )
            if path.exists() and path.stat().st_size > 0:
                return path
        except Exception as exc:
            log.warning("Windows voice failed too (%s). The answer is only in the window/log.", exc)
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
        return None
