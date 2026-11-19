"""
inbox.py - voice memos: record now (anywhere), run later.

Record a voice memo with your glasses as usual. Get the file onto this PC
(Hi Rokid app -> share to OneDrive/Google Drive, or the ADB copy tool) into
the memo folder. GlassesClaude notices new files and:

  * memo starts with "Claude, ..."  -> it's a COMMAND: Claude Code runs it
  * any other memo                  -> it's a NOTE: just written down

Every result is saved as a small text file in the "done" folder, next to the
original recording. If the memo folder is inside OneDrive/Google Drive, the
results show up in your cloud too.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import ROOT, resolve_folder

log = logging.getLogger("glassesclaude")

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".webm", ".mp4",
                    ".amr", ".3gp", ".flac", ".wma", ".mka"}


def _unique(path: Path) -> Path:
    """'memo.m4a' -> 'memo (2).m4a' if the name is already taken."""
    if not path.exists():
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} {time.time_ns()}{path.suffix}")


class MemoInbox:
    def __init__(self, settings: dict, brain):
        self.brain = brain
        self.folder = resolve_folder(settings["voice_memos"]["folder"], ROOT / "inbox")
        self.processing = self.folder / "processing"
        self.done = self.folder / "done"
        self._seen: dict[Path, tuple[int, float]] = {}
        brain.on_memo_done = self.finished

    def start(self) -> bool:
        try:
            self.processing.mkdir(parents=True, exist_ok=True)
            self.done.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Voice memo folder can't be used (%s): %s", exc, self.folder)
            return False
        # Anything left from last time (PC turned off mid-way) is processed again.
        for leftover in sorted(self.processing.iterdir()):
            if leftover.is_file() and leftover.suffix.lower() in AUDIO_EXTENSIONS:
                self.brain.submit_audio(leftover, "memo", memo_name=leftover.name)
        threading.Thread(target=self._loop, name="memos", daemon=True).start()
        return True

    def _loop(self) -> None:
        while True:
            try:
                self._scan()
            except Exception:
                log.exception("Voice memo folder check failed")
            time.sleep(4)

    def _scan(self) -> None:
        now = time.time()
        for f in sorted(self.folder.iterdir()):
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            previous = self._seen.get(f)
            self._seen[f] = sig
            # Wait until the file stops changing (a sync app may still be copying it).
            if previous != sig or now - st.st_mtime < 3 or st.st_size == 0:
                continue
            target = _unique(self.processing / f.name)
            try:
                shutil.move(str(f), str(target))   # "claim" it so it's handled once
            except OSError:
                continue                            # still locked; try next round
            self._seen.pop(f, None)
            log.info("[memo] new recording: %s", f.name)
            self.brain.submit_audio(target, "memo", memo_name=f.name)

    def finished(self, job) -> None:
        """Called by the brain when a memo is done: write the result file."""
        stamp = datetime.now().strftime("%Y-%m-%d %H%M")
        stem = Path(job.memo_name or "memo").stem
        result_file = _unique(self.done / f"{stamp} {stem}.md")
        if job.kind == "note":
            body = (f"# Note: {job.memo_name}\n\nSaved {stamp}\n\n## What you said\n\n"
                    f"{job.text or job.reply}\n")
        else:
            body = (f"# Command: {job.memo_name}\n\nRun {stamp} - result: {job.status}\n\n"
                    f"## What you said\n\n{job.text}\n\n## Claude's answer\n\n{job.reply}\n")
        result_file.write_text(body, encoding="utf-8")
        if job.audio_path and Path(job.audio_path).exists():
            try:
                shutil.move(str(job.audio_path), str(_unique(self.done / Path(job.audio_path).name)))
            except OSError:
                pass
        log.info("[memo] saved %s", result_file.name)
