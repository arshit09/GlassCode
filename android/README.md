# GlassesClaude for Android: tap your glasses to talk

With this small app your phone can stay in your pocket:

1. **Tap your glasses once.** They send "play/pause" to the phone over Bluetooth.
2. **You hear a beep and talk.** The app records through the glasses' microphone and stops when you pause.
3. **It goes to your PC** through your Cloudflare relay, and Claude Code does the work.
4. **You hear "Got it", then Claude's answer, in your glasses.**

Tap again while it's listening to send early. Tap while an answer is playing to cut it off and talk.
If your glasses send "next track", that repeats the last answer.

It uses the same relay and phone key as the GlassesClaude web app, so set up away mode first
(`3 - Set up away mode (optional)` on the PC).

## Install it (once)

**Easiest:** on your phone, open
[GlassesClaude.apk](https://github.com/arshit09/GlassCode/releases/latest/download/GlassesClaude.apk)
(from the [latest release](https://github.com/arshit09/GlassCode/releases/latest)), open the downloaded file
and tap **Install**. If Android asks, allow your browser (or Files app) to install apps. Google Play Protect
may warn you because the app isn't from the Play Store; choose to install anyway.

### Or build it yourself

You need Android Studio on your PC and a USB cable for your phone.

1. **Turn on USB debugging on the phone.** Settings > About phone > Software information > tap
   *Build number* 7 times. Then Settings > Developer options > turn on *USB debugging*.
2. **Open this `android` folder in Android Studio** (File > Open). The first sync downloads Gradle and
   the Android build tools; wait until it finishes. If it offers to upgrade the Android Gradle Plugin,
   say yes.
3. **Plug in the phone**, allow USB debugging on the phone, pick it in the device list at the top, and
   press **Run** (the green triangle). The app opens on the phone.

No cable? In the *Build* menu choose *Build APK(s)* (newer versions: *Generate App Bundles or APKs >
Generate APKs*), copy `app/build/outputs/apk/debug/app-debug.apk` to the phone and open it (allow
installing from that app).

## Set it up (once)

1. In the GlassesClaude **web app** on the phone: *Settings > Copy phone link*.
2. In the **Android app**: tap **Paste and save**, then **Check my PC** (it should say your PC is online).
3. Tap **Start** and allow the microphone, notifications and nearby devices.
4. Tap **Allow running in the background**. On Samsung also set *Settings > Apps > GlassesClaude >
   Battery* to **Unrestricted**.
5. Tap your glasses. The line *Last button from your glasses* shows what they sent. If it stays at
   "none yet", your glasses' tap doesn't reach Android as a media button.

## Good to know

- After you restart the phone, open the app once and tap **Start**.
- Android sends the glasses' tap to the app that last played sound. If you play music in another app
  (or use the glasses' own assistant), open GlassesClaude once to take the tap back.
- Close the GlassesClaude web app while you use this app, or both will play the answers.
- The ongoing notification shows what's happening and has **Talk**, **Stop** and **Quit** buttons.

## How it works (code)

| File | What it does |
| --- | --- |
| `GlassesService.java` | Foreground service: media session for the tap, sends, follows the request, plays audio |
| `Recorder.java` | Switches the mic to the Bluetooth glasses, records AAC, stops when you pause |
| `Relay.java` | HTTPS calls to your relay: `POST /api/ask`, `GET /api/jobs/:id`, audio downloads, cancel |
| `Prefs.java` | Keeps the relay address and phone key from the pasted phone link |
| `MainActivity.java` | The setup screen |
