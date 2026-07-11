# Use AirPods as PC headphones — over WiFi, no Bluetooth, no app

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-black)
![Python](https://img.shields.io/badge/python-3.9%2B-black)
![License](https://img.shields.io/badge/license-MIT-black)

**Your Windows PC has no Bluetooth (or no headset), but you have a phone and
earphones? This streams your PC's system audio over WiFi to a plain web page
on your phone — and your phone plays it to your AirPods, wired earbuds, or
anything else. Free, open source, ~200 ms of delay.**

```
Windows audio ──WASAPI loopback──▶ server.py (WebRTC/Opus) ──WiFi──▶ phone browser ──▶ AirPods / any output
```

- **No app, no sideloading, no Mac, no cable, no Bluetooth dongle** — the
  receiver is one web page; iOS Safari works out of the box.
- **Not iPhone-only despite the examples**: any device with a modern browser
  can listen — Android (Chrome), iPad, another laptop — and several devices
  can listen at the same time.
- **Nothing leaves your network**: PC → phone directly over your LAN, no
  server in the cloud, no account.
- **Low latency by design**: WebRTC + Opus, ~200 ms end-to-end with AirPods
  (most of that is the AirPods' own Bluetooth link). Fine for music, YouTube
  and films; fast competitive games will feel the delay.

## Requirements

- Windows 10/11, Python 3.9+ (built and tested with 3.12)
- PC and phone on the **same WiFi network**

## Setup (once)

```powershell
git clone https://github.com/Azec-Zuckerberg/pc-audio-to-iphone
cd pc-audio-to-iphone
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python server.py
```

The console prints one URL for both modes, e.g.:

```
  Open in Safari on your iPhone (Listen / Mic toggle on the page):
      https://192.168.1.42:8080
```

If several URLs are listed (VPN / WSL / multiple adapters), use the one on the
same WiFi as the phone. Open it, tap **START** — done. The page has a
**Listen / Mic** toggle (Listen = PC audio → your AirPods; Mic = your phone's
microphone → the PC, see
*[Use your phone as a PC microphone](#use-your-phone-as-a-pc-microphone)*).

> **It's `https://` and Safari warns "not private" — that's expected.** The
> address is HTTPS with a self-signed certificate the server makes on first run
> (browsers only allow microphone access on a secure page). Tap **Show Details
> → visit this website** the first time. It's your own PC on your own LAN.

> **Windows Firewall:** the first run pops a firewall prompt for Python.
> You **must click Allow** (at least for *Private networks*) or the phone will
> never reach the server. If you accidentally clicked Cancel: Windows Security
> → Firewall & network protection → *Allow an app through firewall* → enable
> Python, or delete the "python.exe" block rules and run again.

## Capturing a different output device

By default the server captures the **current default output device**. To
capture another one:

```powershell
.\.venv\Scripts\python server.py --list-devices
# Capturable loopback devices (each mirrors one output device):
#   [  5] Speakers (Realtek(R) Audio) [Loopback]  (48000 Hz, 2 ch)   <- current default output
#   [  7] LG Monitor (HDMI) [Loopback]            (48000 Hz, 2 ch)

.\.venv\Scripts\python server.py --device 7
```

Tip — **avoiding double audio**: WASAPI loopback taps the signal *after* the
Windows mixer, so the PC speakers keep playing (and the master volume/mute
affects the stream too — muting the PC mutes the phone). If you don't want the
speakers audible, set Windows' default output to a device with nothing
attached (e.g. an unused monitor's HDMI audio) and capture that.

## Use your phone as a PC microphone

The bridge also runs **backwards**: your iPhone's own microphone (not the
AirPods mic — the phone's) becomes an audio input on the PC. Handy when your PC
has no mic, or you just want to talk from across the room.

Open the same URL from the banner, switch the toggle to **Mic**, and tap
**START**. (If you haven't accepted the certificate warning yet, do that first —
see the note in [Run](#run) above; the mic won't work until you do.)

By default the phone's mic just plays out of your **PC speakers** (a wireless
monitor). To make it a **real microphone** that Discord, OBS, Zoom, games, etc.
can select:

1. Install a virtual audio cable — the free
   [VB-CABLE](https://vb-audio.com/Cable/) is the usual choice. It adds a
   "CABLE Input" playback device and a matching "CABLE Output" recording device.
2. Point the bridge at it:
   ```powershell
   .\.venv\Scripts\python server.py --list-devices
   # Output devices for --mic-device (...):
   #   [  1] Speakers (Realtek(R) Audio)   <- current default output
   #   [ 30] CABLE Input (VB-Audio Virtual Cable)

   .\.venv\Scripts\python server.py --mic-device 30
   ```
3. In the app you want to speak into, pick **CABLE Output** as the microphone.

Now your phone is that app's microphone, over WiFi.

## Latency notes

- End-to-end delay is typically ~150–350 ms: WebRTC itself is fast (≈30–80 ms),
  but the AirPods' own Bluetooth link adds ~100–200 ms you can't remove.
- The main software knobs are already set for minimum delay:
  - server: depth-1 capture queue that drops stale audio instead of buffering
    (`LoopbackCapture`, see comment in `server.py`)
  - server: gapless, steady-cadence RTP schedule (`LoopbackAudioTrack.recv`) —
    WASAPI loopback stops delivering while the PC is silent, and a naive
    implementation lets the browser's jitter buffer balloon to ~1 s because
    of it; see the docstring in `server.py` for the full story
  - server: 1 ms Windows timers + high process/capture-thread priority, so a
    game loading the CPU can't jitter the audio pipeline
  - client: `playoutDelayHint` / `jitterBufferTarget` asked for the browser's
    minimum in `index.html`
- The page shows a live `buffer N ms` readout — that's the browser's own
  jitter buffer, the browser-side share of whatever delay you perceive.
- Keep the page **in the foreground with the screen on** — phones suspend
  browser tabs (and the audio) when locked. The page requests a screen
  wake-lock automatically where supported.

## FAQ

**Does this need internet?** No. Everything stays on your local network.

**Can several people/devices listen at once?** Yes — open the page on each
device and tap START.

**Android? iPad? Another PC?** Yes. The receiver is standard WebRTC in a web
page; any modern browser works.

**Why is there any delay at all?** ~30–80 ms WebRTC + browser buffer, plus
~100–200 ms inside the AirPods' Bluetooth link itself. The second part exists
with every Bluetooth product on earth; no software can remove it.

**Is the audio quality good?** 48 kHz stereo Opus — the same codec used by
Discord/WhatsApp calls and YouTube, at music-grade settings. Not bit-exact
lossless, but you will struggle to hear the difference over Bluetooth.

**Sound stopped when the phone locked?** Expected — phones suspend background
tabs. Unlock and tap START again; keep the screen on while listening.

**Can I use the phone as a mic instead?** Yes — see *[Use your phone as a PC
microphone](#use-your-phone-as-a-pc-microphone)*. Same page, same URL, just flip
the Listen/Mic toggle.

**Alternatives?** AudioRelay (freemium, needs an app on the phone), a USB
Bluetooth dongle (~$10, pairs AirPods directly but Windows Bluetooth audio can
be finicky), AirPlay-based tools (typically 1–2 s of delay, wrong direction
for phones). This project's niche: free, open source, zero install on the
listening device.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Page won't load on the phone | Same WiFi? Firewall allowed? Try the alternate URLs from the banner. Some routers isolate WiFi clients ("AP isolation") — disable it. |
| Delay grew after listening a while | Refresh the page and tap START — a fresh connection resets the browser's buffer. If it recurs constantly, check WiFi quality. |
| Connects but silent | Is the PC actually playing audio? Is the PC volume up (loopback is post-volume)? |
| Sound stopped after switching default output device | Restart `server.py` — it binds the device at startup. |
| Choppy audio | Weak WiFi. Move closer to the router; prefer 5 GHz over 2.4 GHz. |
| Safari warns the page isn't private | Expected — the URL is HTTPS with a self-signed cert. Tap *Show Details → visit this website*. It's your own PC on your LAN, and mic mode needs the secure page. |
| Mic toggle says "needs the https:// address" | The server fell back to plain HTTP because it couldn't create a certificate (rare — usually `cryptography` missing). Reinstall requirements; the banner will then show an `https://` URL. |
| Phone mic connects but nothing hears it | It plays into `--mic-device` (default: PC speakers). For apps to use it, install VB-CABLE, run with `--mic-device <CABLE Input>`, and select **CABLE Output** as the mic in the app. |

## Development

`python verify.py` (with the server running) checks stream health without a
phone: it must report ~50 pkt/s, zero pts gaps, and matching wall/media clocks
during both a tone phase and a silence phase. Run it after touching anything
near `LoopbackAudioTrack.recv()` — the invariants it checks are documented in
`CLAUDE.md`.

## License

[MIT](LICENSE). If this saved you buying a headset or a dongle, a ⭐ helps
other people find it.
