package com.glassesclaude.phone;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * A tiny HTTPS client for your Cloudflare relay.
 * It uses the same phone routes as the GlassesClaude web app, with the phone key.
 */
final class Relay {
    private final String base;   // https://glassesclaude-relay.<you>.workers.dev
    private final String key;    // phone key, sent in the X-Key header

    Relay(String base, String key) {
        this.base = base;
        this.key = key;
    }

    /** Sends a recording: POST /api/ask. The reply has {job: {id, ...}, pc_online, say}. */
    JSONObject ask(File audio, String contentType) throws IOException, JSONException {
        HttpURLConnection c = open("/api/ask", "POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", contentType);
        c.setFixedLengthStreamingMode((int) audio.length());   // stream the file, don't buffer it
        try (OutputStream out = c.getOutputStream(); InputStream in = new FileInputStream(audio)) {
            byte[] buf = new byte[16 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        }
        return readJson(c);
    }

    /** Where a request is: GET /api/jobs/<id>. The reply has {job: {status, said, reply, spoken, has_ack, has_answer}}. */
    JSONObject job(String id) throws IOException, JSONException {
        return readJson(open("/api/jobs/" + id, "GET"));
    }

    /** Asks the PC to stop a request: POST /api/jobs/<id>/cancel. */
    void cancel(String id) throws IOException, JSONException {
        HttpURLConnection c = open("/api/jobs/" + id + "/cancel", "POST");
        c.setDoOutput(true);
        c.setFixedLengthStreamingMode(0);
        c.getOutputStream().close();
        readJson(c);
    }

    /** Is the PC connected? GET /api/status. The reply has {pc_online, queued, running, ...}. */
    JSONObject status() throws IOException, JSONException {
        return readJson(open("/api/status", "GET"));
    }

    /**
     * Downloads the "Got it" or answer audio: GET /api/jobs/<id>/audio/<ack|answer>.
     * Returns the saved file, or null if there's no audio for that request.
     */
    File downloadAudio(String id, String which, File to) throws IOException {
        HttpURLConnection c = open("/api/jobs/" + id + "/audio/" + which, "GET");
        int code = c.getResponseCode();
        if (code == 404) return null;                          // no audio (e.g. it was too long)
        if (code >= 400) throw new IOException("audio download failed (" + code + ")");
        try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(to)) {
            byte[] buf = new byte[16 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        }
        return to;
    }

    // ----------------------------------------------------------------- helpers

    private HttpURLConnection open(String path, String method) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(base + path).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(15_000);
        c.setReadTimeout(30_000);
        c.setRequestProperty("X-Key", key);                     // the relay checks this on every call
        c.setRequestProperty("User-Agent", "GlassesClaude-Android");
        return c;
    }

    /** Reads a JSON reply and turns relay errors into readable messages. */
    private static JSONObject readJson(HttpURLConnection c) throws IOException, JSONException {
        int code = c.getResponseCode();
        InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
        String body = in == null ? "" : readAll(in);
        if (code == 401) throw new IOException("wrong phone key: paste the phone link again");
        if (code == 429) throw new IOException("too many wrong keys: wait 10 minutes");
        if (code == 503) throw new IOException("the relay isn't set up yet");
        if (code >= 400) {
            String msg = body;
            try { msg = new JSONObject(body).optString("error", body); } catch (JSONException ignored) { }
            throw new IOException(msg.isEmpty() ? "relay error " + code : msg);
        }
        return new JSONObject(body);
    }

    private static String readAll(InputStream in) throws IOException {
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8 * 1024];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }
}
