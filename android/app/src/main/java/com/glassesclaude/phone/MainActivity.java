package com.glassesclaude.phone;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.util.TypedValue;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * The app screen. You only need it once: paste your phone link, tap Start, allow the
 * permissions. After that the background service does everything and the phone can stay
 * in your pocket.
 */
public class MainActivity extends Activity implements GlassesService.UiListener {

    private static final int PERMISSIONS = 7;

    private Prefs prefs;
    private TextView statusView, buttonView, relayView, pcView;
    private EditText linkField;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = new Prefs(this);

        // A simple scrolling column, built in code (no layout files to keep in sync).
        ScrollView scroll = new ScrollView(this);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        col.setPadding(pad, pad, pad, pad);
        scroll.addView(col);
        setContentView(scroll);

        col.addView(text("GlassesClaude", 26, true));
        statusView = text("", 17, false);
        col.addView(statusView);
        buttonView = text("", 14, false);
        col.addView(buttonView);

        col.addView(heading("1. Connect to your relay"));
        col.addView(text("Copy your phone link (in the GlassesClaude web app: Settings > Copy phone link), "
                + "then tap Paste and save.", 15, false));
        linkField = new EditText(this);
        linkField.setHint("https://...workers.dev/#key=...");
        linkField.setSingleLine(true);
        linkField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        col.addView(linkField);
        col.addView(button("Paste and save", v -> pasteAndSave()));
        relayView = text("", 14, false);
        col.addView(relayView);

        col.addView(heading("2. Start listening for your glasses"));
        col.addView(text("Allow the microphone, notifications and nearby devices when your phone asks.", 15, false));
        col.addView(button("Start", v -> startWithPermissions()));

        col.addView(heading("3. Keep it running in your pocket"));
        col.addView(text("Tap below and choose Allow. On Samsung also set Settings > Apps > GlassesClaude > "
                + "Battery to Unrestricted.", 15, false));
        col.addView(button("Allow running in the background", v -> askBattery()));

        col.addView(heading("Try it"));
        col.addView(text("Tap your glasses once, talk, then pause. Tap again to send early, or while an answer "
                + "plays to cut it off. The line under the status shows the last button your glasses sent.", 15, false));
        col.addView(button("Talk now (test without the glasses)", v -> sendToService(GlassesService.ACTION_TALK)));
        col.addView(button("Check my PC", v -> checkPc()));
        pcView = text("", 14, false);
        col.addView(pcView);
        col.addView(button("Stop GlassesClaude", v -> sendToService(GlassesService.ACTION_QUIT)));

        refresh();
    }

    @Override
    protected void onResume() {
        super.onResume();
        GlassesService.ui = this;
        // Opening the app takes the glasses' tap back if another music app grabbed it.
        if (GlassesService.running) sendToService(GlassesService.ACTION_CLAIM);
        refresh();
    }

    @Override
    protected void onPause() {
        GlassesService.ui = null;
        super.onPause();
    }

    /** Called by the service whenever its status changes. */
    @Override
    public void onStatusChanged() { refresh(); }

    private void refresh() {
        statusView.setText(GlassesService.running ? GlassesService.status : "Not running yet. Tap Start.");
        buttonView.setText("Last button from your glasses: " + GlassesService.lastButton);
        relayView.setText(prefs.ready() ? "Connected to " + prefs.relay() : "No phone link saved yet.");
    }

    // ------------------------------------------------------------------ step 1

    private void pasteAndSave() {
        String text = linkField.getText().toString().trim();
        if (text.isEmpty()) {
            // Nothing typed: take the link from the clipboard.
            ClipboardManager cm = getSystemService(ClipboardManager.class);
            ClipData clip = cm == null ? null : cm.getPrimaryClip();
            if (clip != null && clip.getItemCount() > 0) {
                CharSequence c = clip.getItemAt(0).coerceToText(this);
                text = c == null ? "" : c.toString().trim();
            }
        }
        String problem = prefs.saveLink(text);
        if (problem != null) {
            relayView.setText(problem);
            return;
        }
        linkField.setText("");
        refresh();
        checkPc();
    }

    // ------------------------------------------------------------------ step 2

    private void startWithPermissions() {
        if (!prefs.ready()) {
            relayView.setText("Paste your phone link first, then tap Start.");
            return;
        }
        List<String> need = new ArrayList<>();
        need.add(Manifest.permission.RECORD_AUDIO);
        if (Build.VERSION.SDK_INT >= 33) need.add(Manifest.permission.POST_NOTIFICATIONS);
        if (Build.VERSION.SDK_INT >= 31) need.add(Manifest.permission.BLUETOOTH_CONNECT);
        List<String> missing = new ArrayList<>();
        for (String p : need) {
            if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) missing.add(p);
        }
        if (missing.isEmpty()) startService();
        else requestPermissions(missing.toArray(new String[0]), PERMISSIONS);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code != PERMISSIONS) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startService();
        } else {
            statusView.setText("GlassesClaude needs the microphone to hear you. Tap Start again and allow it.");
        }
    }

    private void startService() {
        // Started while this screen is open, so Android allows the mic in the background later.
        startForegroundService(new Intent(this, GlassesService.class));
        statusView.setText("Starting...");
    }

    private void sendToService(String action) {
        if (!GlassesService.running) {
            if (GlassesService.ACTION_TALK.equals(action)) statusView.setText("Tap Start first.");
            return;
        }
        startService(new Intent(this, GlassesService.class).setAction(action));
    }

    // ------------------------------------------------------------------ step 3

    private void askBattery() {
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) {
            pcView.setText("Already allowed to run in the background.");
            return;
        }
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException e) {
            // Some phones hide that screen: open the app's settings page instead.
            startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName())));
        }
    }

    // ------------------------------------------------------------------ check

    private void checkPc() {
        if (!prefs.ready()) {
            pcView.setText("Paste your phone link first.");
            return;
        }
        pcView.setText("Checking...");
        final Relay relay = new Relay(prefs.relay(), prefs.key());
        new Thread(() -> {
            String msg;
            try {
                JSONObject s = relay.status();
                msg = s.optBoolean("pc_online") ? "Your PC is online. Ready."
                        : "The relay works, but your PC isn't connected. Start \"2 - Start\" on the PC.";
            } catch (Exception e) {
                msg = "Couldn't reach the relay: " + e.getMessage();
            }
            final String m = msg;
            runOnUiThread(() -> pcView.setText(m));
        }).start();
    }

    // ------------------------------------------------------------------ tiny UI helpers

    private TextView text(String s, int sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setPadding(0, dp(4), 0, dp(4));
        return t;
    }

    private TextView heading(String s) {
        TextView t = text(s, 19, true);
        t.setPadding(0, dp(22), 0, dp(4));
        return t;
    }

    private Button button(String label, View.OnClickListener onClick) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setOnClickListener(onClick);
        return b;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
