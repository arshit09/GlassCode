"""
web.py - the small web server behind the talk page.

It only listens on this PC (127.0.0.1), so nothing on your network can reach
it. Every /api request must carry the secret key (data/key.txt); the talk
page link printed at startup contains it. (For your phone, away mode uses a
separate free Cloudflare relay instead - see away.py.)

  GET  /                      the talk page
  POST /api/ask               audio (from the mic) or {"text": "..."} -> job
  GET  /api/jobs/<id>         progress of a job
  GET  /api/audio/<id>/<ack|reply>   voice files to play
  POST /api/cancel | /api/new-chat | /api/repeat
  GET  /api/status
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .config import APP

log = logging.getLogger("glassesclaude")

MAX_UPLOAD = 25 * 1024 * 1024        # 25 MB is about 20+ minutes of compressed speech
_JOB_ID = re.compile(r"^[0-9a-f]{16}$")
_EXT_BY_TYPE = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/wave": ".wav", "video/webm": ".webm", "video/mp4": ".m4a",
}
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "media-src 'self' blob: data:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")


class _Lockout:
    """After many wrong keys, refuse everyone for 10 minutes (stops guessing)."""

    def __init__(self, limit: int = 30, window: float = 600):
        self.limit, self.window = limit, window
        self.failures: list[float] = []
        self.until = 0.0
        self.lock = threading.Lock()

    def blocked(self) -> bool:
        return time.monotonic() < self.until

    def fail(self) -> None:
        with self.lock:
            now = time.monotonic()
            self.failures = [t for t in self.failures if now - t < self.window] + [now]
            if len(self.failures) >= self.limit:
                self.until = now + self.window
                self.failures.clear()
                log.warning("Too many wrong keys on the talk page: locked for 10 minutes.")


class TalkServer:
    def __init__(self, settings: dict, brain, key: str, upload_dir: Path):
        self.port = int(settings["web"]["port"])
        self.brain = brain
        self.key = key
        self.upload_dir = upload_dir
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.lockout = _Lockout()
        self.httpd: ThreadingHTTPServer | None = None

    @property
    def local_link(self) -> str:
        return f"http://localhost:{self.port}/#key={self.key}"

    def start(self) -> None:
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True).start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()


def _make_handler(server: TalkServer):
    index_file = APP / "static" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "GlassesClaude"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep the window quiet
            log.debug("web: " + fmt, *args)

        # ---------------------------------------------------------- helpers
        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _authed(self) -> bool:
            if server.lockout.blocked():
                self._json(429, {"error": "Too many wrong keys. Try again in 10 minutes."})
                return False
            given = self.headers.get("X-Key", "")
            if given and hmac.compare_digest(given.encode(), server.key.encode()):
                return True
            server.lockout.fail()
            self._json(401, {"error": "Wrong or missing key. Open the link shown in the GlassesClaude window."})
            return False

        def _body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_UPLOAD:
                self._json(413 if length > MAX_UPLOAD else 400, {"error": "Missing or too large body."})
                return None
            return self.rfile.read(length)

        def _status(self) -> dict:
            name, path = server.brain.project()
            return {"ok": True, "project": name, "project_path": str(path), "busy": server.brain.busy,
                    "waiting": server.brain.waiting(), "working_on": server.brain.current_text()}

        # ---------------------------------------------------------- routes
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                try:
                    body = index_file.read_bytes()
                except OSError:
                    body = b"<h1>Talk page file missing</h1>"
                self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": _CSP})
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if not path.startswith("/api/"):
                self._json(404, {"error": "Not found"})
                return
            if not self._authed():
                return
            if path == "/api/status":
                self._json(200, self._status())
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[1] == "jobs" and _JOB_ID.match(parts[2]):
                job = server.brain.get(parts[2])
                self._json(200, {"job": job.public()}) if job else self._json(404, {"error": "Unknown job"})
                return
            if len(parts) == 4 and parts[1] == "audio" and _JOB_ID.match(parts[2]) and parts[3] in ("ack", "reply"):
                job = server.brain.get(parts[2])
                file = (job.ack_audio if parts[3] == "ack" else job.reply_audio) if job else None
                if not file or not Path(file).exists():
                    self._json(404, {"error": "No audio"})
                    return
                ctype = "audio/mpeg" if Path(file).suffix == ".mp3" else "audio/wav"
                self._send(200, Path(file).read_bytes(), ctype)
                return
            self._json(404, {"error": "Not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                self._json(404, {"error": "Not found"})
                return
            if not self._authed():
                return
            if path == "/api/ask":
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                body = self._body()
                if body is None:
                    return
                if ctype == "application/json":
                    try:
                        text = str(json.loads(body.decode("utf-8")).get("text", "")).strip()
                    except (ValueError, AttributeError):
                        self._json(400, {"error": "Bad JSON"})
                        return
                    if not text:
                        self._json(400, {"error": "Empty command"})
                        return
                    job = server.brain.submit_text(text, "web")
                else:
                    ext = _EXT_BY_TYPE.get(ctype, ".bin")
                    file = server.upload_dir / f"{uuid.uuid4().hex}{ext}"
                    file.write_bytes(body)
                    job = server.brain.submit_audio(file, "web")
                self._json(200, {"job": job.public()})
                return
            if path == "/api/cancel":
                self._json(200, {"stopped": server.brain.cancel()})
                return
            if path == "/api/new-chat":
                self._json(200, {"message": server.brain.new_chat()})
                return
            if path == "/api/repeat":
                job = server.brain.repeat_last()
                self._json(200, {"job": job.public()})
                return
            self._json(404, {"error": "Not found"})

    return Handler
