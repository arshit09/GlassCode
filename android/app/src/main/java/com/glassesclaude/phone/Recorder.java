package com.glassesclaude.phone;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.media.MediaRecorder;
import android.media.ToneGenerator;
import android.os.Build;
import android.os.SystemClock;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Records one request.
 *
 *  1. Switches the phone's mic to the Bluetooth glasses (if they're connected).
 *  2. Beeps in the glasses: "talk now".
 *  3. Records AAC audio (small files) until you pause for 1.6 seconds.
 *  4. Beeps again ("sent") and switches the audio back to normal.
 */
final class Recorder {

    /** What came out of one recording. */
    static final class Result {
        File file;              // the .m4a recording
        boolean heardSpeech;    // false = you didn't say anything, nothing to send
        boolean viaBluetooth;   // true = the glasses' mic was used
    }

    // How "stop when I pause" behaves (same numbers as the web app).
    private static final long SILENCE_MS = 1600;     // this much quiet after speech ends the recording
    private static final long NO_SPEECH_MS = 6000;   // nothing said this long: give up
    private static final long MAX_MS = 60_000;       // longest recording (keeps files far under the 1.9 MB limit)

    private final Context ctx;
    private final AudioManager am;
    private volatile boolean stopNow;     // a second tap: stop and send what we have
    private volatile boolean cancelNow;   // stop and throw it away

    Recorder(Context ctx) {
        this.ctx = ctx;
        this.am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
    }

    /** Second tap on the glasses: finish now and send. */
    void stop() { stopNow = true; }

    /** Throw the recording away. */
    void cancel() { cancelNow = true; stopNow = true; }

    /** Records into `out`. Runs on a background thread and blocks until you stop talking. */
    @SuppressWarnings("deprecation")   // the no-argument MediaRecorder() is only used before Android 12
    Result record(File out) throws IOException {
        stopNow = false;
        cancelNow = false;
        Result res = new Result();
        res.file = out;

        // 1. Use the glasses' microphone when they're connected (Bluetooth "hands-free" link).
        res.viaBluetooth = routeToBluetooth();
        AudioDeviceInfo btMic = res.viaBluetooth ? findInput() : null;

        // 2. "Talk now" beep. During a Bluetooth call-style link, the voice-call stream reaches the glasses.
        beep(res.viaBluetooth, ToneGenerator.TONE_PROP_BEEP);

        MediaRecorder rec = Build.VERSION.SDK_INT >= 31 ? new MediaRecorder(ctx) : new MediaRecorder();
        boolean started = false;
        try {
            // VOICE_COMMUNICATION follows the Bluetooth route and cleans up background noise.
            rec.setAudioSource(res.viaBluetooth ? MediaRecorder.AudioSource.VOICE_COMMUNICATION
                                                : MediaRecorder.AudioSource.MIC);
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);   // .m4a, "audio/mp4"
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
            rec.setAudioChannels(1);
            rec.setAudioSamplingRate(16_000);                         // speech only needs 16 kHz
            rec.setAudioEncodingBitRate(32_000);                      // about 4 KB per second
            rec.setOutputFile(out.getAbsolutePath());
            if (btMic != null && Build.VERSION.SDK_INT >= 28) rec.setPreferredDevice(btMic);
            rec.prepare();
            rec.start();
            started = true;

            // 3. Listen to the loudness every 50 ms and stop when you pause.
            res.heardSpeech = waitForPause(rec);
        } finally {
            if (started) {
                try { rec.stop(); } catch (RuntimeException e) { res.heardSpeech = false; }  // stop() fails if nothing was recorded
            }
            rec.release();
            // 4. "Sent" beep (a different tone), then give the audio back to music/answers.
            if (res.heardSpeech && !cancelNow) beep(res.viaBluetooth, ToneGenerator.TONE_PROP_ACK);
            restoreRouting();
        }
        if (cancelNow) res.heardSpeech = false;
        return res;
    }

    /**
     * Returns true when speech was heard and then a pause (or a second tap) ended it.
     * Returns false when nothing was said within 6 seconds.
     */
    private boolean waitForPause(MediaRecorder rec) {
        long t0 = SystemClock.elapsedRealtime();
        List<Float> calibration = new ArrayList<>();
        float threshold = -1;       // set after the first 0.35 s
        int loudInARow = 0;
        boolean speaking = false;
        long lastLoud = 0;

        while (true) {
            SystemClock.sleep(50);
            long t = SystemClock.elapsedRealtime() - t0;
            float level = rec.getMaxAmplitude() / 32767f;          // loudest peak since the last check, 0..1

            if (stopNow) return speaking || t > 800;               // a tap means "send it" (if you said anything)
            if (t > MAX_MS) return true;

            // The first 0.35 s measure the room, so a noisy street needs a louder voice than a quiet room.
            if (t < 350) { calibration.add(level); continue; }
            if (threshold < 0) {
                Collections.sort(calibration);
                float floor = calibration.isEmpty() ? 0 : calibration.get(calibration.size() / 2);
                threshold = Math.max(0.035f, Math.min(0.25f, floor * 2.5f));
            }

            if (level > threshold) {
                loudInARow++;
                lastLoud = t;
                if (loudInARow >= 3) speaking = true;              // 150 ms of voice counts as speech
            } else {
                loudInARow = 0;
            }

            if (!speaking && t > NO_SPEECH_MS) return false;       // nothing said
            if (speaking && t - lastLoud > SILENCE_MS) return true; // you paused: done
        }
    }

    // ------------------------------------------------------------ Bluetooth mic routing

    /** Switches the mic (and call-style audio) to the Bluetooth glasses. True if that worked. */
    private boolean routeToBluetooth() {
        try {
            am.setMode(AudioManager.MODE_IN_COMMUNICATION);
            if (Build.VERSION.SDK_INT >= 31) {
                AudioDeviceInfo target = null;
                for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
                    int type = d.getType();
                    if (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLE_HEADSET) {
                        target = d;
                        break;
                    }
                }
                if (target == null || !am.setCommunicationDevice(target)) {
                    am.setMode(AudioManager.MODE_NORMAL);
                    return false;                                 // no glasses connected: use the phone's mic
                }
                // The switch happens in the background; wait until it's done (at most 2.5 s).
                for (int i = 0; i < 25; i++) {
                    AudioDeviceInfo now = am.getCommunicationDevice();
                    if (now != null && now.getId() == target.getId()) break;
                    SystemClock.sleep(100);
                }
            } else {
                if (!am.isBluetoothScoAvailableOffCall()) {
                    am.setMode(AudioManager.MODE_NORMAL);
                    return false;
                }
                if (!startScoAndWait()) {
                    am.setMode(AudioManager.MODE_NORMAL);
                    return false;
                }
            }
            SystemClock.sleep(300);   // let the Bluetooth link settle so your first word isn't cut off
            return true;
        } catch (RuntimeException e) {
            restoreRouting();
            return false;
        }
    }

    /** Older Android (before 12): start the Bluetooth voice link and wait up to 3 s for it. */
    @SuppressWarnings("deprecation")
    private boolean startScoAndWait() {
        CountDownLatch connected = new CountDownLatch(1);
        BroadcastReceiver r = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) {
                int state = i.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE, -1);
                if (state == AudioManager.SCO_AUDIO_STATE_CONNECTED) connected.countDown();
            }
        };
        ctx.registerReceiver(r, new IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED));
        try {
            am.startBluetoothSco();
            am.setBluetoothScoOn(true);
            return connected.await(3, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            return false;
        } finally {
            ctx.unregisterReceiver(r);
        }
    }

    /** Puts audio back to normal so answers play in good quality (stereo music profile). */
    @SuppressWarnings("deprecation")
    private void restoreRouting() {
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                am.clearCommunicationDevice();
            } else {
                am.setBluetoothScoOn(false);
                am.stopBluetoothSco();
            }
            am.setMode(AudioManager.MODE_NORMAL);
        } catch (RuntimeException ignored) { }
    }

    /** The glasses' microphone as an input device, so the recorder prefers it. */
    private AudioDeviceInfo findInput() {
        for (AudioDeviceInfo d : am.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
            int type = d.getType();
            if (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) return d;
            if (Build.VERSION.SDK_INT >= 31 && type == AudioDeviceInfo.TYPE_BLE_HEADSET) return d;
        }
        return null;
    }

    /** A short beep in the glasses (or the phone speaker when no glasses are connected). */
    private static void beep(boolean viaBluetooth, int tone) {
        try {
            ToneGenerator tg = new ToneGenerator(viaBluetooth ? AudioManager.STREAM_VOICE_CALL
                                                              : AudioManager.STREAM_MUSIC, 85);
            tg.startTone(tone, 180);
            SystemClock.sleep(230);
            tg.release();
        } catch (RuntimeException ignored) { }   // a missing beep isn't worth failing over
    }
}
