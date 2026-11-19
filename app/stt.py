"""
stt.py - Speech-To-Text: turns your recorded voice into text.

It uses "faster-whisper", a free version of OpenAI's Whisper model that
runs completely ON YOUR PC (your voice is not uploaded anywhere for this
step). The model file (~0.5 GB for "small") is downloaded once by
"1 - Install" and kept in GlassesClaude/data/models.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from .config import DATA

log = logging.getLogger("glassesclaude")

# Whisper sometimes "hears" these phrases in pure silence. If that is ALL it
# heard, we treat the recording as empty instead of sending it to Claude.
_SILENCE_GHOSTS = {
    "", "you", "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "bye", "okay", "ok", "so", "hmm", "uh", "um",
}


class SpeechToText:
    def __init__(self, settings: dict):
        cfg = settings["voice_to_text"]
        self.model_name = cfg["model"] or "small"
        self.language = (cfg["language"] or "").strip() or None   # None = auto-detect
        hint = (cfg["hint_words"] or "").strip()
        # Whisper treats this like "text that came before", which nudges it to
        # spell tech words correctly (React, npm, GitHub ...).
        self.prompt = f"Voice commands for Claude Code about software projects: {hint}." if hint else None
        self._model = None
        self._load_lock = threading.Lock()   # load the model only once

    def _load(self):
        with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel  # imported late: it is big

                log.info("Loading the speech model '%s' (first time can take a minute)...", self.model_name)
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",          # works on every PC, no graphics card needed
                    compute_type="int8",   # small + fast on normal CPUs
                    num_workers=2,         # a live command isn't stuck behind a long voice memo
                    download_root=str(DATA / "models"),
                )
        return self._model

    def download(self) -> None:
        """Used by the installer: fetch the model now so the first command is fast."""
        self._load()

    def transcribe(self, audio_path: Path) -> str:
        """Return the words in the audio file ('' if nothing was said)."""
        from .audio_io import decode_file

        # We decode the file ourselves (16 kHz mono, what Whisper expects). This works
        # for wav, mp3, m4a, webm, ogg ... and doesn't depend on faster-whisper's own
        # decoder, which breaks with some versions of the PyAV library.
        audio = decode_file(Path(audio_path), 16000)
        if len(audio) < 1600:          # shorter than 0.1 s: nothing to hear
            return ""
        model = self._load()
        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
            vad_filter=True,                  # skip silent parts
            initial_prompt=self.prompt,
            condition_on_previous_text=False,
        )
        text = " ".join(seg.text.strip() for seg in segments)   # the words appear here
        text = re.sub(r"\s+", " ", text).strip()
        if text.lower().strip(" .!?,") in _SILENCE_GHOSTS:
            return ""
        return text
