package com.glassesclaude.phone;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

/**
 * Remembers your relay address and phone key, private to this app.
 *
 * Both come from the "phone link" that away-mode setup made on your PC:
 *   https://glassesclaude-relay.<you>.workers.dev/#key=<phone key>
 */
final class Prefs {
    private final SharedPreferences sp;

    Prefs(Context context) {
        // MODE_PRIVATE: no other app can read this file.
        sp = context.getSharedPreferences("glassesclaude", Context.MODE_PRIVATE);
    }

    /** e.g. "https://glassesclaude-relay.you.workers.dev" (no slash at the end). */
    String relay() { return sp.getString("relay", ""); }

    /** The phone key, the part after "#key=" in the link. */
    String key() { return sp.getString("key", ""); }

    /** True once a link has been saved. */
    boolean ready() { return !relay().isEmpty() && !key().isEmpty(); }

    /**
     * Saves the whole phone link.
     * Returns null when it worked, or a short message saying what's wrong.
     */
    String saveLink(String text) {
        if (text == null || text.trim().isEmpty()) return "Paste the phone link first.";
        text = text.trim();

        // The key comes right after "key=" and is made of letters, digits, "-" and "_".
        int at = text.indexOf("key=");
        if (!text.startsWith("https://") || at < 0) {
            return "That isn't the phone link. It starts with https:// and has key= in it.";
        }
        String key = text.substring(at + 4).replaceAll("[^A-Za-z0-9_\\-].*$", "");

        // The relay address is just the https:// part plus the host name.
        String host = Uri.parse(text).getHost();
        if (host == null || host.isEmpty() || key.length() < 16) {
            return "That link looks cut off. Copy it again.";
        }
        sp.edit().putString("relay", "https://" + host).putString("key", key).apply();
        return null;
    }
}
