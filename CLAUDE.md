# CLAUDE.md

Context for AI coding agents working on this project.

## What this is

A free, WiFi-only bridge that streams a Windows PC's system audio to an iPhone
(and from there to AirPods over normal Bluetooth). No iOS app: the phone plays
the stream in a plain Safari page. See `README.md` for user-facing docs.

```
WASAPI loopback (pyaudiowpatch) → LoopbackAudioTrack (aiortc) → Opus/RTP over WebRTC → Safari <audio>
```

## Files

- `server.py` — everything PC-side: capture thread, aiortc track, aiohttp
  signaling (`POST /offer`, aiortc offer/answer, no trickle ICE) and static
  serving of `index.html`. One process, one port (default 8080).
- `index.html` — the Safari receiver. Start button (iOS gesture unlock),
  recvonly transceiver, live jitter-buffer readout via `getStats()`.
- `requirements.txt` — pinned; tested on Python 3.12 / Windows 10.

## Run / verify

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python server.py            # prints the LAN URL(s) to open in Safari
.\.venv\Scripts\python server.py --list-devices
```

To verify changes without a phone, run `python verify.py` while the server is
running. It measures the received stream during BOTH a tone phase (plays a
real tone through the PC's output) and a silence phase, and checks
(a) packets/s ≈ 50, (b) zero pts gaps, (c) media-clock span ≈ wall-clock span.
Exit code 0 = healthy. Run it after touching anything near
`LoopbackAudioTrack.recv()`.

## Hard-won invariants — do not regress these

The scheduler in `LoopbackAudioTrack.recv()` is the heart of the project and
has been broken in two opposite ways already:

1. **WASAPI loopback delivers NOTHING while the PC renders silence** (measured
   on real hardware, not a theory). Any design that emits RTP frames only when
   capture data arrives lets the RTP clock stall during quiet moments; Safari
   reads the stall as network jitter and permanently inflates its playout
   buffer (~1 s delay that never drains).
2. **The RTP timeline must be gapless** — pts advances exactly one 20 ms chunk
   per frame, every frame. A "smarter" wall-clock-anchored pts scheme that
   jumped timestamps forward after drops/starvation caused constant audible
   stutter (the receiver conceals every gap with a mute).

The current design satisfies both: event-driven forwarding while audio flows,
absolute-schedule silence frames while starved, schedule *slides* when badly
behind (never rapid-fire catch-up), and a grace window ensures late-but-real
chunks are never displaced by silence. Read the `recv()` docstring before
touching any of it, and re-run the two-phase verification after.

Other constraints:

- aiortc's Opus ptime is hard-coded to 20 ms (960 samples @ 48 kHz); the
  capture chunk matches it deliberately.
- `timeBeginPeriod(1)` is required — Windows' default ~15.6 ms timer
  granularity exceeds the scheduler's grace window.
- The depth-1 drop-stale queue in `LoopbackCapture` is the anti-buffering
  knob; do not deepen it to "fix" glitches.
- No HTTPS/STUN/TURN needed: plain http + host ICE candidates work for
  LAN-only, receive-only Safari playback. Don't add them.
- The capture stream binds one device at startup; switching Windows' default
  output requires a server restart (documented user-facing).

<!-- ATELIER:COORDINATION:START -->
## Multi-agent coordination (Atelier)

You are one of several AI agents working in this same project at the same time, coordinated through the **atelier** MCP server. Tools: `whoami`, `file_claim`, `file_release`, `task_list`, `task_add`, `task_claim`, `task_update`, `memory_set`, `memory_list`, `memory_backlinks`, `message_send`, `inbox`.

**First, call `whoami` (with your name).** It returns your **role** and the team roster. Act according to your role without being asked:
- **builder** → implement features and write code.
- **reviewer** → review the other agents' changes for bugs/quality; don't add features, report via `message_send`/`memory_set`.
- **scout** → explore & research, record findings via `memory_set`; don't edit files.
- **coordinator** → split the goal into `task_add` tasks and keep the board organized; delegate, don't edit.

Always, without being asked:
- **Before editing ANY file, call `file_claim` with its path.** If it returns BLOCKED, another agent is in that file right now — do not edit it; pick other work or `message_send` the holder. Call `file_release` when you finish the file. This keeps the team off each other's toes automatically.
- For larger units of work, also `task_add`/`task_claim` and check `task_list` + `memory_list` so efforts don't overlap.
- Call `task_update` to mark work done, and `memory_set` to record decisions others should know. In memory values, cross-link related notes with `[[other-key]]` wikilinks; use `memory_backlinks` to see what references a note.
<!-- ATELIER:COORDINATION:END -->
