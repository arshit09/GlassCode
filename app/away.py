"""
away.py - "away mode": requests from your phone, wherever you are.

Your PC can't be reached from the internet (and shouldn't be), so instead it
asks a small free Cloudflare relay every few seconds: "anything for me?"

    phone app --> relay (Cloudflare, free) <-- this PC asks every 5 s
                                                  |
                                                  v
                                   the same Brain: hear -> do -> speak
                                                  |
    phone app <-- relay <-- answer + voice file --+
    phone     <-- ntfy push notification ("Claude finished: ...")

Only outgoing HTTPS requests are used: no open ports, no port forwarding.
The relay address and keys live in data/away.json (made by away_setup.py).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DATA

log = logging.getLogger("glassesclaude")

CONF_FILE = DATA / "away.json"
_EXT = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/m4a": ".m4a",
        "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "video/webm": ".webm", "video/mp4": ".m4a"}


def load_conf() -> dict | None:
    """The relay address + keys, or None if away mode isn't set up yet."""
    try:
        conf = json.loads(CONF_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if conf.get("relay_url") and conf.get("pc_key"):
        return conf
    return None


def keep_pc_awake() -> bool:
    """Ask Windows not to sleep while this program runs (the screen may still turn off)."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except Exception:
        return False


class AwayLink:
    def __init__(self, settings: dict, brain, conf: dict):
        self.brain = brain
        self.url = conf["relay_url"].rstrip("/")
        self.key = conf["pc_key"]
        self.topic = conf.get("ntfy_topic") or ""
        self.answer_in_notification = bool(settings["away"]["answer_in_notification"])
        self.poll = float(settings["away"]["poll_seconds"])
        self.folder = DATA / "away"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.online = False
        self._by_relay_id: dict[str, object] = {}
        self._outbox: "queue.Queue" = queue.Queue()
        self._warned_at = 0.0
        self._stop = threading.Event()

    def start(self) -> None:
        self.brain.listeners.append(self._on_event)
        threading.Thread(target=self._poll_loop, name="away-poll", daemon=True).start()
        threading.Thread(target=self._send_loop, name="away-send", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._on_event in self.brain.listeners:
            self.brain.listeners.remove(self._on_event)

    # ------------------------------------------------------------- HTTP
    def _request(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None,
                 timeout: float = 30) -> bytes:
        req = urllib.request.Request(self.url + path, data=body, method=method)
        req.add_header("X-Key", self.key)
        req.add_header("User-Agent", "GlassesClaude-PC/1.0")
        if ctype:
            req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _json(self, method: str, path: str, obj: dict | None = None) -> dict:
        body = json.dumps(obj).encode("utf-8") if obj is not None else None
        raw = self._request(method, path, body, "application/json" if body is not None else None)
        return json.loads(raw.decode("utf-8") or "{}")

    # ------------------------------------------------------------ polling
    def _poll_loop(self) -> None:
        wait = self.poll
        while not self._stop.is_set():
            got_job = False
            try:
                data = self._json("GET", "/api/pc/next")
                if not self.online:
                    log.info("[away] connected to your relay - requests from your phone will run here")
                self.online, wait = True, self.poll
                for relay_id in data.get("cancel") or []:
                    job = self._by_relay_id.get(relay_id)
                    if job is not None and self.brain.cancel_job(job.id):
                        log.info("[away] stopped at your phone's request")
                if data.get("job"):
                    self._take(data["job"])
                    got_job = True
            except urllib.error.HTTPError as exc:
                self._problem(f"the relay said {exc.code}" + (" (wrong PC key? run '3 - Set up away mode' again)"
                                                              if exc.code in (401, 503) else ""))
                wait = min(max(wait * 2, self.poll), 60)
            except Exception as exc:
                self._problem(f"can't reach the relay ({exc.__class__.__name__})")
                wait = min(max(wait * 2, self.poll), 60)
            self._stop.wait(0.3 if got_job else wait)    # more might be waiting: check again soon

    def _problem(self, what: str) -> None:
        if self.online or time.monotonic() - self._warned_at > 600:
            log.warning("[away] %s. Retrying in the background.", what)
            self._warned_at = time.monotonic()
        self.online = False

    def _take(self, relay_job: dict) -> None:
        relay_id = relay_job["id"]
        if relay_job.get("kind") == "audio":
            data = self._request("GET", f"/api/pc/jobs/{relay_id}/audio", timeout=60)
            path = self.folder / f"{relay_id}{_EXT.get(relay_job.get('audio_type', ''), '.bin')}"
            path.write_bytes(data)
            job = self.brain.submit_audio(path, "away", external_id=relay_id)
        else:
            job = self.brain.submit_text(relay_job.get("text", ""), "away", external_id=relay_id)
        self._by_relay_id[relay_id] = job
        log.info("[away] new request from your phone")

    # ------------------------------------------------ progress -> relay
    def _on_event(self, job, event: str) -> None:
        if job.source == "away" and job.external_id:
            self._outbox.put((job, event))

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job, event = self._outbox.get(timeout=1)
            except queue.Empty:
                continue
            for attempt in range(6):          # retry for about a minute if the internet hiccups
                try:
                    self._send(job, event)
                    break
                except Exception as exc:
                    if attempt == 5:
                        log.warning("[away] couldn't send '%s' to the relay (%s)", event, exc)
                    time.sleep(2 ** attempt)

    def _send(self, job, event: str) -> None:
        rid = job.external_id
        if event == "heard":
            self._json("POST", f"/api/pc/jobs/{rid}/update", {"status": "heard", "transcript": job.text})
        elif event == "ack" and job.ack_audio and Path(job.ack_audio).exists():
            self._put_audio(rid, "ack", Path(job.ack_audio))
        elif event == "working":
            self._json("POST", f"/api/pc/jobs/{rid}/update", {"status": "working"})
        elif event == "finished":
            if job.reply_audio and Path(job.reply_audio).exists():
                self._put_audio(rid, "answer", Path(job.reply_audio))
            status = job.status if job.status in ("done", "error", "cancelled") else ("done" if job.ok else "error")
            self._json("POST", f"/api/pc/jobs/{rid}/update", {
                "status": status, "transcript": job.text or None, "reply": job.reply or "",
                "spoken": job.spoken or "", "ok": bool(job.ok), "notify": "pc" if self.topic else "relay",
            })
            self._by_relay_id.pop(rid, None)
            if self.topic:
                self._notify(job, rid, status)

    def _notify(self, job, rid: str, status: str) -> None:
        """Push "Claude finished: ..." to your phone through ntfy.sh (free, no account)."""
        titles = {"done": "Claude finished", "error": "Claude hit a problem", "cancelled": "Claude stopped"}
        said = (job.text or "your request")[:70]
        message = (job.spoken or job.reply or "Done.")[:900] if self.answer_in_notification else "Tap to hear the answer."
        payload = {
            "topic": self.topic, "title": f"{titles.get(status, 'Claude')}: {said}"[:120], "message": message,
            "tags": ["white_check_mark" if status == "done" else "warning"], "priority": 4,
            "click": f"{self.url}/?job={rid}",
        }
        try:
            req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "GlassesClaude-PC/1.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    return
        except Exception as exc:
            log.info("[away] ntfy.sh didn't answer from this PC (%s); asking the relay to send it", exc.__class__.__name__)
        try:   # fallback: the relay sends it
            self._json("POST", f"/api/pc/jobs/{rid}/notify", {"include_answer": self.answer_in_notification})
        except Exception as exc:
            log.warning("[away] couldn't send the notification (%s)", exc)

    def _put_audio(self, rid: str, which: str, path: Path) -> None:
        ctype = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
        data = path.read_bytes()
        if len(data) > 1_900_000:             # the relay keeps files under 2 MB
            log.info("[away] answer audio too long to send; the phone will read the text instead")
            return
        self._request("PUT", f"/api/pc/jobs/{rid}/audio/{which}", data, ctype, timeout=60)
