"""
main.py - starts GlassesClaude (this is what "2 - Start" runs).

    python -m app.main                  start everything
    python -m app.main --check          print a health check (used by the installer)
    python -m app.main --download-models  download the speech model now
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time

from .config import DATA, LOGS, ROOT, SettingsError, State, access_key, load_settings

# Quiet, private model downloads (no telemetry, no Windows symlink warning).
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

log = logging.getLogger("glassesclaude")

PERMISSION_WORDS = {
    "auto": "auto (Claude does the work; a safety checker blocks risky actions)",
    "acceptEdits": "acceptEdits (Claude can edit files in the project, other commands are refused)",
    "plan": "plan (Claude only reads and plans; nothing is changed)",
    "default": "default (only reading; anything else is refused)",
    "manual": "manual (only reading; anything else is refused)",
    "dontAsk": "dontAsk (only pre-approved tools; anything else is refused)",
    "bypassPermissions": "bypassPermissions (NO SAFETY CHECKS - not recommended for voice!)",
}


def _setup_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")      # never crash on odd characters
        except Exception:
            pass
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    file = logging.handlers.RotatingFileHandler(LOGS / "app.log", maxBytes=1_000_000, backupCount=3,
                                                encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    log.addHandler(console)
    log.addHandler(file)


def _clean_old_files() -> None:
    """Delete old temporary voice files so the folder doesn't grow forever."""
    now = time.time()
    for folder, days in ((DATA / "audio", 2), (DATA / "uploads", 2), (DATA / "recordings", 7)):
        if folder.is_dir():
            for f in folder.iterdir():
                try:
                    if f.is_file() and now - f.stat().st_mtime > days * 86400:
                        f.unlink()
                except OSError:
                    pass


def _model_downloaded(name: str) -> bool:
    models = DATA / "models"
    return models.is_dir() and any(name in p.name for p in models.iterdir() if p.is_dir())


def run_check(settings: dict) -> int:
    from .claude_bridge import ClaudeBridge

    print("\nGlassesClaude health check")
    print("-" * 60)
    print(f"  Folder        : {ROOT}")
    print(f"  Python        : {sys.version.split()[0]}  ({sys.executable})")
    bridge = ClaudeBridge(settings, State())
    exe = bridge.find()
    print(f"  Claude Code   : {bridge.version() or 'found' if exe else 'NOT FOUND'}" + (f"  ({exe})" if exe else ""))
    name = settings["voice_to_text"]["model"]
    print(f"  Speech model  : {name} - {'downloaded' if _model_downloaded(name) else 'not downloaded yet'}")
    try:
        from .audio_io import list_devices, pick_device

        print("  Audio devices :")
        for d in list_devices():
            kind = ("mic" if d["inputs"] else "") + ("+" if d["inputs"] and d["outputs"] else "") + \
                   ("speaker" if d["outputs"] else "")
            print(f"     [{d['index']:>2}] {d['name']}  ({d['api']}, {kind})")
        dm = settings["desk_mode"]
        mic = pick_device(dm["microphone"], "input")
        spk = pick_device(dm["speaker"], "output")
        print(f"  Desk mic      : {mic[1]}" + ("" if mic[3] else f"  (no device named '{dm['microphone']}' yet - "
                                                                 "pair the glasses first)"))
        print(f"  Desk speaker  : {spk[1]}" + ("" if spk[3] else f"  (no device named '{dm['speaker']}' yet)"))
    except Exception as exc:
        print(f"  Audio devices : could not read ({exc})")
    from .away import load_conf

    conf = load_conf()
    print(f"  Away mode     : {'set up - ' + conf['relay_url'] if conf else 'not set up (optional)'}")
    print("-" * 60)
    if not exe:
        print("  Claude Code is missing. Install it from PowerShell with:")
        print("      irm https://claude.ai/install.ps1 | iex")
        print("  then type  claude  once in a terminal to sign in.")
    return 0 if exe else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GlassesClaude")
    parser.add_argument("--check", action="store_true", help="print a health check and exit")
    parser.add_argument("--download-models", action="store_true", help="download the speech model and exit")
    args = parser.parse_args(argv)

    _setup_logging()
    try:
        settings = load_settings()
    except SettingsError as exc:
        print("\n" + str(exc) + "\n")
        return 2

    if args.check:
        return run_check(settings)

    from .stt import SpeechToText

    stt = SpeechToText(settings)
    if args.download_models:
        print(f"Downloading the speech model '{stt.model_name}' (about 0.5 GB for 'small') ...")
        stt.download()
        print("Speech model ready.")
        return 0

    from .brain import Brain, History
    from .claude_bridge import ClaudeBridge
    from .desk_mode import DeskMode
    from .inbox import MemoInbox
    from .tts import TextToSpeech
    from .web import TalkServer

    _clean_old_files()
    state = State()
    key = access_key()
    claude = ClaudeBridge(settings, state)
    tts = TextToSpeech(settings, DATA / "audio")
    brain = Brain(settings, state, stt, claude, tts, History())

    server = TalkServer(settings, brain, key, DATA / "uploads")
    try:
        server.start()
    except OSError:
        print(f"\nPort {server.port} is busy - is GlassesClaude already open in another window?\n"
              "Close the other window, or change  port  under [web] in settings.toml.\n")
        return 3
    (DATA / "talk-page-link.txt").write_text(server.local_link + "\n", encoding="utf-8")

    desk = DeskMode(settings, brain)
    desk_on = settings["desk_mode"]["enabled"] and desk.start()
    memos = MemoInbox(settings, brain)
    memos_on = settings["voice_memos"]["enabled"] and memos.start()

    from .away import AwayLink, keep_pc_awake, load_conf

    away_conf = load_conf() if settings["away"]["enabled"] else None
    away_link = AwayLink(settings, brain, away_conf) if away_conf else None
    awake = bool(away_conf and settings["away"]["keep_pc_awake"] and keep_pc_awake())

    exe = claude.find()
    version = claude.version() if exe else None
    name, folder = brain.project()
    line = "=" * 66
    print(f"\n{line}\n  GlassesClaude is running.  Keep this window open (close it to stop).\n{line}")
    print(f"  Claude Code : {version or 'found' if exe else 'NOT FOUND - install Claude Code, then restart'}")
    print(f"  Project     : {name}   ({folder})")
    print(f"  Permission  : {PERMISSION_WORDS.get(settings['claude']['permission_mode'])}")
    print()
    if desk_on:
        print("  TALK AT YOUR DESK  (glasses paired to this PC by Bluetooth)")
        print(f"    Mic     : {desk.mic_name}" + ("" if desk.mic_matched else
                                                  "   <- not your glasses? pair them, then restart"))
        print(f"    Speaker : {desk.speaker_name}")
        if not desk.mic_matched:
            try:
                from .audio_io import list_devices

                mics = sorted({d["name"] for d in list_devices() if d["inputs"] > 0})
                print("    Microphones this PC can see: " + "; ".join(mics[:12]))
                print('    Put part of the glasses\' name in settings.toml: microphone = "..." and speaker = "..."')
            except Exception:
                pass
        tap = " (or tap your glasses)" if settings["desk_mode"]["use_media_key"] else ""
        print(f"    Press {desk.hotkey_text}{tap}, talk, then pause.")
    elif settings["desk_mode"]["enabled"]:
        print(f"  TALK AT YOUR DESK : not available - {desk.problem}")
    print()
    print("  TALK PAGE on this PC (open in your browser):")
    print(f"    {server.local_link}")
    print("    For your phone when you're out, use away mode (below).")
    print()
    if away_conf:
        print("  AWAY MODE (your phone, from anywhere)")
        print(f"    Relay : {away_conf['relay_url']}")
        print(f"    Phone setup page (QR code): {DATA / 'away-phone-link.html'}")
        if awake:
            print("    This PC stays awake while GlassesClaude runs.")
        print()
    elif settings["away"]["enabled"]:
        print("  AWAY MODE: not set up yet - double-click '3 - Set up away mode (optional)'.")
        print()
    if memos_on:
        print("  VOICE MEMOS: put recordings in")
        print(f"    {memos.folder}")
        print('    Start a memo with "Claude, ..." to make it a command. Others are saved as notes.')
        print()
    print('  Say "stop" to cancel, "new chat" to start fresh, "repeat" to hear the last answer,')
    print('  "which project" / "switch to <name>" to change folders.')
    print(f"  Everything is saved in {LOGS / 'history.md'}")
    print(f"{line}\n")
    if settings["claude"]["permission_mode"] == "bypassPermissions":
        log.warning("WARNING: permission_mode is bypassPermissions - Claude can do ANYTHING without checks.")
    if away_link is not None:
        away_link.start()          # starts after the banner so its messages appear below it

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping GlassesClaude ...")
        if away_link is not None:
            away_link.stop()
        brain.cancel()
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
