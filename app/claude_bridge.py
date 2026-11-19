"""
claude_bridge.py - hands your spoken command to Claude Code on this PC.

How it works (the same as typing in a terminal):

    claude -p --output-format json --resume <conversation-id> ...  <- your words go in on stdin

  * "-p" runs Claude Code once without the interactive screen and prints
    the answer.
  * "--resume" keeps ONE ongoing "glasses" conversation, so Claude remembers
    what you said before ("now commit that").
  * Your words are passed through stdin (not as a command-line argument),
    so special characters you happen to say can never break the command.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import VOICE_PROMPT_FILE, State

log = logging.getLogger("glassesclaude")

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_UNKNOWN_OPTION = re.compile(r"unknown option ['\"]?(--[a-z0-9-]+)", re.I)

# Used only if this Claude Code version is too old for --append-system-prompt-file.
_INLINE_VOICE_PROMPT = (
    "You are controlled by voice through screenless AI glasses and your final answer is read aloud. "
    "Answer in one to three short plain spoken sentences with no markdown, lists, code blocks or links. "
    "Say briefly what you did and the result. The request comes from speech recognition and may contain "
    "small mistakes, so interpret it sensibly, and ask one short question if it is unclear or risky."
)


@dataclass
class ClaudeResult:
    ok: bool                 # False when something went wrong
    text: str                # Claude's answer (or a plain-words error)
    session_id: str | None = None
    cost_usd: float | None = None
    denied: int = 0          # how many actions the safety rules blocked


class ClaudeBridge:
    def __init__(self, settings: dict, state: State):
        cfg = settings["claude"]
        self.permission_mode = cfg["permission_mode"]
        self.model = cfg["model"]
        self.max_seconds = int(cfg["max_minutes"]) * 60
        self.command_setting = (cfg["command"] or "").strip().strip('"').strip("'")
        self.state = state
        self._skip_options: set[str] = set()   # flags this Claude Code version doesn't know
        self._lock = threading.Lock()          # one Claude run at a time

    # ------------------------------------------------------------------ setup
    def find(self) -> str | None:
        """Locate the `claude` program (works for the normal and npm installs)."""
        if self.command_setting:
            p = Path(os.path.expandvars(os.path.expanduser(self.command_setting)))
            return str(p) if p.exists() else shutil.which(self.command_setting)
        found = shutil.which("claude")
        if found:
            return found
        home = Path.home()
        candidates = [
            home / ".local" / "bin" / "claude.exe",                     # native Windows installer
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",  # npm install -g
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "claude.exe",
            home / ".local" / "bin" / "claude",                         # macOS / Linux
            home / ".claude" / "local" / "claude",
        ]
        for c in candidates:
            if str(c) and c.exists():
                return str(c)
        return None

    def version(self) -> str | None:
        exe = self.find()
        if not exe:
            return None
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=60, creationflags=_NO_WINDOW)
            return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
        except Exception:
            return None

    def new_conversation(self) -> None:
        self.state.set(session_id=None)

    # -------------------------------------------------------------------- ask
    def ask(self, prompt: str, project_dir: Path, cancel: threading.Event) -> ClaudeResult:
        """Run one command in Claude Code and return its answer."""
        exe = self.find()
        if not exe:
            return ClaudeResult(False, "I can't find Claude Code on this PC. Install it, sign in once by "
                                       "typing claude in a terminal, then restart GlassesClaude.")
        with self._lock:
            session = self.state.get("session_id")
            if session and not _UUID.match(str(session)):
                session = None
            for _attempt in range(4):  # retries cover: old flags, expired conversation
                cmd = self._build(exe, session)
                out, err, status = self._run(cmd, prompt, project_dir, cancel)
                if status == "cancelled":
                    return ClaudeResult(False, "Okay, I stopped that.")
                if status == "timeout":
                    return ClaudeResult(False, f"That took longer than {self.max_seconds // 60} minutes, "
                                               "so I stopped it. Try a smaller step.")
                if status == "failed":
                    return ClaudeResult(False, f"I couldn't start Claude Code: {err}")
                # Old Claude Code without a newer flag? Drop that flag and retry.
                m = _UNKNOWN_OPTION.search(err or "")
                if m and m.group(1) not in self._skip_options:
                    log.info("This Claude Code version doesn't know %s, retrying without it.", m.group(1))
                    self._skip_options.add(m.group(1))
                    continue
                # The saved conversation is gone? Start a fresh one.
                if session and "no conversation found" in (out + err).lower():
                    log.info("The previous conversation wasn't found, starting a new one.")
                    session = None
                    self.state.set(session_id=None)
                    continue
                data = _parse_result(out)
                if data is None:
                    detail = (err or out or "no output").strip().splitlines()
                    return ClaudeResult(False, "Claude Code didn't give an answer. " +
                                        (detail[-1][:200] if detail else ""))
                sid = data.get("session_id")
                if isinstance(sid, str) and _UUID.match(sid):
                    self.state.set(session_id=sid)
                text = str(data.get("result") or "").strip()
                denied = len(data.get("permission_denials") or [])
                if data.get("is_error"):
                    return ClaudeResult(False, f"Claude Code reported a problem: {text or 'unknown error'}",
                                        sid, data.get("total_cost_usd"), denied)
                return ClaudeResult(True, text or "Done.", sid, data.get("total_cost_usd"), denied)
        return ClaudeResult(False, "Claude Code kept failing. Open a terminal and run claude to check it works.")

    # --------------------------------------------------------------- helpers
    def _build(self, exe: str, session: str | None) -> list[str]:
        cmd = [exe, "-p", "--output-format", "json", "--permission-mode", self.permission_mode]
        if "--permission-prompts" not in self._skip_options:
            # Nobody can click "Allow" while you're only talking, so anything that would
            # ask for permission is refused instead of waiting forever.
            cmd += ["--permission-prompts", "none"]
        if "--append-system-prompt-file" not in self._skip_options and VOICE_PROMPT_FILE.exists():
            cmd += ["--append-system-prompt-file", str(VOICE_PROMPT_FILE)]
        else:
            cmd += ["--append-system-prompt", _INLINE_VOICE_PROMPT]
        if self.model:
            cmd += ["--model", self.model]
        if session:
            cmd += ["--resume", session]
        return cmd

    def _run(self, cmd: list[str], prompt: str, cwd: Path, cancel: threading.Event):
        """Start Claude Code, feed the prompt, wait (watching for cancel/timeout)."""
        kwargs = dict(cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, encoding="utf-8", errors="replace")
        if os.name == "nt":
            kwargs["creationflags"] = _NO_WINDOW
        else:
            kwargs["start_new_session"] = True  # lets us stop Claude and anything it started
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            return "", str(exc), "failed"
        deadline = time.monotonic() + self.max_seconds
        first = True
        while True:
            try:
                out, err = proc.communicate(input=prompt if first else None, timeout=0.5)
                return out or "", err or "", "done"
            except subprocess.TimeoutExpired:
                first = False
                if cancel.is_set() or time.monotonic() > deadline:
                    _kill_tree(proc)
                    try:
                        proc.communicate(timeout=10)
                    except Exception:
                        pass
                    return "", "", "cancelled" if cancel.is_set() else "timeout"


def _parse_result(out: str) -> dict | None:
    """Find Claude Code's JSON result in its output."""
    s = (out or "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    for line in reversed(s.splitlines()):   # in case something printed before the JSON
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict) and ("result" in data or data.get("type") == "result"):
                    return data
            except ValueError:
                continue
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop Claude Code AND anything it started (tests, servers ...)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=_NO_WINDOW, timeout=20)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
