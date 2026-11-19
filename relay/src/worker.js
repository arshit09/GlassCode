// GlassesClaude relay - runs free on Cloudflare Workers.
//
// It is a small "mailbox" between your phone and your PC:
//
//   phone app  --(your voice or text)-->  relay  <--(asks "anything for me?" every few seconds)--  your PC
//   phone app  <--(answer + voice file)--  relay  <--(answer when Claude is done)----------------  your PC
//                                            |
//                                            +--> ntfy.sh push notification to your phone
//
// Nothing here runs Claude: your PC does all the work. The relay only stores
// requests and answers for a few days, inside one SQLite-backed Durable Object.
//
// Notifications: your PC sends "Claude finished" itself (from your home internet), and
// only asks the relay to send it if that fails. The relay sends the rare "not run, PC
// offline" notice, because then your PC can't.
//
// Secrets (set by "3 - Set up away mode"):
//   PHONE_KEY   - lets the phone app send requests and read answers
//   PC_KEY      - lets your PC pick up requests and post answers
//   NTFY_TOPIC  - the private ntfy.sh topic your phone subscribes to
// Optional plain variables (wrangler.toml [vars]):
//   NTFY_INCLUDE_ANSWER = "yes" | "no"   (put the answer text inside the notification)
//   QUEUE_MAX_MINUTES   = "120"           (don't run requests that waited longer than this)

import { DurableObject } from "cloudflare:workers";

const MAX_AUDIO = 1_900_000;          // a Durable Object SQLite row can hold up to 2 MB
const MAX_TEXT = 20_000;
const KEEP_DAYS = 3;                  // old requests and answers are deleted after this
const PC_ONLINE_MS = 30_000;          // PC counts as "online" if it checked in recently
const FINAL = ["done", "error", "cancelled", "expired"];
const ID = /^[0-9a-f]{16}$/;

// ------------------------------------------------------------------ helpers
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
const text = (body, status = 200) =>
  new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });

function safeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a), y = enc.encode(b);
  if (x.byteLength !== y.byteLength) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

function cleanString(v, max = MAX_TEXT) {
  if (v === undefined || v === null) return null;
  return String(v).slice(0, max);
}

function newId() {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 16);
}

// --------------------------------------------------------- entry point
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // The phone app (index.html etc.) is served by Workers Static Assets before
    // this code runs. Everything under /api/ goes to the one mailbox object.
    if (url.pathname.startsWith("/api/")) {
      const mailbox = env.MAILBOX.get(env.MAILBOX.idFromName("main"));
      return mailbox.fetch(request);
    }
    return text("Not found", 404);
  },
};

// ------------------------------------------------------------ the mailbox
export class Mailbox extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.env = env;
    this.sql = ctx.storage.sql;
    this.failures = new Map();
    this.sql.exec(`CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY,
      created INTEGER NOT NULL,
      updated INTEGER NOT NULL,
      status TEXT NOT NULL,
      kind TEXT NOT NULL,
      source TEXT,
      text TEXT,
      audio BLOB, audio_type TEXT,
      transcript TEXT, reply TEXT, spoken TEXT, ok INTEGER,
      ack BLOB, ack_type TEXT,
      answer BLOB, answer_type TEXT,
      cancel_requested INTEGER NOT NULL DEFAULT 0,
      notified INTEGER NOT NULL DEFAULT 0,
      notify_tries INTEGER NOT NULL DEFAULT 0
    )`);
    // Add columns that newer versions of this file use (safe to run every time).
    const have = new Set(this.sql.exec("PRAGMA table_info(jobs)").toArray().map((c) => c.name));
    if (!have.has("notify_tries")) this.sql.exec("ALTER TABLE jobs ADD COLUMN notify_tries INTEGER NOT NULL DEFAULT 0");
    this.sql.exec("CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created)");
    this.sql.exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)");
  }

  // ---- small storage helpers
  meta(k) {
    const row = this.sql.exec("SELECT v FROM meta WHERE k = ?", k).toArray()[0];
    return row ? row.v : null;
  }
  setMeta(k, v) {
    this.sql.exec("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", k, String(v));
  }
  row(id) {
    return this.sql.exec(
      `SELECT id, status, kind, source, created, updated, text, transcript, reply, spoken, ok,
              cancel_requested, notified, (ack IS NOT NULL) AS has_ack, (answer IS NOT NULL) AS has_answer
       FROM jobs WHERE id = ?`, id).toArray()[0] || null;
  }
  publicJob(r) {
    if (!r) return null;
    return {
      id: r.id, status: r.status, kind: r.kind, created: r.created, updated: r.updated,
      said: r.transcript || r.text || "", reply: r.reply || "", spoken: r.spoken || "",
      ok: r.ok === null || r.ok === undefined ? null : !!r.ok,
      has_ack: !!r.has_ack, has_answer: !!r.has_answer,
    };
  }

  // ---- who is calling? (phone, pc, or nobody)
  // A correct key always works. Wrong keys are counted per IP address, and an
  // address with 30 wrong keys in 10 minutes is refused for 10 minutes.
  auth(request) {
    const key = request.headers.get("X-Key") || "";
    if (key && this.env.PHONE_KEY && safeEqual(key, this.env.PHONE_KEY)) return "phone";
    if (key && this.env.PC_KEY && safeEqual(key, this.env.PC_KEY)) return "pc";
    const now = Date.now();
    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    const entry = this.failures.get(ip) || { times: [], until: 0 };
    if (now < entry.until) return "locked";
    entry.times = entry.times.filter((t) => now - t < 600_000);
    entry.times.push(now);
    if (entry.times.length >= 30) { entry.until = now + 600_000; entry.times = []; }
    this.failures.set(ip, entry);
    if (this.failures.size > 1000) this.failures.clear();
    return now < entry.until ? "locked" : null;
  }

  async fetch(request) {
    if (!this.env.PHONE_KEY || !this.env.PC_KEY) {
      return json({ error: "The relay isn't set up yet. Run '3 - Set up away mode' on your PC." }, 503);
    }
    const who = this.auth(request);
    if (who === "locked") return json({ error: "Too many wrong keys. Try again in 10 minutes." }, 429);
    if (!who) return json({ error: "Wrong or missing key." }, 401);
    const url = new URL(request.url);
    try {
      await this.housekeeping(url.origin);
      if (who === "pc") return await this.pcRoutes(request, url);
      return await this.phoneRoutes(request, url);
    } catch (err) {
      return json({ error: String((err && err.message) || err) }, 500);
    }
  }

  // ------------------------------------------------------------ phone side
  async phoneRoutes(request, url) {
    const p = url.pathname, m = request.method;
    let g;
    if (p === "/api/ask" && m === "POST") return this.ask(request, url);
    if (p === "/api/status" && m === "GET") return this.status();
    if (p === "/api/jobs" && m === "GET") return this.list(url);
    if (p === "/api/latest.txt" && m === "GET") return this.latestText();
    if (p === "/api/test-notify" && m === "POST") {
      const sent = await this.push({
        title: "GlassesClaude test", message: "Notifications work. You'll get one like this when Claude is done.",
        tags: ["bell"], click: url.origin + "/",
      });
      return json({ sent });
    }
    if ((g = p.match(/^\/api\/jobs\/([0-9a-f]{16})$/)) && m === "GET") {
      const r = this.row(g[1]);
      return r ? json({ job: this.publicJob(r) }) : json({ error: "Unknown request" }, 404);
    }
    if ((g = p.match(/^\/api\/jobs\/([0-9a-f]{16})\/audio\/(ack|answer)$/)) && m === "GET") {
      return this.audioOut(g[1], g[2]);
    }
    if ((g = p.match(/^\/api\/jobs\/([0-9a-f]{16})\/cancel$/)) && m === "POST") return this.cancel(g[1]);
    return json({ error: "Not found" }, 404);
  }

  async ask(request, url) {
    const type = (request.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    const now = Date.now();
    const id = newId();
    const source = url.searchParams.get("source") === "shortcut" ? "shortcut" : "app";
    if (type === "application/json") {
      let body;
      try { body = await request.json(); } catch { return json({ error: "Bad JSON" }, 400); }
      const said = cleanString(body && body.text, 8000);
      if (!said || !said.trim()) return json({ error: "Empty request" }, 400);
      this.sql.exec("INSERT INTO jobs (id, created, updated, status, kind, source, text) VALUES (?, ?, ?, 'queued', 'text', ?, ?)",
        id, now, now, source, said.trim());
    } else if (type.startsWith("audio/") || type.startsWith("video/") || type === "application/octet-stream") {
      const buf = await request.arrayBuffer();
      if (!buf.byteLength) return json({ error: "Empty recording" }, 400);
      if (buf.byteLength > MAX_AUDIO) return json({ error: "Recording too long. Keep it under about a minute." }, 413);
      this.sql.exec("INSERT INTO jobs (id, created, updated, status, kind, source, audio, audio_type) VALUES (?, ?, ?, 'queued', 'audio', ?, ?, ?)",
        id, now, now, source, buf, type);
    } else {
      return json({ error: "Send audio or JSON {\"text\": \"...\"}" }, 415);
    }
    const online = this.pcOnline();
    const say = online ? "Sent to your PC. I'll notify you when it's done."
                       : "Saved. Your PC is offline right now, so it will run when the PC is back.";
    if (url.searchParams.get("format") === "text") return text(say);
    return json({ job: this.publicJob(this.row(id)), pc_online: online, say });
  }

  pcLastSeen() {
    return Math.max(Number(this.meta("pc_last_seen") || 0), this.lastSeenWrite || 0);
  }

  pcOnline() {
    return Date.now() - this.pcLastSeen() < PC_ONLINE_MS;
  }

  status() {
    const last = this.pcLastSeen();
    const queued = this.sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued'").one().n;
    const running = this.sql.exec(
      "SELECT id FROM jobs WHERE status IN ('sent','heard','working') ORDER BY created DESC LIMIT 1").toArray()[0];
    return json({
      pc_online: this.pcOnline(), pc_last_seen: last || null, queued,
      running: running ? this.publicJob(this.row(running.id)) : null,
      ntfy_topic: this.env.NTFY_TOPIC || null,
      queue_max_minutes: this.queueMaxMinutes(),
    });
  }

  list(url) {
    const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "10", 10) || 10, 1), 50);
    const ids = this.sql.exec("SELECT id FROM jobs ORDER BY created DESC LIMIT ?", limit).toArray();
    return json({ jobs: ids.map((r) => this.publicJob(this.row(r.id))) });
  }

  latestText() {
    const running = this.sql.exec(
      "SELECT id FROM jobs WHERE status IN ('queued','sent','heard','working') ORDER BY created DESC LIMIT 1").toArray()[0];
    const done = this.sql.exec(
      "SELECT id FROM jobs WHERE status IN ('done','error','cancelled','expired') ORDER BY updated DESC LIMIT 1").toArray()[0];
    const parts = [];
    if (running) {
      const r = this.publicJob(this.row(running.id));
      parts.push(r.said ? `Claude is still working on: ${r.said.slice(0, 120)}.` : "Claude is still working on your last request.");
    }
    if (done) {
      const r = this.publicJob(this.row(done.id));
      const answer = r.spoken || r.reply || "No answer text.";
      parts.push(r.said ? `For ${r.said.slice(0, 100)}: ${answer}` : answer);
    }
    if (!parts.length) parts.push("There are no answers yet.");
    if (!this.pcOnline()) parts.push("Your PC seems to be offline.");
    return text(parts.join(" "));
  }

  audioOut(id, which) {
    const col = which === "ack" ? "ack" : "answer";
    const r = this.sql.exec(`SELECT ${col} AS data, ${col}_type AS type FROM jobs WHERE id = ?`, id).toArray()[0];
    if (!r || !r.data) return json({ error: "No audio" }, 404);
    return new Response(r.data, { headers: { "content-type": r.type || "audio/mpeg", "cache-control": "no-store" } });
  }

  cancel(id) {
    const r = this.row(id);
    if (!r) return json({ error: "Unknown request" }, 404);
    if (FINAL.includes(r.status)) return json({ job: this.publicJob(r), stopped: false });
    const now = Date.now();
    if (r.status === "queued") {
      this.sql.exec("UPDATE jobs SET status = 'cancelled', reply = 'Cancelled before your PC started it.', ok = 0, updated = ? WHERE id = ?", now, id);
    } else {
      this.sql.exec("UPDATE jobs SET cancel_requested = 1, updated = ? WHERE id = ?", now, id);
    }
    return json({ job: this.publicJob(this.row(id)), stopped: true });
  }

  // --------------------------------------------------------------- PC side
  async pcRoutes(request, url) {
    const p = url.pathname, m = request.method;
    let g;
    if (p === "/api/pc/next" && m === "GET") return this.next();
    if ((g = p.match(/^\/api\/pc\/jobs\/([0-9a-f]{16})\/audio$/)) && m === "GET") {
      const r = this.sql.exec("SELECT audio, audio_type FROM jobs WHERE id = ?", g[1]).toArray()[0];
      if (!r || !r.audio) return json({ error: "No audio" }, 404);
      return new Response(r.audio, { headers: { "content-type": r.audio_type || "application/octet-stream" } });
    }
    if ((g = p.match(/^\/api\/pc\/jobs\/([0-9a-f]{16})\/audio\/(ack|answer)$/)) && m === "PUT") {
      const buf = await request.arrayBuffer();
      if (!buf.byteLength || buf.byteLength > MAX_AUDIO) return json({ error: "Bad audio size" }, 413);
      const type = (request.headers.get("content-type") || "audio/mpeg").split(";")[0].trim();
      const col = g[2] === "ack" ? "ack" : "answer";
      this.sql.exec(`UPDATE jobs SET ${col} = ?, ${col}_type = ?, updated = ? WHERE id = ?`, buf, type, Date.now(), g[1]);
      return json({ ok: true });
    }
    if ((g = p.match(/^\/api\/pc\/jobs\/([0-9a-f]{16})\/update$/)) && m === "POST") return this.update(request, url, g[1]);
    if ((g = p.match(/^\/api\/pc\/jobs\/([0-9a-f]{16})\/notify$/)) && m === "POST") {
      // The PC couldn't reach ntfy.sh itself: send the notification from here instead.
      let body = {};
      try { body = await request.json(); } catch { body = {}; }
      const r = this.row(g[1]);
      if (!r) return json({ error: "Unknown request" }, 404);
      this.sql.exec("UPDATE jobs SET notified = 0 WHERE id = ?", g[1]);
      return json({ sent: await this.notifyDone(this.row(g[1]), url.origin, body.include_answer) });
    }
    return json({ error: "Not found" }, 404);
  }

  next() {
    const now = Date.now();
    // Remember when the PC last checked in (written at most every 15 s to save free-plan writes).
    if (!this.lastSeenWrite || now - this.lastSeenWrite > 15_000) {
      this.setMeta("pc_last_seen", now);
      this.lastSeenWrite = now;
    }
    const r = this.sql.exec(
      "SELECT id, kind, text, audio_type, length(audio) AS size FROM jobs WHERE status = 'queued' ORDER BY created LIMIT 1").toArray()[0];
    let job = null;
    if (r) {
      this.sql.exec("UPDATE jobs SET status = 'sent', updated = ? WHERE id = ?", now, r.id);
      job = { id: r.id, kind: r.kind, text: r.text || "", audio_type: r.audio_type || "", audio_size: r.size || 0 };
    }
    const cancel = this.sql.exec(
      "SELECT id FROM jobs WHERE cancel_requested = 1 AND status IN ('sent','heard','working')").toArray().map((x) => x.id);
    return json({ job, cancel, now });
  }

  async update(request, url, id) {
    const r = this.row(id);
    if (!r) return json({ error: "Unknown request" }, 404);
    let body;
    try { body = await request.json(); } catch { return json({ error: "Bad JSON" }, 400); }
    const status = String(body.status || "");
    if (!["heard", "working", "done", "error", "cancelled"].includes(status)) return json({ error: "Bad status" }, 400);
    if (FINAL.includes(r.status)) return json({ ok: true, ignored: true });   // already finished
    const ok = body.ok === undefined || body.ok === null ? null : (body.ok ? 1 : 0);
    this.sql.exec(
      `UPDATE jobs SET status = ?, transcript = COALESCE(?, transcript), reply = COALESCE(?, reply),
              spoken = COALESCE(?, spoken), ok = COALESCE(?, ok), updated = ? WHERE id = ?`,
      status, cleanString(body.transcript), cleanString(body.reply), cleanString(body.spoken, 4000), ok, Date.now(), id);
    let notified = false;
    if (FINAL.includes(status)) {
      if (body.notify === "pc") this.sql.exec("UPDATE jobs SET notified = 1 WHERE id = ?", id);  // PC sends it
      else notified = await this.notifyDone(this.row(id), url.origin);
    }
    return json({ ok: true, notified });
  }

  // ------------------------------------------------------------- housekeeping
  queueMaxMinutes() {
    const n = parseInt(this.env.QUEUE_MAX_MINUTES || "120", 10);
    return Number.isFinite(n) && n > 0 ? n : 120;
  }

  async housekeeping(origin) {
    const now = Date.now();
    // Runs at most once a minute. Your PC checks in every few seconds, and every row
    // these queries look at counts toward the free plan's 5 million "rows read" a day.
    if (this.lastHousekeeping && now - this.lastHousekeeping < 60_000) return;
    this.lastHousekeeping = now;
    // Requests that waited too long (PC was off) are not run later by surprise.
    const stale = this.sql.exec(
      "SELECT id FROM jobs WHERE status = 'queued' AND created < ?", now - this.queueMaxMinutes() * 60_000).toArray();
    for (const s of stale) {
      this.sql.exec(
        "UPDATE jobs SET status = 'expired', ok = 0, reply = 'Not run: your PC was offline for too long. Ask again when it is on.', updated = ? WHERE id = ?",
        now, s.id);
      await this.notifyDone(this.row(s.id), origin);
    }
    // Notifications that failed (ntfy.sh unreachable) get up to 3 tries in the first 30 minutes.
    const retry = this.sql.exec(
      `SELECT id FROM jobs WHERE notified = 0 AND notify_tries BETWEEN 1 AND 2
         AND status IN ('done','error','cancelled','expired') AND updated > ? LIMIT 3`, now - 1_800_000).toArray();
    for (const r of retry) await this.notifyDone(this.row(r.id), origin);
    // Once an hour, forget anything older than KEEP_DAYS.
    const last = Number(this.meta("last_cleanup") || 0);
    if (now - last > 3_600_000) {
      this.sql.exec("DELETE FROM jobs WHERE created < ?", now - KEEP_DAYS * 86_400_000);
      this.setMeta("last_cleanup", now);
    }
  }

  // --------------------------------------------------------------- ntfy push
  async notifyDone(r, origin, includeAnswer) {
    if (!r || r.notified) return false;
    this.sql.exec("UPDATE jobs SET notified = 1, notify_tries = notify_tries + 1 WHERE id = ?", r.id);
    const job = this.publicJob(r);
    const titles = { done: "Claude finished", error: "Claude hit a problem", cancelled: "Claude stopped", expired: "Not run: PC offline" };
    const said = (job.said || "your request").slice(0, 70);
    const withAnswer = typeof includeAnswer === "boolean" ? includeAnswer
      : (this.env.NTFY_INCLUDE_ANSWER || "yes").toLowerCase() !== "no";
    const message = withAnswer ? (job.spoken || job.reply || "Done.").slice(0, 900) : "Tap to hear the answer.";
    const sent = await this.push({
      title: `${titles[job.status] || "Claude"}: ${said}`.slice(0, 120),
      message,
      tags: [job.status === "done" ? "white_check_mark" : "warning"],
      priority: 4,
      click: `${origin}/?job=${job.id}`,
    });
    if (!sent) this.sql.exec("UPDATE jobs SET notified = 0 WHERE id = ?", r.id);   // try again later
    return sent;
  }

  async push(msg) {
    const topic = this.env.NTFY_TOPIC;
    if (!topic) return false;
    try {
      const res = await fetch("https://ntfy.sh/", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ topic, ...msg }),
      });
      return res.ok;
    } catch {
      return false;
    }
  }
}
