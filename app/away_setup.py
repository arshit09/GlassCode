"""
away_setup.py - one-time setup for away mode ("3 - Set up away mode").

What it does, step by step:
  1. Makes three private random values: a key for your phone, a key for this
     PC, and a private ntfy topic for notifications. (Saved in data/away.json.)
  2. Signs you in to your free Cloudflare account (opens your browser once).
  3. Puts the relay (the "relay" folder) on Cloudflare Workers - free plan.
  4. Gives the relay the two keys and the topic as secrets.
  5. Checks that it answers, then opens a page with a QR code for your phone.

Safe to run again: it keeps your keys and just updates the relay.
Run with --new-keys to make new keys (old phone links stop working).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from .config import DATA, ROOT

RELAY = ROOT / "relay"
CONF_FILE = DATA / "away.json"
WRANGLER = "wrangler@4"
_URL = re.compile(r"https://[a-z0-9][a-z0-9.-]*\.workers\.dev")


def say(text: str = "") -> None:
    print(text, flush=True)


def find_npx() -> str | None:
    found = shutil.which("npx")
    if found:
        return found
    for c in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "npx.cmd",
              Path(os.environ.get("APPDATA", "")) / "npm" / "npx.cmd"):
        if c.exists():
            return str(c)
    return None


def _env() -> dict:
    return dict(os.environ, WRANGLER_SEND_METRICS="false", FORCE_COLOR="0", NO_COLOR="1")


def wrangler(npx: str, *args: str, capture: bool = True) -> tuple[int, str]:
    """Run `npx wrangler@4 <args>` in the relay folder, showing its output as it comes."""
    cmd = [npx, "--yes", WRANGLER, *args]
    if not capture:   # fully interactive (used for the browser sign-in)
        return subprocess.run(cmd, cwd=RELAY, env=_env()).returncode, ""
    proc = subprocess.Popen(cmd, cwd=RELAY, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    lines = []
    for line in proc.stdout:
        print("   " + line.rstrip(), flush=True)
        lines.append(line)
    return proc.wait(), "".join(lines)


def make_conf(new_keys: bool) -> dict:
    conf = {}
    if CONF_FILE.exists() and not new_keys:
        try:
            conf = json.loads(CONF_FILE.read_text(encoding="utf-8"))
        except ValueError:
            conf = {}
    alphabet = string.ascii_lowercase + string.digits
    conf.setdefault("phone_key", secrets.token_urlsafe(24))
    conf.setdefault("pc_key", secrets.token_urlsafe(24))
    conf.setdefault("ntfy_topic", "gc-" + "".join(secrets.choice(alphabet) for _ in range(22)))
    if new_keys:
        conf.pop("relay_url", None)
    return conf


def save_conf(conf: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    CONF_FILE.write_text(json.dumps(conf, indent=2), encoding="utf-8")


def check_relay(url: str, key: str) -> dict | None:
    req = urllib.request.Request(url.rstrip("/") + "/api/status", headers={"X-Key": key, "User-Agent": "GlassesClaude-setup"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def phone_page(conf: dict) -> Path:
    """A local page with a QR code for the phone link and the notification steps."""
    import qrcode
    import qrcode.image.svg

    link = f"{conf['relay_url']}/#key={conf['phone_key']}"
    svg = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, box_size=11).to_string(encoding="unicode")
    page = DATA / "away-phone-link.html"
    page.write_text(f"""<!doctype html><meta charset="utf-8"><title>GlassesClaude - phone setup</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<body style="font:17px/1.5 'Segoe UI',system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 20px;color:#13233a;background:#eef2f5">
<h1 style="font-family:Bahnschrift,system-ui;font-weight:600">Set up your phone</h1>
<h2 style="font-family:Bahnschrift,system-ui;font-weight:600;font-size:21px">1. Open the app on your phone</h2>
<p>Scan this with your phone camera. It opens GlassesClaude in the browser with your private key.
Then use the browser menu: <b>Add to Home screen</b> (Android: Install app).</p>
<p><b>iPhone:</b> in Safari first tap <b>Settings</b> &gt; <b>Copy key</b> in the GlassesClaude page, then Share &gt;
<b>Add to Home Screen</b>. Open the new icon once and paste the key into the box it shows.</p>
<div style="width:300px;background:#fff;padding:12px;border-radius:12px">{svg}</div>
<p style="word-break:break-all;font-size:14px;color:#56677c">{link}</p>
<p><b>Keep this link private.</b> Anyone who has it can send requests to Claude on this PC.</p>
<h2 style="font-family:Bahnschrift,system-ui;font-weight:600;font-size:21px">2. Get a notification when Claude is done</h2>
<ol><li>Install the free <b>ntfy</b> app from Google Play or the App Store.</li>
<li>Tap <b>+</b>, keep the server <b>ntfy.sh</b>, and subscribe to this topic:
<div style="font:18px Consolas,monospace;background:#fff;padding:8px 12px;border-radius:8px;margin:6px 0">{conf['ntfy_topic']}</div></li>
<li>In the GlassesClaude app, open <b>Settings</b> and tap <b>Send a test</b>.</li></ol>
<h2 style="font-family:Bahnschrift,system-ui;font-weight:600;font-size:21px">3. Keep your PC ready</h2>
<p>Leave "2 - Start" running and the PC on. GlassesClaude keeps the PC awake while it runs.
To start it automatically, run <b>tools\\Start GlassesClaude with Windows (optional)</b>.</p>
<h2 style="font-family:Bahnschrift,system-ui;font-weight:600;font-size:21px">Android: keep the phone in your pocket (optional)</h2>
<p>The Android app in the <b>android</b> folder lets you tap your glasses to talk, with the phone locked in your pocket.
Install it with Android Studio (steps in <b>android\\README.md</b>), then in the web app tap <b>Settings</b> &gt;
<b>Copy phone link</b> and paste it into the Android app.</p>
</body>""", encoding="utf-8")
    return page


def main(argv: list[str]) -> int:
    say("\n  GlassesClaude - away mode setup")
    say("  Everything used here is free: Cloudflare Workers (free plan) and ntfy.sh.\n")
    npx = find_npx()
    if not npx:
        say("  Node.js is needed once for Cloudflare's tool. Install it with:")
        say("      winget install -e --id OpenJS.NodeJS.LTS")
        say("  then run '3 - Set up away mode (optional)' again.")
        return 1

    conf = make_conf("--new-keys" in argv)
    save_conf(conf)

    say("[1/4] Checking your Cloudflare sign-in ...")
    code, out = wrangler(npx, "whoami")
    if code != 0 or "not authenticated" in out.lower() or "you are not logged in" in out.lower():
        say("\n      Your browser will open: sign in to Cloudflare (a free account is fine) and click Allow.\n")
        code, _ = wrangler(npx, "login", capture=False)
        if code != 0:
            say("  Sign-in didn't finish. Run this again when you're ready.")
            return 1

    say("\n[2/4] Putting the relay on Cloudflare (free plan) ...")
    code, out = wrangler(npx, "deploy")
    if code != 0:
        if "workers.dev" in out and "subdomain" in out:
            say("\n  Cloudflare needs a free workers.dev address first:")
            say("  open https://dash.cloudflare.com -> Workers & Pages, choose a subdomain, then run this again.")
        else:
            say("\n  The upload failed (see the messages above). Check your internet and run this again.")
        return 1
    found = _URL.findall(out)
    url = found[-1] if found else conf.get("relay_url", "")
    if not url:
        url = input("\n  Paste the https://....workers.dev address shown above: ").strip()
    if not _URL.fullmatch(url):
        say("  That doesn't look like a workers.dev address. Run this again.")
        return 1
    conf["relay_url"] = url
    save_conf(conf)

    say("\n[3/4] Giving the relay your private keys ...")
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"PHONE_KEY": conf["phone_key"], "PC_KEY": conf["pc_key"], "NTFY_TOPIC": conf["ntfy_topic"]}, fh)
        code, out = wrangler(npx, "secret", "bulk", tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)
    if code != 0:
        say("  Saving the keys failed. Run this again.")
        return 1

    say("\n[4/4] Checking that the relay answers ...")
    status = None
    for _ in range(12):
        status = check_relay(url, conf["phone_key"])
        if status is not None:
            break
        time.sleep(3)
    if status is None:
        say(f"  The relay doesn't answer yet at {url}. Wait a minute and run this again.")
        return 1

    page = phone_page(conf)
    say("\n  =================================================================")
    say("  Away mode is ready!")
    say(f"  Relay: {url}")
    say("  Next:")
    say("   1. Close and reopen '2 - Start' (it now connects to the relay).")
    say("   2. On your phone, scan the QR code on the page that just opened.")
    say("   3. Install the ntfy app and subscribe to your topic (shown on that page).")
    say("  =================================================================\n")
    try:
        webbrowser.open(page.as_uri())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
