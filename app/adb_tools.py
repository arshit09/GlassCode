"""
adb_tools.py - OPTIONAL helpers for glasses that allow "ADB debugging".

Not needed for normal use. Some Rokid glasses let a PC talk to them over a
USB data cable once Developer Mode / ADB debugging is switched on in the
Hi Rokid app. Then these helpers can:

    python -m app.adb_tools check   -> is the PC able to see the glasses?
    python -m app.adb_tools pull    -> copy new voice recordings from the
                                       glasses into the voice-memo folder

Google's free "platform-tools" (adb) is downloaded automatically the first
time, into GlassesClaude/tools/platform-tools.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from .config import DATA, ROOT, load_settings, resolve_folder

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
AUDIO = (".m4a", ".aac", ".wav", ".mp3", ".opus", ".ogg", ".amr", ".3gp", ".flac")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ensure_adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    local = ROOT / "tools" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    if local.exists():
        return str(local)
    if os.name != "nt":
        print("Install adb (Android platform-tools) and try again.")
        return None
    print("Downloading Google's platform-tools (adb), about 7 MB ...")
    try:
        with urllib.request.urlopen(PLATFORM_TOOLS_URL, timeout=120) as resp:
            data = resp.read()
        zipfile.ZipFile(io.BytesIO(data)).extractall(ROOT / "tools")
    except Exception as exc:
        print(f"Download failed: {exc}")
        return None
    return str(local) if local.exists() else None


def adb(exe: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=_NO_WINDOW)


def connected_device(exe: str) -> str | None:
    out = adb(exe, "devices", "-l").stdout
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    for l in lines:
        parts = l.split()
        if len(parts) > 1 and parts[1] == "device":     # "SERIAL device product:... model:..."
            return parts[0]
    if any("unauthorized" in l for l in lines):
        print("The glasses are connected but not allowed yet: accept the prompt on the glasses/Hi Rokid app.")
    return None


def list_recordings(exe: str) -> list[str]:
    names = " -o ".join(f"-iname '*{e}'" for e in AUDIO)
    out = adb(exe, "shell", f"find /sdcard -type f \\( {names} \\) 2>/dev/null", timeout=120).stdout
    return [p.strip() for p in out.splitlines() if p.strip() and "/Android/" not in p]


def check() -> int:
    exe = ensure_adb()
    if not exe:
        return 1
    serial = connected_device(exe)
    if not serial:
        print("No glasses found over ADB.\n"
              "  1. Use a DATA cable (some magnetic charging cables only charge).\n"
              "  2. In the Hi Rokid app: Settings > glasses settings > Developer > turn on ADB debugging\n"
              "     (if your model has it).\n"
              "  3. Plug in, accept any prompt, and run this again.\n"
              "Not every model allows this - the desk mode and voice memos work without it.")
        return 1
    model = adb(exe, "shell", "getprop ro.product.model").stdout.strip()
    android = adb(exe, "shell", "getprop ro.build.version.release").stdout.strip()
    files = list_recordings(exe)
    print(f"Found glasses: {model or serial} (Android {android or '?'})")
    print(f"Voice recordings on the glasses: {len(files)}")
    for f in files[:20]:
        print(f"   {f}")
    return 0


def pull() -> int:
    exe = ensure_adb()
    if not exe or not connected_device(exe):
        print("Glasses not connected over ADB. Run the 'Check glasses with ADB' tool first.")
        return 1
    settings = load_settings()
    target = resolve_folder(settings["voice_memos"]["folder"], ROOT / "inbox")
    target.mkdir(parents=True, exist_ok=True)
    seen_file = DATA / "pulled.json"
    seen = set(json.loads(seen_file.read_text(encoding="utf-8"))) if seen_file.exists() else set()
    new = [f for f in list_recordings(exe) if f not in seen]
    if not new:
        print("No new recordings.")
        return 0
    staging = DATA / "pulling"
    staging.mkdir(parents=True, exist_ok=True)
    for remote in new:
        name = remote.rsplit("/", 1)[-1]
        local = staging / name
        result = adb(exe, "pull", remote, str(local), timeout=600)
        if result.returncode == 0 and local.exists():
            final = target / name
            n = 2
            while final.exists():
                final = target / f"{Path(name).stem} ({n}){Path(name).suffix}"
                n += 1
            shutil.move(str(local), str(final))   # appears in the memo folder only when complete
            seen.add(remote)
            print(f"Copied {name}")
        else:
            print(f"Could not copy {remote}: {result.stderr.strip()[:200]}")
    seen_file.write_text(json.dumps(sorted(seen), indent=1), encoding="utf-8")
    print(f"Done. GlassesClaude will process them from: {target}")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    sys.exit(pull() if action == "pull" else check())
