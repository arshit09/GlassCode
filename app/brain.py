"""
brain.py - the middle of everything.

Every way of talking (desk hotkey, talk page, voice memos) hands its audio
or text to the Brain. The Brain does the same 3 steps every time:

    1. HEAR   voice -> text           (stt.py, on your PC)
    2. DO     text  -> Claude Code    (claude_bridge.py, in your project folder)
    3. SPEAK  answer -> voice file    (tts.py) -> played in your glasses

Two worker threads keep it responsive:
  * the "hear" thread turns audio into text right away, so a spoken "stop"
    can cancel a long task immediately;
  * the "do" thread runs Claude Code one command at a time, in order.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .claude_bridge import ClaudeResult
from .config import LOGS, State, default_project, resolve_folder
from .speakable import make_speakable, short_echo

log = logging.getLogger("glassesclaude")

# Things you can say that GlassesClaude handles itself (no Claude needed).
# The WHOLE sentence must match, so "stop the dev server" still goes to Claude.
CANCEL = {"cancel", "stop", "stop it", "stop that", "cancel that", "cancel it", "never mind",
          "nevermind", "abort", "stop working", "please stop"}
NEW_CHAT = {"new chat", "new conversation", "start over", "start a new chat", "start a new conversation",
            "reset", "reset conversation", "reset the conversation", "clear conversation",
            "clear the conversation", "fresh start", "forget everything"}
REPEAT = {"repeat", "repeat that", "say that again", "say it again", "again", "what did you say",
          "come again", "repeat the last answer", "repeat please"}
STATUS = {"status", "what are you doing", "are you working", "are you done", "progress",
          "whats happening", "what is happening", "are you still working", "how is it going"}
WHICH_PROJECT = {"which project", "what project", "which project am i in", "what project am i in",
                 "current project", "where are you working"}
_SWITCH = re.compile(r"^(?:switch|change|go|move)(?: over)? to(?: the)?(?: project)? (?P<name>.+?)(?: project)?$")
_WAKE = re.compile(r"^(?:(?:hey|ok|okay|hi)\s+)?claude\s+")
_DEFAULT_NAMES = {"workspace", "default", "the workspace", "default project", "home"}


def _normalize(text: str) -> str:
    """'Hey Claude, STOP!' -> 'stop' (lowercase, no punctuation, no wake word)."""
    t = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).replace("'", "")
    t = re.sub(r"\s+", " ", t).strip()
    return _WAKE.sub("", t + " ").strip()


def _squash(name: str) -> str:
    """'My Portfolio' / 'my-portfolio' -> 'myportfolio' (for matching)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


@dataclass
class Job:
    id: str
    source: str                      # "desk", "web", "memo" or "away" (phone, from anywhere)
    kind: str                        # "audio", "text" or "note"
    created: float = field(default_factory=time.time)
    audio_path: Path | None = None
    memo_name: str = ""
    external_id: str = ""            # the relay's id for requests from the phone (away mode)
    text: str = ""                   # what you said (transcript) or typed
    command: str = ""                # what is sent to Claude
    status: str = "queued"           # listening -> queued -> working -> speaking -> done/error/cancelled/note
    ok: bool = True
    reply: str = ""                  # Claude's full answer
    spoken: str = ""                 # the short version that is read aloud
    ack_audio: Path | None = None    # "Got it: ..." voice file
    reply_audio: Path | None = None  # the answer voice file
    finished: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    reply_ready: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        """The safe, JSON-friendly view used by the talk page."""
        return {
            "id": self.id, "source": self.source, "kind": self.kind, "status": self.status,
            "ok": self.ok, "text": self.text, "reply": self.reply, "spoken": self.spoken,
            "has_ack": bool(self.ack_audio), "has_reply_audio": bool(self.reply_audio),
            "created": self.created, "finished": self.finished,
        }


class History:
    """Appends every exchange to logs/history.md (open it with any text editor)."""

    def __init__(self, path: Path | None = None):
        self.path = path or (LOGS / "history.md")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.path.write_text("# GlassesClaude history\n\nEverything you said and every answer.\n\n",
                                 encoding="utf-8")

    def add(self, job: Job, project: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        where = {"desk": "glasses at desk", "web": "talk page", "memo": f"voice memo {job.memo_name}",
                 "away": "phone (away)"}.get(job.source, job.source)
        lines = [f"### {stamp} - {where} - project: {project}"]
        if job.kind == "note":
            lines.append(f"Note: {job.text}")
        else:
            lines.append(f"You: {job.text}")
            lines.append(f"Claude ({job.status}): {job.reply}")
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")


class Brain:
    def __init__(self, settings: dict, state: State, stt, claude, tts, history: History | None = None,
                 upload_dir: Path | None = None):
        self.settings = settings
        self.state = state
        self.stt = stt
        self.claude = claude
        self.tts = tts
        self.history = history or History()
        self.upload_dir = upload_dir
        self.local_player = None          # desk mode plugs in its speaker here
        self.on_memo_done = None          # the voice-memo inbox plugs in here
        self.listeners: list = []         # away mode listens here: fn(job, event)
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._jobs_lock = threading.Lock()
        self._hear_q: "queue.Queue[Job]" = queue.Queue()
        self._memo_q: "queue.Queue[Job]" = queue.Queue()     # long memos get their own line
        self._do_q: "queue.Queue[Job]" = queue.Queue()
        self._current: Job | None = None
        self._cancel = threading.Event()
        self._reset_after_current = False
        self._last_answer: Job | None = None
        self._warned_missing: set[str] = set()
        threading.Thread(target=self._hear_loop, args=(self._hear_q,), name="hear", daemon=True).start()
        threading.Thread(target=self._hear_loop, args=(self._memo_q,), name="hear-memos", daemon=True).start()
        threading.Thread(target=self._do_loop, name="do", daemon=True).start()

    # ================================================================ public
    def submit_audio(self, path: Path, source: str, memo_name: str = "", external_id: str = "") -> Job:
        job = self._new_job(source, "audio", audio_path=Path(path), memo_name=memo_name, external_id=external_id)
        job.status = "listening"
        (self._memo_q if source == "memo" else self._hear_q).put(job)
        return job

    def submit_text(self, text: str, source: str, external_id: str = "") -> Job:
        job = self._new_job(source, "text", external_id=external_id)
        job.text = (text or "").strip()[:8000]
        self._route(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    @property
    def busy(self) -> bool:
        return self._current is not None

    def waiting(self) -> int:
        return self._do_q.qsize()

    def current_text(self) -> str:
        job = self._current
        return job.command if job else ""

    def cancel(self) -> bool:
        """Stop the running Claude task and drop anything waiting."""
        stopped = False
        while True:
            try:
                waiting = self._do_q.get_nowait()
            except queue.Empty:
                break
            waiting.status = "cancelled"
            waiting.reply = "Cancelled before it started."
            waiting.finished = time.time()
            self._after(waiting)
            stopped = True
        if self._current is not None:
            self._cancel.set()
            stopped = True
        return stopped

    def cancel_job(self, job_id: str) -> bool:
        """Stop one request (used when you press Stop in the phone app)."""
        job = self.get(job_id)
        if job is None or job.status in ("done", "error", "cancelled", "note"):
            return False
        if self._current is job:
            self._cancel.set()
            return True
        job.status = "cancelled"            # still waiting: the "do" loop will skip it
        job.ok = False
        job.reply = "Cancelled before it started."
        job.finished = time.time()
        self._after(job)
        return True

    def new_chat(self) -> str:
        if self.busy:
            self._reset_after_current = True
            return "Okay. I'll start a new conversation after the current task."
        self.claude.new_conversation()
        return "Okay, new conversation."

    def repeat_last(self) -> Job:
        job = self._new_job("web", "text")
        job.text = "repeat"
        self._answer_locally(job, *self._repeat_reply())
        return job

    def project(self) -> tuple[str, Path]:
        """Which folder Claude works in right now (name, path)."""
        projects = self.settings.get("projects") or {}
        chosen = self.state.get("project") or ""
        if chosen and chosen in projects:
            p = resolve_folder(projects[chosen], default_project())
            if p.is_dir():
                return chosen, p
            self._warn_once(chosen, f"Project folder for '{chosen}' doesn't exist: {p}")
        base = resolve_folder(self.settings.get("project_folder", ""), default_project())
        if not base.is_dir():
            self._warn_once("project_folder", f"project_folder doesn't exist: {base}. Using the workspace folder.")
            base = default_project()
        return base.name, base

    # ============================================================== internals
    def _new_job(self, source: str, kind: str, **extra) -> Job:
        job = Job(id=uuid.uuid4().hex[:16], source=source, kind=kind, **extra)
        with self._jobs_lock:
            self._jobs[job.id] = job
            while len(self._jobs) > 200:      # forget very old jobs
                self._jobs.popitem(last=False)
        return job

    def _emit(self, job: Job, event: str) -> None:
        for listener in list(self.listeners):
            try:
                listener(job, event)
            except Exception:
                log.exception("A listener failed")

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned_missing:
            self._warned_missing.add(key)
            log.warning(message)

    # ---- step 1: HEAR ------------------------------------------------------
    def _hear_loop(self, jobs: "queue.Queue[Job]") -> None:
        while True:
            job = jobs.get()
            try:
                job.text = self.stt.transcribe(job.audio_path)
            except Exception as exc:
                log.exception("Speech-to-text failed")
                self._answer_locally(job, f"Sorry, I couldn't process that recording. {exc.__class__.__name__}.",
                                     ok=False)
                continue
            self._route(job)

    def _route(self, job: Job) -> None:
        """Decide what to do with what you said."""
        if job.status == "cancelled":
            return
        if job.text:
            self._emit(job, "heard")
        if not job.text:
            if job.source == "memo":
                job.kind, job.status, job.finished = "note", "note", time.time()
                job.reply = "(no speech found in this recording)"
                self._after(job)
            else:
                self._answer_locally(job, "Sorry, I didn't catch that.", ok=False)
            return
        log.info("[heard] %s", job.text)

        if job.source == "memo":
            command = self._memo_command(job.text)
            if command is None:                # a plain note, not for Claude
                job.kind, job.status, job.finished = "note", "note", time.time()
                self._after(job)
                return
            job.command = command
        else:
            job.command = job.text

        if self._handle_local_command(job):
            return
        ahead = self._do_q.qsize() + (1 if self._current else 0)
        job.status = "queued"
        self._do_q.put(job)
        self._acknowledge(job, ahead)

    def _memo_command(self, text: str) -> str | None:
        """'Claude, add a README' -> 'add a README'. Returns None for plain notes."""
        words = self.settings["voice_memos"]["command_words"]
        for phrase in sorted(words, key=len, reverse=True):
            tokens = [re.escape(t) for t in phrase.split()]
            if not tokens:
                continue
            m = re.match(r"^\W*" + r"[\s,.!?;:-]+".join(tokens) + r"\b[\s,.!?;:-]*", text, re.I)
            if m:
                rest = text[m.end():].strip()
                return rest or None
        return None

    def _handle_local_command(self, job: Job) -> bool:
        said = _normalize(job.command)
        if said in CANCEL:
            reply = "Okay, I stopped." if self.cancel() else "There's nothing to stop."
            self._answer_locally(job, reply)
            return True
        if said in NEW_CHAT:
            self._answer_locally(job, self.new_chat())
            return True
        if said in REPEAT:
            self._answer_locally(job, *self._repeat_reply())
            return True
        if said in STATUS:
            if self._current:
                reply = f"I'm working on: {short_echo(self._current.command)}."
                if self._do_q.qsize():
                    reply += f" {self._do_q.qsize()} more waiting."
            else:
                reply = "I'm free. What should I do?"
            self._answer_locally(job, reply)
            return True
        if said in WHICH_PROJECT:
            name, _path = self.project()
            self._answer_locally(job, f"I'm working in {name}.")
            return True
        m = _SWITCH.match(said)
        if m:
            self._answer_locally(job, self._switch_project(m.group("name")))
            return True
        return False

    def _switch_project(self, spoken: str) -> str:
        projects = self.settings.get("projects") or {}
        wanted = _squash(spoken)
        if spoken.strip() in _DEFAULT_NAMES:
            name = ""
        else:
            name = next((n for n in projects if _squash(n) == wanted), None)
            if name is None:
                return (f"I don't know a project called {spoken}. "
                        "Add it under projects in the settings file.")
        self.state.set(project=name)
        if self.busy:
            self._reset_after_current = True
        else:
            self.claude.new_conversation()
        shown, _ = self.project()
        return f"Switched to {shown}. I started a new conversation there."

    def _repeat_reply(self) -> tuple[str, bool, Path | None]:
        last = self._last_answer
        if last and last.reply:
            return last.reply, True, last.reply_audio if (last.reply_audio and last.reply_audio.exists()) else None
        return "I don't have an answer to repeat yet.", True, None

    def _acknowledge(self, job: Job, ahead: int) -> None:
        """Say "Got it: <what you said>" right away, while Claude works."""
        if job.kind != "audio" or job.source == "memo":
            return
        confirm = self.settings["text_to_voice"]["confirm_what_i_heard"]
        if ahead:
            phrase = f"Got it. I'll do that next: {short_echo(job.command)}." if confirm else "Got it, that's next."
        else:
            phrase = f"Got it: {short_echo(job.command)}." if confirm else "Got it."

        def run():
            path = self.tts.to_file(phrase, f"{job.id}-ack")
            with job.lock:                     # never play "Got it" after the answer
                if path and not job.reply_ready.is_set():
                    job.ack_audio = path
                    if job.source == "desk":
                        self._play_local(path)
                    self._emit(job, "ack")

        threading.Thread(target=run, name="ack", daemon=True).start()

    # ---- step 2: DO --------------------------------------------------------
    def _do_loop(self) -> None:
        while True:
            job = self._do_q.get()
            if job.status == "cancelled":
                continue
            self._current = job
            self._cancel.clear()
            job.status = "working"
            self._emit(job, "working")
            name, folder = self.project()
            log.info("[working] in %s ...", name)
            try:
                result = self.claude.ask(job.command, folder, self._cancel)
            except Exception as exc:
                log.exception("Claude bridge crashed")
                result = ClaudeResult(False, f"Something went wrong while talking to Claude Code: {exc}")
            finally:
                cancelled = self._cancel.is_set()
                self._current = None
            if self._reset_after_current:
                self._reset_after_current = False
                self.claude.new_conversation()
            job.reply = result.text
            job.ok = result.ok
            spoken = make_speakable(result.text, self.settings["text_to_voice"]["max_spoken_chars"])
            if result.denied and result.ok:
                spoken += " Some steps needed permission, so they were skipped."
            if cancelled:
                spoken = ""   # the "stop" command already said "Okay, I stopped."
            log.info("[answer] %s", result.text if len(result.text) < 400 else result.text[:400] + " ...")
            self._speak(job, spoken, status="cancelled" if cancelled else ("done" if result.ok else "error"))
            if result.ok and not cancelled:
                self._last_answer = job

    # ---- step 3: SPEAK -------------------------------------------------------
    def _answer_locally(self, job: Job, text: str, ok: bool = True, audio: Path | None = None) -> None:
        """Answer without Claude (for "stop", "new chat", errors ...)."""
        job.reply = text
        job.ok = ok
        if audio is not None:                   # "repeat": reuse the old voice file
            with job.lock:
                job.reply_ready.set()
            job.spoken = make_speakable(text, self.settings["text_to_voice"]["max_spoken_chars"])
            job.reply_audio = audio
            job.status = "done"
            job.finished = time.time()
            self._play_for(job, audio)
            self._after(job)
            return
        self._speak(job, make_speakable(text, self.settings["text_to_voice"]["max_spoken_chars"]),
                    status="done" if ok else "error")

    def _speak(self, job: Job, spoken: str, status: str) -> None:
        with job.lock:
            job.reply_ready.set()
        job.spoken = spoken
        job.status = "speaking"
        path = self.tts.to_file(spoken, f"{job.id}-reply") if spoken else None
        job.reply_audio = path
        job.status = status
        job.finished = time.time()
        if path:
            self._play_for(job, path)
        self._after(job)

    def _play_for(self, job: Job, path: Path) -> None:
        if job.source == "desk" or (job.source == "memo" and self.settings["voice_memos"]["speak_answers"]):
            self._play_local(path)

    def _play_local(self, path: Path) -> None:
        player = self.local_player
        if player is not None:
            try:
                player.play(path)
            except Exception:
                log.exception("Could not play audio")

    def _after(self, job: Job) -> None:
        """Bookkeeping when a job is finished: history + voice-memo files."""
        if job.finished is None:
            job.finished = time.time()
        try:
            name, _ = self.project()
            self.history.add(job, name)
        except Exception:
            log.exception("Could not write history")
        if job.source == "memo" and self.on_memo_done is not None:
            try:
                self.on_memo_done(job)
            except Exception:
                log.exception("Could not file the voice memo result")
        self._emit(job, "finished")
