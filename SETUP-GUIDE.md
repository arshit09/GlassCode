# Talk to Claude Code from your AI glasses, from anywhere

Tap your glasses, say what you want built, and keep walking. Claude Code does the work on your PC at home, and the answer plays in your glasses. Everything here is free except Claude itself, which uses the plan you already have.

> **Download the project:** [GlassCode on GitHub](https://github.com/arshit09/GlassCode)

---

## How it works

1. **You tap your glasses.** They send "play/pause" to your phone over Bluetooth, like earbuds do.
2. **The GlassesClaude Android app records you** through the glasses' microphone and stops when you pause.
3. **It sends the recording to your own relay** on Cloudflare (a free Worker with a small database).
4. **Your PC at home checks the relay every 5 seconds**, downloads the recording and turns it into text on your own computer (Whisper).
5. **Claude Code does the work** in the project folder you chose.
6. **The answer comes back**: you hear "Got it" right away, then Claude's answer in your glasses, and a notification lands on your phone.

Your PC never opens a port to the internet: it only reaches out to your relay.

## What you need

| Thing | Why | Cost |
| --- | --- | --- |
| AI glasses that pair with your phone like Bluetooth headphones (tested with Rokid AI glasses) | Microphone, speakers and the tap | You own them |
| A Windows PC that stays on at home | Runs Claude Code and the speech-to-text | Free |
| Claude Code, signed in | Does the actual work | Your Claude plan |
| A free Cloudflare account | Hosts your private relay | Free (100,000 requests a day) |
| An Android phone | Runs the tap-to-talk app | Free |
| Android Studio (optional) | Only if you want to build the app yourself | Free |
| The free ntfy app | "Claude finished" notifications | Free |

## Part 1: Set up the PC

1. **Unzip the folder to `C:\`.** On the GitHub page click **Code > Download ZIP**. Right-click the zip > Properties > tick **Unblock** > OK. Then right-click > **Extract All** > set the destination to `C:\`, and rename the new `GlassCode-main` folder to `GlassesClaude`. You now have `C:\GlassesClaude`. Keep it out of OneDrive, because it grows to about 1 GB.
2. **Check Claude Code.** Open PowerShell and run `claude --version`. If it isn't found, install it with `irm https://claude.ai/install.ps1 | iex`, open a new PowerShell window, run `claude` once and sign in.
3. **Run the installer.** Double-click **1 - Install** in `C:\GlassesClaude`. It installs Python 3.12 if needed, the speech and audio packages, and the speech model (about 0.5 GB). It takes 5 to 15 minutes; click **Yes** if Windows asks. Wait for "Setup finished!".
4. **Choose where Claude works.** Open `C:\GlassesClaude\settings.toml`. Leave `project_folder = ''` to test in the safe `workspace` folder, or set it to your project, for example `project_folder = 'C:\Users\you\source\repos\my-site'`. If you only use your phone, set `enabled = false` under `[desk_mode]`. Save.

## Part 2: Put your relay on Cloudflare

1. **Make a free Cloudflare account** at dash.cloudflare.com/sign-up and confirm your email. In the dashboard, open **Workers & Pages** once: that gives you a free `workers.dev` address.
2. **Run the setup.** Double-click **3 - Set up away mode (optional)** in `C:\GlassesClaude`. It installs Node.js if needed, opens your browser to sign in to Cloudflare (click **Allow**), uploads your relay, creates your private keys and opens a **Set up your phone** page with a QR code. Keep that page open.
   - If it says the relay doesn't answer yet, wait a minute and run it again. Your keys are kept.
3. **Start the PC side.** Double-click **2 - Start**. The black window should say **AWAY MODE** with your relay address. Leave it open and the PC on; it keeps the PC from sleeping.
   - Optional: run `tools\Start GlassesClaude with Windows (optional)` so it starts by itself.

## Part 3: Set up your phone

1. **Open the web app.** Scan the QR code on the **Set up your phone** page with your phone camera. GlassesClaude opens with your private key, and the top says **PC online**.
2. **Turn on notifications.** Install the free **ntfy** app, tap **+**, and subscribe to the topic shown on the setup page (keep the server as ntfy.sh). Tick **Instant delivery in doze mode**. On Samsung, also set Settings > Apps > ntfy > Battery to **Unrestricted**.
3. **Copy your phone link.** In the GlassesClaude web app, tap **Settings > Copy phone link**.

## Part 4: Install the tap-to-talk Android app

1. **Install the app.** On your phone, open [GlassesClaude.apk](https://github.com/arshit09/GlassCode/releases/latest/download/GlassesClaude.apk) and let it download. Open the downloaded file and tap **Install**. If Android asks, allow your browser (or Files app) to install apps. The app isn't from the Play Store, so Google Play Protect may warn you; choose to install anyway. Every line of its code is in the `android` folder.
   - Prefer to build it yourself? Follow "Or build it yourself" in `C:\GlassesClaude\android\README.md`.
2. **Connect it.** In the GlassesClaude Android app tap **Paste and save**, then **Check my PC**: it should say your PC is online.
3. **Start it.** Tap **Start** and allow the microphone, notifications and nearby devices. Tap **Allow running in the background**. On Samsung, set Settings > Apps > GlassesClaude > Battery to **Unrestricted**.

## Part 5: Try it

1. With the glasses connected to your phone as usual, **tap the glasses once**. You hear a beep.
2. Say something like: *"Create a file called hello.txt that says hello from my glasses."* Then stop talking.
3. You hear a second beep, then **"Got it"**, then Claude's answer. A notification arrives on your phone.

Now lock your phone and put it in your pocket. That's the whole workflow.

**Tips**
- Tap again while it's listening to send right away. Tap while an answer plays to cut it off.
- Things you can say: "status", "which project", "switch to *project name*", "new chat", "repeat", "stop".
- If you play music in another app or use the glasses' own assistant, open GlassesClaude once so the tap comes back to it.
- After restarting your phone, open the app and tap **Start**.

## If something doesn't work

| Problem | Fix |
| --- | --- |
| "Last button from your glasses" stays at "none yet" | Your glasses' tap isn't sent as a media button. Use the **Talk** button in the app's notification instead. |
| Notifications don't arrive | Set ntfy's battery use to **Unrestricted** and turn off Do Not Disturb. |
| "PC isn't connected" | Make sure **2 - Start** is running and the PC is awake. |
| A request waited and never ran | Requests expire after 2 hours if the PC is off; you get a notification saying so. |
| Claude says some steps needed permission | It can't ask you to approve things by voice, so it skips them. Run that step at the desk. |

## Is it safe?

- **Your relay is private.** The phone and the PC each have their own secret key; nobody else can send commands.
- **Your PC stays closed.** It only makes outbound connections.
- **Don't share your keys.** Never post your phone link, your ntfy topic, or the PC's black window on camera.
- **The cloud forgets.** The relay deletes recordings and answers after 3 days. Everything else stays on your PC.

---

*Built with Claude Code.*
