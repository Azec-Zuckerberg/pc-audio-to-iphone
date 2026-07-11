# PC → iPhone → AirPods audio bridge

Streams your Windows PC's system audio ("what you hear") to your iPhone over
WiFi using WebRTC/Opus. The iPhone plays it in a plain Safari web page — no
app, no cable, no Bluetooth on the PC. Connect your AirPods to the iPhone the
normal way; whatever the iPhone's current audio output is, that's where the
sound goes.

```
Windows audio ──WASAPI loopback──▶ server.py (aiortc/Opus) ──WiFi/WebRTC──▶ Safari ──Bluetooth──▶ AirPods
```

## Requirements

- Windows 10/11, Python 3.9+ (built and tested with 3.12)
- PC and iPhone on the **same WiFi network**
- iOS Safari (any recent version)

## Setup (once)

```powershell
cd path\to\this\folder
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python server.py
```

The console prints the URL to open, e.g.:

```
  Open this URL in Safari on your iPhone:
      http://192.168.1.42:8080
```

If several URLs are listed (VPN / WSL / multiple adapters), use the one on the
same WiFi as the phone. Open it in Safari, tap **Start**, done.

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

## Latency notes

- End-to-end delay is typically ~150–350 ms: WebRTC itself is fast (≈30–80 ms),
  but the AirPods' own Bluetooth link adds ~100–200 ms you can't remove.
  Great for music/videos; noticeable in fast games.
- The main software knobs are already set for minimum delay:
  - server: depth-1 capture queue that drops stale audio instead of buffering
    (`LoopbackCapture`, see comment in `server.py`)
  - server: gapless, steady-cadence RTP schedule (`LoopbackAudioTrack.recv`) —
    WASAPI loopback stops delivering while the PC is silent, and a naive
    implementation lets the browser's jitter buffer balloon to ~1 s because
    of it; see the docstring in `server.py` for the full story
  - server: 1 ms Windows timer resolution (`timeBeginPeriod`), without which
    the 20 ms audio schedule can't be kept
  - client: `playoutDelayHint` / `jitterBufferTarget` set low in `index.html`
- The page shows a live `buffer N ms` readout — that's the browser's own
  jitter buffer, the browser-side share of whatever delay you perceive.
- Keep Safari **in the foreground with the screen on** — iOS suspends the tab
  (and the audio) when the phone locks or you switch apps. The page requests a
  screen wake-lock automatically where supported.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Page won't load on the phone | Same WiFi? Firewall allowed? Try the alternate URLs from the banner. Some routers isolate WiFi clients ("AP isolation") — disable it. |
| Delay grew after listening a while | Refresh the page and tap Start — a fresh connection resets the browser's buffer. If it recurs constantly, check WiFi quality. |
| Connects but silent | Is the PC actually playing audio? Is the PC volume up (loopback is post-volume)? |
| Sound stopped after switching default output device | Restart `server.py` — it binds the device at startup. |
| Audio stopped when phone locked | Expected on iOS; unlock and tap Start again. Keep the screen on. |
| Choppy audio | Weak WiFi. Move closer to the router; avoid 2.4 GHz if possible. |
