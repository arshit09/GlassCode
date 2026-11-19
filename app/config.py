"""
config.py - reads your settings and remembers small things between runs.

Two kinds of data live here:
  * settings.toml  -> the file YOU edit (project folder, hotkey, voice ...).
  * data/state.json -> things the APP remembers (current Claude conversation,
                       which project you switched to by voice). You never
                       need to edit this one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import tomllib  # built into Python 3.11+; reads .toml files
from copy import deepcopy
from pathlib import Path

log = logging.getLogger("glassesclaude")

# ROOT is the GlassesClaude folder (the one with "1 - Install.bat" in it).
ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
DATA = ROOT / "data"           # models, recordings, spoken answers, state
LOGS = ROOT / "logs"           # history.md = everything you said + every answer
SETTINGS_FILE = ROOT / "settings.toml"
VOICE_PROMPT_FILE = APP / "voice-prompt.txt"

# Permission modes Claude Code understands (see settings.toml for plain words).
PERMISSION_MODES = {"auto", "acceptEdits", "plan", "default", "manual", "dontAsk", "bypassPermissions"}

# These are used when settings.toml is missing a value (or has a broken one).
DEFAULTS: dict = {
    "project_folder": "",
    "projects": {},
    "claude": {
        "permission_mode": "auto",
        "model": "",
        "max_minutes": 15,
        "command": "",
    },
    "voice_to_text": {
        "model": "small",
        "language": "en",
        "hint_words": "Claude, Claude Code, GitHub, npm, React, Next.js, TypeScript, "
                      "JavaScript, Python, Cloudflare, README, commit, pull request, localhost, API",
    },
    "text_to_voice": {
        "voice": "en-US-EmmaMultilingualNeural",
        "speed": "+10%",
        "max_spoken_chars": 600,
        "confirm_what_i_heard": True,
    },
    "desk_mode": {
        "enabled": True,
        "hotkey": "ctrl+alt+space",
        "use_media_key": True,
        "microphone": "rokid",
        "speaker": "rokid",
        "silence_seconds": 1.6,
        "max_record_seconds": 60,
    },
    "voice_memos": {
        "enabled": True,
        "folder": "",
        "command_words": ["claude", "hey claude", "ok claude", "okay claude"],
        "speak_answers": False,
    },
    "web": {
        "port": 8787,
    },
    "away": {
        "enabled": True,
        "poll_seconds": 5,
        "keep_pc_awake": True,
        "answer_in_notification": True,
    },
}


class SettingsError(Exception):
    """settings.toml could not be read. The message explains how to fix it."""


def _merge(defaults: dict, user: dict, path: str = "") -> dict:
    """Copy `defaults`, then lay the user's values on top.

    If a user value has the wrong type (for example text where a number is
    expected) we keep the default and print a warning, so one typo never
    stops the whole app.
    """
    out = deepcopy(defaults)
    for key, value in user.items():
        where = f"{path}{key}"
        if key not in defaults:
            # Unknown keys are kept for tables like [projects] (free-form),
            # and ignored (with a note) everywhere else.
            if path == "projects.":
                out[key] = value
            else:
                log.warning("settings.toml: unknown setting '%s' (ignored)", where)
            continue
        default = defaults[key]
        if isinstance(default, dict):
            if isinstance(value, dict):
                out[key] = _merge(default, value, where + ".") if key != "projects" else {
                    str(k): str(v) for k, v in value.items()
                }
            else:
                log.warning("settings.toml: '%s' should be a section, using defaults", where)
        elif isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
            else:
                log.warning("settings.toml: '%s' should be true or false, using %s", where, default)
        elif isinstance(default, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = type(default)(value) if isinstance(default, float) else value
            else:
                log.warning("settings.toml: '%s' should be a number, using %s", where, default)
        elif isinstance(default, list):
            if isinstance(value, list):
                out[key] = [str(v) for v in value]
            else:
                log.warning("settings.toml: '%s' should be a list like [\"a\", \"b\"], using default", where)
        else:  # strings
            if isinstance(value, str):
                out[key] = value
            else:
                log.warning("settings.toml: '%s' should be text in quotes, using default", where)
    return out


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    """Read settings.toml, fill gaps with DEFAULTS and sanity-check values."""
    user: dict = {}
    if path.exists():
        try:
            with path.open("rb") as fh:
                user = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(
                f"settings.toml has a mistake: {exc}.\n"
                "Tip: write folder paths in SINGLE quotes, for example\n"
                "     project_folder = 'C:\\Users\\you\\Projects\\my-app'"
            ) from exc
    settings = _merge(DEFAULTS, user)

    # --- keep numbers inside sensible ranges --------------------------------
    c = settings["claude"]
    if c["permission_mode"] not in PERMISSION_MODES:
        log.warning("settings.toml: permission_mode '%s' is unknown, using 'auto'", c["permission_mode"])
        c["permission_mode"] = "auto"
    if c["model"] and not re.fullmatch(r"[A-Za-z0-9._\-\[\]]{1,80}", c["model"]):
        log.warning("settings.toml: model name '%s' looks wrong, using your normal model", c["model"])
        c["model"] = ""
    c["max_minutes"] = min(max(int(c["max_minutes"]), 1), 180)

    t = settings["text_to_voice"]
    if not re.fullmatch(r"[+-]\d{1,3}%", t["speed"]):
        t["speed"] = "+0%"
    t["max_spoken_chars"] = min(max(int(t["max_spoken_chars"]), 80), 4000)

    d = settings["desk_mode"]
    d["silence_seconds"] = min(max(float(d["silence_seconds"]), 0.6), 6.0)
    d["max_record_seconds"] = min(max(int(d["max_record_seconds"]), 5), 300)

    w = settings["web"]
    if not (1024 <= int(w["port"]) <= 65535):
        w["port"] = 8787

    a = settings["away"]
    a["poll_seconds"] = min(max(float(a["poll_seconds"]), 2.0), 60.0)
    return settings


def resolve_folder(text: str, fallback: Path) -> Path:
    """Turn a folder from settings into a real path (or use the fallback)."""
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return fallback
    p = Path(os.path.expandvars(os.path.expanduser(text)))
    return p


def default_project() -> Path:
    """The safe practice folder Claude uses until you set project_folder."""
    p = ROOT / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def access_key() -> str:
    """A long random password for the talk page. Created once, then reused."""
    DATA.mkdir(parents=True, exist_ok=True)
    key_file = DATA / "key.txt"
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if len(key) >= 20:
            return key
    key = secrets.token_urlsafe(24)
    key_file.write_text(key, encoding="utf-8")
    return key


class State:
    """Tiny JSON file the app uses to remember things between runs.

    Thread-safe: every read/write holds a lock, and writes go to a temp file
    first and are then swapped in, so a crash can never leave half a file.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or (DATA / "state.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, **values) -> None:
        with self._lock:
            self._data.update(values)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
