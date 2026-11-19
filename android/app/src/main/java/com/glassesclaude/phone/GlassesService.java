package com.glassesclaude.phone;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.media.MediaPlayer;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.speech.tts.TextToSpeech;
import android.view.KeyEvent;

import org.json.JSONObject;

import java.io.File;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * The part of the app that keeps running with your phone in your pocket.
 *
 * Tap the glasses once  -> they send "play/pause" over Bluetooth -> this service hears it,
 * records your voice through the glasses' mic, sends it to your relay, follows the request,
 * and plays "Got it" and then Claude's answer back in the glasses.
 *
 * Tap again while it's listening to send early. Tap while an answer plays to cut it off and talk.
 */
public class GlassesService extends Service {

    // Commands the app screen and the notification buttons send to this service.
    static final String ACTION_TALK = "com.glassesclaude.phone.TALK";
    static final String ACTION_STOP = "com.glassesclaude.phone.STOP";
    static final String ACTION_REPEAT = "com.glassesclaude.phone.REPEAT";
    static final String ACTION_CLAIM = "com.glassesclaude.phone.CLAIM";
    static final String ACTION_QUIT = "com.glassesclaude.phone.QUIT";

    /** The app screen listens for status changes through this (only while it's open). */
    interface UiListener { void onStatusChanged(); }

    static volatile UiListener ui;
    static volatile boolean running;
    static volatile String status = "Not started";
    static volatile String lastButton = "none yet";

    private static final int NOTE_ID = 1;
    private static final String CHANNEL = "status";
    private static final List<String> FINAL = Arrays.asList("done", "error", "cancelled", "expired");
    private static final long GIVE_UP_MS = 130 * 60_000L;   // the relay gives up on queued jobs after 2 hours

    /** One request we're following on the relay. */
    private static final class Job {
        final String id;
        final long started = SystemClock.elapsedRealtime();
        String said = "";
        boolean ackPlayed, workingShown;
        int errors;
        Job(String id) { this.id = id; }
    }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();            // record + send
    private final ScheduledExecutorService poller = Executors.newSingleThreadScheduledExecutor(); // follow jobs
    private final Map<String, Job> active = new ConcurrentHashMap<>();
    private final ArrayDeque<File> playQueue = new ArrayDeque<>();   // used on the main thread only

    private Prefs prefs;
    private Recorder recorder;
    private AudioManager am;
    private MediaSession session;
    private MediaPlayer player;
    private AudioFocusRequest focus;
    private TextToSpeech tts;
    private boolean ttsReady;
    private PowerManager.WakeLock wake;
    private volatile boolean recording;
    private volatile boolean polling;
    private volatile File lastAnswer;
    private volatile String lastSpoken = "";
    private volatile String lastJobId;
    private long lastButtonAt;

    // =================================================================== lifecycle

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = new Prefs(this);
        recorder = new Recorder(this);
        am = (AudioManager) getSystemService(AUDIO_SERVICE);

        // 1. Show the ongoing notification and become a "foreground service", which Android
        //    lets keep running and use the microphone with the screen off.
        createChannel();
        try {
            int types = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
            if (Build.VERSION.SDK_INT >= 30) types |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            if (Build.VERSION.SDK_INT >= 29) startForeground(NOTE_ID, buildNote("Tap your glasses to talk."), types);
            else startForeground(NOTE_ID, buildNote("Tap your glasses to talk."));
        } catch (RuntimeException e) {
            // Android refuses when this wasn't started from the open app (or the mic isn't allowed).
            running = false;
            stopSelf();
            return;
        }
        running = true;

        // 2. A media session: this is what receives the glasses' play/pause tap.
        session = new MediaSession(this, "GlassesClaude");
        session.setCallback(new MediaSession.Callback() {
            @Override public boolean onMediaButtonEvent(Intent intent) {
                KeyEvent ev = Build.VERSION.SDK_INT >= 33
                        ? intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent.class)
                        : legacyKeyEvent(intent);
                if (ev == null) return super.onMediaButtonEvent(intent);
                // React once per press: on "key down", and ignore auto-repeat while it's held.
                if (ev.getAction() == KeyEvent.ACTION_DOWN && ev.getRepeatCount() == 0) {
                    onKey(ev.getKeyCode());
                }
                return true;
            }
            // Some phones turn Bluetooth buttons into these calls instead of key events.
            @Override public void onPlay() { onButton("play", "talk"); }
            @Override public void onPause() { onButton("pause", "talk"); }
            @Override public void onSkipToNext() { onButton("next", "repeat"); }
            @Override public void onSkipToPrevious() { onButton("previous", "stop"); }
        }, main);
        setPlaybackState(PlaybackState.STATE_PAUSED);
        session.setActive(true);

        // 3. A phone voice for short messages ("I didn't hear anything", errors, answers without audio).
        tts = new TextToSpeech(this, s -> {
            ttsReady = s == TextToSpeech.SUCCESS;
            if (ttsReady && tts != null) tts.setAudioAttributes(mediaAttributes());
        });

        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GlassesClaude:work");
        wake.setReferenceCounted(false);

        cleanCache();
        claimMediaButtons();
        setStatus(prefs.ready() ? "Ready: tap your glasses to talk." : "Paste your phone link in the app first.");
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!running) return START_NOT_STICKY;
        String action = intent == null ? null : intent.getAction();
        if (ACTION_TALK.equals(action)) talkToggle();
        else if (ACTION_STOP.equals(action)) stopCurrent();
        else if (ACTION_REPEAT.equals(action)) repeatLast();
        else if (ACTION_CLAIM.equals(action)) claimMediaButtons();
        else if (ACTION_QUIT.equals(action)) { stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); }
        // Not sticky: Android may not restart a microphone service in the background anyway.
        // Open the app once after restarting your phone.
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        status = "Stopped. Open the app and tap Start.";
        notifyUi();
        recorder.cancel();
        stopPlayback();
        if (session != null) session.release();
        if (tts != null) tts.shutdown();
        worker.shutdownNow();
        poller.shutdownNow();
        if (wake != null && wake.isHeld()) wake.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    // =================================================================== buttons

    @SuppressWarnings("deprecation")
    private static KeyEvent legacyKeyEvent(Intent intent) {
        return intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT);
    }

    /** Maps the Bluetooth key the glasses sent to what it should do. */
    private void onKey(int code) {
        switch (code) {
            case KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE: onButton("play/pause", "talk"); break;
            case KeyEvent.KEYCODE_MEDIA_PLAY:       onButton("play", "talk"); break;
            case KeyEvent.KEYCODE_MEDIA_PAUSE:      onButton("pause", "talk"); break;
            case KeyEvent.KEYCODE_HEADSETHOOK:      onButton("headset button", "talk"); break;
            case KeyEvent.KEYCODE_MEDIA_NEXT:       onButton("next", "repeat"); break;
            case KeyEvent.KEYCODE_MEDIA_PREVIOUS:   onButton("previous", "stop"); break;
            case KeyEvent.KEYCODE_MEDIA_STOP:       onButton("stop", "stop"); break;
            default:                                onButton("key " + code, "ignore");
        }
    }

    /** One button press. Presses closer than 0.7 s are treated as one (some phones send two signals). */
    private void onButton(String name, String what) {
        long now = SystemClock.elapsedRealtime();
        lastButton = name + " (" + what + ")";
        notifyUi();
        if (now - lastButtonAt < 700) return;
        lastButtonAt = now;
        if ("talk".equals(what)) talkToggle();
        else if ("repeat".equals(what)) repeatLast();
        else if ("stop".equals(what)) stopCurrent();
    }

    /** First tap: listen. Tap while listening: send now. Tap while an answer plays: stop it and listen. */
    private void talkToggle() {
        if (recording) { recorder.stop(); return; }
        stopPlayback();
        if (tts != null) tts.stop();
        if (!prefs.ready()) { say("Open the GlassesClaude app and paste your phone link first."); return; }
        startRecording();
    }

    // =================================================================== record and send

    private void startRecording() {
        recording = true;
        wake.acquire(15 * 60_000L);
        setPlaybackState(PlaybackState.STATE_PLAYING);
        setStatus("Listening... talk, then pause.");
        worker.execute(() -> {
            File file = new File(audioDir(), "rec-" + System.currentTimeMillis() + ".m4a");
            Recorder.Result r;
            try {
                r = recorder.record(file);
            } catch (Exception e) {
                done("Couldn't use the microphone: " + e.getMessage(), "I couldn't use the microphone.");
                file.delete();
                return;
            }
            recording = false;
            setPlaybackState(PlaybackState.STATE_PAUSED);
            claimMediaButtons();
            main.post(this::playNext);          // play anything that arrived while we were listening

            if (!r.heardSpeech) {
                file.delete();
                done("Didn't hear anything. Tap your glasses to try again.", "I didn't hear anything.");
                return;
            }
            setStatus("Sending" + (r.viaBluetooth ? "" : " (recorded with the phone's mic)") + "...");
            try {
                JSONObject res = relay().ask(file, "audio/mp4");
                String id = res.getJSONObject("job").getString("id");
                active.put(id, new Job(id));
                lastJobId = id;
                if (!res.optBoolean("pc_online", true)) {
                    // The PC isn't connected: it will run when the PC is back (or expire after 2 hours).
                    String say = res.optString("say", "");
                    say(say.isEmpty() ? "Your PC isn't connected right now. I'll run it when it's back." : say);
                    setStatus("Sent. Waiting for your PC to come online...");
                } else {
                    setStatus("Sent. Waiting for your PC...");
                }
                startPolling();
            } catch (Exception e) {
                done("Couldn't send: " + e.getMessage(), "I couldn't reach your relay.");
            } finally {
                file.delete();
            }
        });
    }

    /** Ends a failed attempt: status text, a spoken message, and let the phone sleep again. */
    private void done(String statusText, String spoken) {
        recording = false;
        setPlaybackState(PlaybackState.STATE_PAUSED);
        setStatus(statusText);
        say(spoken);
        releaseWakeIfIdle();
    }

    /** "Stop": throw away the current recording, or ask the PC to stop the latest request. */
    private void stopCurrent() {
        if (recording) { recorder.cancel(); setStatus("Cancelled."); return; }
        stopPlayback();
        final String id = lastJobId;
        if (id == null || !active.containsKey(id)) { say("Nothing to stop."); return; }
        poller.execute(() -> {
            try { relay().cancel(id); say("Okay, stopping it."); }
            catch (Exception e) { say("I couldn't reach your relay to stop it."); }
        });
    }

    // =================================================================== follow requests

    private void startPolling() {
        if (polling) return;
        polling = true;
        wake.acquire(GIVE_UP_MS);
        poller.schedule(this::pollOnce, 1500, TimeUnit.MILLISECONDS);
    }

    /** Checks every request we're waiting for, plays what's new, and schedules the next check. */
    private void pollOnce() {
        if (active.isEmpty()) { polling = false; releaseWakeIfIdle(); return; }
        Relay relay = relay();
        boolean young = false;
        for (Job job : new ArrayList<>(active.values())) {
            if (SystemClock.elapsedRealtime() - job.started < 3 * 60_000L) young = true;
            try {
                JSONObject j = relay.job(job.id).getJSONObject("job");
                job.errors = 0;
                String st = j.optString("status");
                String said = j.optString("said", "");
                if (!said.isEmpty() && !said.equals(job.said)) {
                    job.said = said;
                    setStatus("Heard: " + said);
                }
                // "Got it: ..." from the PC (skip it if the answer is already there).
                if (j.optBoolean("has_ack") && !job.ackPlayed && !FINAL.contains(st)) {
                    job.ackPlayed = true;
                    File ack = relay.downloadAudio(job.id, "ack", new File(audioDir(), job.id + "-ack.mp3"));
                    if (ack != null) enqueue(ack);
                }
                if ("working".equals(st) && !job.workingShown) {
                    job.workingShown = true;
                    setStatus("Claude is working on: " + job.said);
                }
                if (FINAL.contains(st)) {
                    finish(job, j, relay);
                } else if (SystemClock.elapsedRealtime() - job.started > GIVE_UP_MS) {
                    active.remove(job.id);
                    setStatus("Stopped waiting. You'll still get the notification.");
                }
            } catch (Exception e) {
                if (++job.errors == 5) setStatus("Can't reach the relay right now. Still trying...");
            }
        }
        // Every 2 s at first (quick "Got it"), then every 5 s for long jobs.
        poller.schedule(this::pollOnce, young ? 2000 : 5000, TimeUnit.MILLISECONDS);
    }

    /** A request finished: play the answer audio, or read the text if there's no audio. */
    private void finish(Job job, JSONObject j, Relay relay) {
        active.remove(job.id);
        String st = j.optString("status");
        String reply = j.optString("reply", "");
        String spoken = j.optString("spoken", "");
        if (spoken.isEmpty()) spoken = reply;
        lastSpoken = spoken.isEmpty() ? endWords(st) : spoken;

        File answer = null;
        if (j.optBoolean("has_answer")) {
            try {
                answer = relay.downloadAudio(job.id, "answer", new File(audioDir(), job.id + "-answer.mp3"));
            } catch (Exception e) {
                answer = null;   // the download failed: read the text aloud instead
            }
        }
        lastAnswer = answer;
        if (answer != null) enqueue(answer);
        else say(lastSpoken);

        String text = reply.isEmpty() ? spoken : reply;
        setStatus(endWords(st) + (text.isEmpty() ? "" : " " + shorten(text, 220)));
    }

    private static String endWords(String st) {
        switch (st) {
            case "done": return "Done.";
            case "cancelled": return "Stopped.";
            case "expired": return "Not run: your PC was offline too long.";
            default: return "Something went wrong.";
        }
    }

    private void repeatLast() {
        File f = lastAnswer;
        if (f != null && f.exists()) { stopPlayback(); enqueue(f); }
        else if (!lastSpoken.isEmpty()) say(lastSpoken);
        else say("Nothing to repeat yet.");
    }

    // =================================================================== playback

    /** Adds an MP3/WAV to the queue that plays through the glasses. */
    private void enqueue(File f) {
        main.post(() -> {
            playQueue.add(f);
            if (player == null) playNext();
        });
    }

    /** Plays the next queued file (main thread). Waits while we're recording. */
    private void playNext() {
        if (player != null || recording) return;
        File f = playQueue.poll();
        if (f == null) {
            abandonFocus();
            setPlaybackState(PlaybackState.STATE_PAUSED);
            releaseWakeIfIdle();
            return;
        }
        try {
            requestFocus();
            MediaPlayer mp = new MediaPlayer();
            // "Media" audio: it goes to the glasses in good quality, and it keeps this app
            // as the one that receives the glasses' play/pause tap.
            mp.setAudioAttributes(mediaAttributes());
            mp.setDataSource(f.getAbsolutePath());
            mp.setOnCompletionListener(m -> { m.release(); player = null; playNext(); });
            mp.setOnErrorListener((m, what, extra) -> { m.release(); player = null; playNext(); return true; });
            mp.prepare();
            mp.start();
            player = mp;
            setPlaybackState(PlaybackState.STATE_PLAYING);
        } catch (Exception e) {
            player = null;
            main.post(this::playNext);   // skip a broken file
        }
    }

    private void stopPlayback() {
        main.post(() -> {
            playQueue.clear();
            if (player != null) {
                try { player.stop(); } catch (RuntimeException ignored) { }
                player.release();
                player = null;
            }
            abandonFocus();
        });
    }

    /** Speaks a short message with the phone's own voice. */
    private void say(String text) {
        if (tts != null && ttsReady && text != null && !text.isEmpty()) {
            tts.speak(text, TextToSpeech.QUEUE_ADD, null, "gc-" + SystemClock.elapsedRealtime());
        }
    }

    private static AudioAttributes mediaAttributes() {
        return new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build();
    }

    /** Lowers music from other apps while an answer plays. */
    private void requestFocus() {
        if (focus == null) {
            focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(mediaAttributes())
                    .build();
        }
        am.requestAudioFocus(focus);
    }

    private void abandonFocus() {
        if (focus != null) am.abandonAudioFocusRequest(focus);
    }

    /**
     * Android sends Bluetooth play/pause to the app that most recently played audio.
     * A quarter second of silence makes that this app, so the glasses' tap comes here.
     * (If you play music in another app afterwards, open GlassesClaude once to take it back.)
     */
    private void claimMediaButtons() {
        new Thread(() -> {
            try {
                int rate = 16_000;
                short[] silence = new short[rate / 4];
                AudioTrack t = new AudioTrack.Builder()
                        .setAudioAttributes(new AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_MEDIA)
                                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                                .build())
                        .setAudioFormat(new AudioFormat.Builder()
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .setSampleRate(rate)
                                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                                .build())
                        .setBufferSizeInBytes(silence.length * 2)
                        .setTransferMode(AudioTrack.MODE_STATIC)
                        .build();
                t.write(silence, 0, silence.length);
                t.play();
                SystemClock.sleep(400);
                t.stop();
                t.release();
            } catch (RuntimeException ignored) { }
        }, "claim-buttons").start();
    }

    private void setPlaybackState(int state) {
        if (session == null) return;
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                        | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_SKIP_TO_NEXT
                        | PlaybackState.ACTION_SKIP_TO_PREVIOUS | PlaybackState.ACTION_STOP)
                .setState(state, 0, 1f)
                .build());
    }

    // =================================================================== status + notification

    private void setStatus(String text) {
        status = text;
        if (running) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            nm.notify(NOTE_ID, buildNote(text));
        }
        notifyUi();
    }

    private void notifyUi() {
        main.post(() -> {
            UiListener l = ui;
            if (l != null) l.onStatusChanged();
        });
    }

    private void createChannel() {
        NotificationChannel ch = new NotificationChannel(CHANNEL, "GlassesClaude status",
                NotificationManager.IMPORTANCE_LOW);          // quiet: no sound, no pop-up
        ch.setDescription("Shows what GlassesClaude is doing.");
        getSystemService(NotificationManager.class).createNotificationChannel(ch);
    }

    private Notification buildNote(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("GlassesClaude")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(open)
                .addAction(action("Talk", ACTION_TALK, 1))
                .addAction(action("Stop", ACTION_STOP, 2))
                .addAction(action("Quit", ACTION_QUIT, 3));
        if (Build.VERSION.SDK_INT >= 31) b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        return b.build();
    }

    private Notification.Action action(String label, String act, int code) {
        PendingIntent pi = PendingIntent.getService(this, code,
                new Intent(this, GlassesService.class).setAction(act),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Action.Builder(Icon.createWithResource(this, R.drawable.ic_stat), label, pi).build();
    }

    // =================================================================== small helpers

    private Relay relay() { return new Relay(prefs.relay(), prefs.key()); }

    private File audioDir() {
        File d = new File(getCacheDir(), "audio");
        if (!d.exists()) d.mkdirs();
        return d;
    }

    /** Deletes recordings and answers left from earlier runs. */
    private void cleanCache() {
        File[] files = audioDir().listFiles();
        if (files != null) for (File f : files) f.delete();
    }

    private void releaseWakeIfIdle() {
        if (!recording && active.isEmpty() && player == null && wake != null && wake.isHeld()) wake.release();
    }

    private static String shorten(String s, int max) {
        s = s.replace('\n', ' ').trim();
        return s.length() <= max ? s : s.substring(0, max - 1) + "…";
    }
}
