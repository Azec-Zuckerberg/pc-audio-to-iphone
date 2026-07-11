"""
server.py — stream Windows system audio to an iPhone over WiFi using WebRTC.

Pipeline:
    WASAPI loopback (pyaudiowpatch) -> LoopbackAudioTrack (aiortc) -> Opus/RTP -> mobile Safari

One process, one port: aiohttp serves index.html plus the single POST /offer
signaling endpoint; aiortc owns the WebRTC session and encodes Opus.

Usage:
    python server.py                 # capture the current default output device
    python server.py --list-devices  # show every capturable loopback device
    python server.py --device 21     # capture a specific loopback device
    python server.py --port 9000     # serve on another port (default 8080)
"""

import argparse
import asyncio
import fractions
import logging
import socket
import threading
import time
from pathlib import Path

import pyaudiowpatch as pyaudio
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

ROOT = Path(__file__).parent
DEFAULT_PORT = 8080

# Capture block size. 20 ms matches aiortc's Opus packet time (ptime), which is
# hard-coded to 20 ms in aiortc (aiortc.codecs.opus, 960 samples @ 48 kHz) —
# that is already the smallest frame it supports without patching the library.
CHUNK_MS = 20
CHUNK_S = CHUNK_MS / 1000

log = logging.getLogger("airpods-bridge")


# --------------------------------------------------------------------------- #
# Audio capture
# --------------------------------------------------------------------------- #
class LoopbackCapture:
    """Captures the PC's rendered output ("what you hear") via WASAPI loopback
    in a background thread and fans the PCM chunks out to per-connection
    asyncio queues.

    LATENCY KNOB (the main one): every subscriber queue has depth 1, and when
    a new chunk arrives while the previous one is still unread, the stale
    chunk is DROPPED instead of queued. Audio can therefore never pile up
    between capture and the Opus encoder — a slow consumer costs a glitch,
    never growing delay.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, device_index: int | None = None):
        self._loop = loop
        self._pa = pyaudio.PyAudio()
        self.device = self._pick_device(device_index)
        self.rate = int(self.device["defaultSampleRate"])
        # Opus is mono/stereo only; take the first 2 channels of >2ch devices.
        self.channels = min(int(self.device["maxInputChannels"]), 2)
        self.samples_per_chunk = self.rate * CHUNK_MS // 1000
        self._subscribers: set[asyncio.Queue] = set()
        self._sub_lock = threading.Lock()
        self._running = False
        self._stream = None
        self._thread: threading.Thread | None = None

    def _pick_device(self, device_index: int | None) -> dict:
        if device_index is not None:
            device = self._pa.get_device_info_by_index(device_index)
            if not device.get("isLoopbackDevice"):
                raise SystemExit(
                    f"Device {device_index} ({device['name']!r}) is not a loopback "
                    f"device. Run with --list-devices to see valid choices."
                )
            return device
        try:
            # Loopback mirror of whatever Windows currently uses as default output.
            return self._pa.get_default_wasapi_loopback()
        except (OSError, LookupError) as exc:
            raise SystemExit(
                "Could not find a default WASAPI loopback device. Make sure a "
                "playback device is enabled in Windows, or pick one explicitly "
                f"with --device (see --list-devices). Original error: {exc}"
            )

    def start(self) -> None:
        self._stream = self._pa.open(
            format=pyaudio.paInt16,  # PortAudio converts the float32 mix to s16 for us
            channels=self.channels,
            rate=self.rate,
            frames_per_buffer=self.samples_per_chunk,
            input=True,
            input_device_index=self.device["index"],
        )
        self._running = True
        self._thread = threading.Thread(target=self._run, name="wasapi-capture", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Blocking read loop; runs in its own thread so the event loop never waits on audio I/O."""
        while self._running:
            try:
                chunk = self._stream.read(self.samples_per_chunk, exception_on_overflow=False)
            except OSError as exc:
                # Typically the captured device was removed/changed. Subscribers
                # keep the connection alive with silence; a restart re-binds.
                log.error("Capture stream failed (%s). Restart the server if you "
                          "changed the default output device.", exc)
                break
            with self._sub_lock:
                subscribers = list(self._subscribers)
            for queue in subscribers:
                self._loop.call_soon_threadsafe(self._offer_chunk, queue, chunk)

    @staticmethod
    def _offer_chunk(queue: asyncio.Queue, chunk: bytes) -> None:
        # Depth-1, drop-stale delivery (see class docstring — main latency knob).
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(chunk)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        with self._sub_lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._sub_lock:
            self._subscribers.discard(queue)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        self._pa.terminate()


class LoopbackAudioTrack(MediaStreamTrack):
    """aiortc audio source that yields the captured system audio as PCM
    AudioFrames; aiortc's sender encodes them to Opus (resampling to 48 kHz
    stereo internally only if the device isn't already 48 kHz stereo — no
    extra buffering is added on our side)."""

    kind = "audio"

    # Scheduling constants for recv() below, in units of one chunk (20 ms):
    # grace: how long past a frame's due time we keep waiting for REAL audio
    # before emitting silence instead. Must comfortably exceed asyncio timer
    # jitter (~1 ms once fine timers are enabled) so a merely-late capture
    # chunk is never displaced by silence mid-music — displacement is audible
    # as stutter. The resulting offset is constant, so it adds NO latency.
    GRACE_CHUNKS = 2
    # re-anchor: if we fall further than this behind schedule (event-loop
    # stall), slide the whole schedule forward instead of machine-gunning
    # catch-up frames. Must exceed grace, or steady silence re-anchors forever.
    REANCHOR_CHUNKS = 3

    def __init__(self, capture: LoopbackCapture):
        super().__init__()
        self._capture = capture
        self._queue = capture.subscribe()
        self._layout = "stereo" if capture.channels == 2 else "mono"
        self._silence = bytes(capture.samples_per_chunk * capture.channels * 2)
        self._pts = 0
        self._start: float | None = None  # wall-clock instant where pts == 0

    async def recv(self) -> AudioFrame:
        """One 20 ms frame per call, on a hybrid schedule.

        LATENCY + STABILITY CORE — two invariants, both mandatory:

        1. The RTP timeline is GAPLESS: pts advances by exactly one chunk per
           frame, every frame. Timeline gaps force the receiver to conceal
           missing audio, which is audible as stutter.
        2. The media clock runs at wall-clock RATE even when the PC is silent.
           WASAPI loopback delivers NOTHING while nothing is rendering; if
           frames were only emitted when data arrived, the RTP clock would
           stall during every quiet moment, and Safari would read the stall
           as network jitter and permanently inflate its playout buffer
           (observed: ~1 s of extra delay that never drained).

        While audio flows, capture chunks are forwarded the moment they
        arrive (event-driven — no pacing delay added). When capture starves,
        silence frames are emitted on the absolute 20 ms schedule, keeping a
        steady ~50 packets/s. Only the RATE matters to the receiver; the
        schedule's absolute offset is irrelevant, which is why falling badly
        behind is handled by sliding the schedule, never by catching up.
        """
        if self.readyState != "live":
            raise MediaStreamError

        n = self._capture.samples_per_chunk
        rate = self._capture.rate

        now = time.monotonic()
        due = now if self._start is None else self._start + self._pts / rate
        if self._start is None or now - due > self.REANCHOR_CHUNKS * CHUNK_S:
            self._start = now - self._pts / rate
            due = now

        try:
            timeout = max(0.0, due + self.GRACE_CHUNKS * CHUNK_S - now)
            pcm = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            pcm = self._silence  # genuinely nothing rendering: silence, on schedule

        frame = AudioFrame(format="s16", layout=self._layout, samples=n)
        frame.planes[0].update(pcm)
        frame.sample_rate = rate
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, rate)
        self._pts += n
        return frame

    def stop(self) -> None:
        self._capture.unsubscribe(self._queue)
        super().stop()


# --------------------------------------------------------------------------- #
# HTTP: static page + signaling
# --------------------------------------------------------------------------- #
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "index.html")


async def offer(request: web.Request) -> web.Response:
    """Standard aiortc offer/answer exchange: the phone POSTs its SDP offer
    (candidates included, no trickle) and gets the complete answer back."""
    params = await request.json()
    remote_offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    request.app["pcs"].add(pc)
    track = LoopbackAudioTrack(request.app["capture"])
    pc.addTrack(track)
    log.info("New peer (%d active)", len(request.app["pcs"]))

    @pc.on("connectionstatechange")
    async def on_state_change() -> None:
        log.info("Peer connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            track.stop()
            await pc.close()
            request.app["pcs"].discard(pc)

    await pc.setRemoteDescription(remote_offer)
    await pc.setLocalDescription(await pc.createAnswer())
    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


def enable_fine_timers() -> None:
    """Windows' default timer granularity is ~15.6 ms — far too coarse for a
    20 ms audio schedule (sleep overshoot would exceed the scheduling grace
    and turn silence handling into stutter). timeBeginPeriod(1) gives ~1 ms
    timers; since Win10 2004 the effect is per-process. Same call OBS and
    Chrome make."""
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:  # non-Windows or missing winmm: run with coarse timers
        log.warning("Could not raise timer resolution; audio pacing may be coarse.")


def get_lan_ip() -> str:
    """Find the LAN-facing IP without sending any traffic (UDP connect only
    selects the outbound interface, no packet leaves the machine)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()


def get_all_ipv4() -> list[str]:
    """All local IPv4 addresses — on multi-adapter machines (VPN, WSL, WiFi)
    the auto-detected primary IP may not be on the phone's network, so the
    banner offers the alternatives too."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        ips = {info[4][0] for info in infos}
    except OSError:
        ips = set()
    return sorted(ip for ip in ips if not ip.startswith("127."))


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def make_on_startup(device_index: int | None, port: int):
    async def on_startup(app: web.Application) -> None:
        capture = LoopbackCapture(asyncio.get_running_loop(), device_index)
        capture.start()
        app["capture"] = capture
        primary = get_lan_ip()
        others = [ip for ip in get_all_ipv4() if ip != primary]
        print()
        print("=" * 62)
        print(f"  Capturing : [{capture.device['index']}] {capture.device['name']}")
        print(f"              {capture.rate} Hz, {capture.channels} ch, {CHUNK_MS} ms chunks")
        print("  Open this URL in Safari on your iPhone:")
        print(f"      http://{primary}:{port}")
        if others:
            print("  (multiple networks detected — if that URL doesn't load, try:")
            for ip in others:
                print(f"      http://{ip}:{port}")
            print("  — use whichever is on the same WiFi as the phone.)")
        print("=" * 62)
        print("  If Windows Firewall prompts, ALLOW Python on private networks,")
        print("  otherwise the phone cannot reach this server.")
        print(flush=True)

    return on_startup


async def on_shutdown(app: web.Application) -> None:
    await asyncio.gather(*(pc.close() for pc in app["pcs"]), return_exceptions=True)
    app["pcs"].clear()
    if "capture" in app:
        app["capture"].stop()


def list_loopback_devices() -> None:
    pa = pyaudio.PyAudio()
    try:
        default = pa.get_default_wasapi_loopback()
    except (OSError, LookupError):
        default = None
    print("Capturable loopback devices (each mirrors one output device):")
    for device in pa.get_loopback_device_info_generator():
        marker = "   <- current default output" if default and device["index"] == default["index"] else ""
        print(
            f"  [{device['index']:3d}] {device['name']}  "
            f"({int(device['defaultSampleRate'])} Hz, {device['maxInputChannels']} ch){marker}"
        )
    pa.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream Windows system audio to a browser via WebRTC.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT})")
    parser.add_argument("--device", type=int, default=None,
                        help="loopback device index to capture (default: mirror of the default output)")
    parser.add_argument("--list-devices", action="store_true", help="list loopback devices and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.list_devices:
        list_loopback_devices()
        return

    enable_fine_timers()
    app = web.Application()
    app["pcs"] = set()
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.on_startup.append(make_on_startup(args.device, args.port))
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
