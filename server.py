"""
server.py — bridge Windows system audio and an iPhone over WiFi, both ways.

Listen (default, PC -> AirPods):
    WASAPI loopback (pyaudiowpatch) -> LoopbackAudioTrack (aiortc) -> Opus/RTP -> mobile Safari
Mic (opt-in, iPhone -> PC):
    Safari getUserMedia -> Opus/RTP -> MicPlayback (aiortc) -> WASAPI output device

aiohttp serves index.html plus two signaling endpoints (POST /offer for listen,
POST /mic-offer for mic); aiortc owns the WebRTC sessions and (de)codes Opus.
Both modes share ONE port. Mic mode requires a secure context (browsers only
grant microphone access over HTTPS), so the server generates a self-signed cert
and serves everything over HTTPS; listen mode rides the same origin. If a cert
can't be made it falls back to plain HTTP (listen only, mic unavailable).

Usage:
    python server.py                 # capture the current default output device
    python server.py --list-devices  # show capture + output devices
    python server.py --device 21     # capture a specific loopback device
    python server.py --mic-device 30 # play the phone's mic into device 30 (e.g. VB-CABLE)
    python server.py --port 9000     # serve both modes on port 9000 (default 8080)
"""

import argparse
import asyncio
import fractions
import logging
import queue as thread_queue
import socket
import ssl
import threading
import time
from pathlib import Path

import pyaudiowpatch as pyaudio
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from av.audio.resampler import AudioResampler

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
def _boost_capture_thread_priority() -> None:
    """THREAD_PRIORITY_TIME_CRITICAL for the capture thread: the 20 ms reads
    must keep their cadence even under heavy CPU load (a game running is the
    typical use case for this program)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 15)
    except Exception:
        pass


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
        _boost_capture_thread_priority()
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
# Reverse direction: iPhone microphone -> PC output device
# --------------------------------------------------------------------------- #
class MicPlayback:
    """Plays an INCOMING WebRTC audio track (the iPhone's real microphone) out
    through a WASAPI *output* device — the mirror image of LoopbackAudioTrack.

    Point --mic-device at a virtual audio cable (e.g. VB-CABLE's "CABLE
    Input") and the phone becomes a microphone that every other Windows app
    can select. With no virtual cable it simply monitors through your speakers.

    The output stream is opened LAZILY and ref-counted: it exists only while at
    least one phone is connected in mic mode. So a listen-only session pays
    nothing, and there is no speaker<->loopback feedback risk when mic mode is
    unused (playing the phone mic to the same speakers this program captures
    would otherwise loop back to the phone).

    Latency is bounded the same way capture is: the hand-off queue is shallow
    and drops the OLDEST buffered audio on overflow, so a slow sound card costs
    a glitch, never growing delay.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, device_index: int | None = None):
        self._loop = loop
        self._pa = pyaudio.PyAudio()
        self.device = self._pick_output(device_index)
        self.rate = int(self.device["defaultSampleRate"])
        self.channels = min(int(self.device["maxOutputChannels"]) or 2, 2)
        self._layout = "stereo" if self.channels == 2 else "mono"
        self._buf: "thread_queue.Queue[bytes]" = thread_queue.Queue(maxsize=8)
        self._lock = threading.Lock()
        self._refs = 0
        self._stream = None
        self._thread: threading.Thread | None = None
        self._running = False

    def _pick_output(self, device_index: int | None) -> dict:
        if device_index is not None:
            device = self._pa.get_device_info_by_index(device_index)
            if int(device.get("maxOutputChannels", 0)) < 1:
                raise SystemExit(
                    f"Device {device_index} ({device['name']!r}) has no output "
                    f"channels; it cannot play the phone's mic. Run with "
                    f"--list-devices to see valid choices."
                )
            return device
        try:
            return self._pa.get_default_output_device_info()
        except (OSError, LookupError) as exc:
            raise SystemExit(
                "Could not find a default output device to play the phone's mic "
                f"into. Pick one explicitly with --mic-device. Original error: {exc}"
            )

    # -- ref-counted stream lifetime ---------------------------------------- #
    def _acquire(self) -> None:
        with self._lock:
            self._refs += 1
            if self._refs == 1:
                self._open()

    def _release(self) -> None:
        with self._lock:
            self._refs -= 1
            if self._refs <= 0:
                self._refs = 0
                self._close_locked()

    def _open(self) -> None:
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            output=True,
            output_device_index=self.device["index"],
            frames_per_buffer=self.rate * CHUNK_MS // 1000,
        )
        self._running = True
        self._thread = threading.Thread(target=self._run, name="mic-playback", daemon=True)
        self._thread.start()
        log.info("Mic playback opened on [%d] %s", self.device["index"], self.device["name"])

    def _close_locked(self) -> None:
        self._running = False
        try:
            self._buf.put_nowait(b"")  # wake the drain thread out of its get() wait
        except thread_queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        while not self._buf.empty():  # drop stale audio so the next session is fresh
            try:
                self._buf.get_nowait()
            except thread_queue.Empty:
                break

    def _run(self) -> None:
        """Blocking write loop in its own thread (the event loop never waits on
        the sound card). getUserMedia streams continuously, so unlike loopback
        capture there is no silent-starvation case to schedule around here."""
        _boost_capture_thread_priority()
        while self._running:
            try:
                data = self._buf.get(timeout=0.1)
            except thread_queue.Empty:
                continue
            if data and self._stream is not None:
                try:
                    self._stream.write(data)
                except OSError as exc:
                    log.error("Mic playback write failed (%s); stopping playback.", exc)
                    break

    def _enqueue(self, data: bytes) -> None:
        # Depth-bounded, drop-oldest — same anti-buffering rule as capture.
        try:
            self._buf.put_nowait(data)
        except thread_queue.Full:
            try:
                self._buf.get_nowait()
            except thread_queue.Empty:
                pass
            try:
                self._buf.put_nowait(data)
            except thread_queue.Full:
                pass

    async def consume(self, track: MediaStreamTrack) -> None:
        """Drain an incoming mic track: pull decoded frames, resample to the
        output device's rate/layout, and hand the PCM to the playback thread.
        Runs until the peer disconnects (recv raises MediaStreamError)."""
        resampler = AudioResampler(format="s16", layout=self._layout, rate=self.rate)
        self._acquire()
        try:
            while True:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    # Packed s16: one plane of interleaved samples. Slice to the
                    # exact valid length (av may pad the buffer) — reading the
                    # plane bytes directly avoids a numpy dependency.
                    self._enqueue(bytes(out.planes[0])[: out.samples * self.channels * 2])
        except MediaStreamError:
            pass
        finally:
            self._release()

    def stop(self) -> None:
        with self._lock:
            self._refs = 0
            self._close_locked()
        self._pa.terminate()


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


async def mic_offer(request: web.Request) -> web.Response:
    """Reverse-direction signaling: the phone sends a sendonly offer carrying
    its microphone; the PC receives the track and plays it into the mic
    playback device. Same non-trickle offer/answer shape as /offer."""
    params = await request.json()
    remote_offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    request.app["pcs"].add(pc)
    playback = request.app["mic_playback"]
    log.info("New mic peer (%d active)", len(request.app["pcs"]))

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        if track.kind != "audio":
            return
        task = asyncio.ensure_future(playback.consume(track))
        request.app["mic_tasks"].add(task)
        task.add_done_callback(request.app["mic_tasks"].discard)

    @pc.on("connectionstatechange")
    async def on_state_change() -> None:
        log.info("Mic peer connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            request.app["pcs"].discard(pc)

    await pc.setRemoteDescription(remote_offer)
    await pc.setLocalDescription(await pc.createAnswer())
    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


def tune_windows_scheduling() -> None:
    """Two OS-level nudges that keep the 20 ms audio schedule honest:

    - timeBeginPeriod(1): Windows' default ~15.6 ms timer granularity is
      coarser than the scheduler's grace window (sleep overshoot would turn
      silence handling into stutter); this gives ~1 ms timers. Per-process
      since Win10 2004 — the same call OBS and Chrome make.
    - HIGH_PRIORITY_CLASS: keeps the event loop responsive while a game or
      video encoder is loading the CPU — exactly when this program is used."""
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00000080)  # HIGH_PRIORITY_CLASS
    except Exception:  # non-Windows or restricted environment
        log.warning("Could not tune OS scheduling; audio pacing may be coarser.")


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


def ensure_self_signed_cert() -> tuple[Path, Path] | None:
    """Mic mode needs HTTPS: browsers only grant getUserMedia (microphone
    access) in a secure context, so plain http://LAN-IP is blocked. We
    self-sign a long-lived cert once and cache it next to server.py; the phone
    shows a one-time "not trusted" warning the user taps through (proceeding
    still grants the secure context Safari requires). Returns None — disabling
    mic mode but leaving listen mode untouched — if signing isn't possible."""
    cert_path = ROOT / ".mic-cert.pem"
    key_path = ROOT / ".mic-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    try:
        import datetime
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        log.warning("cryptography unavailable; cannot self-sign a cert, so mic mode is off.")
        return None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "airpods-bridge")])
    sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for ip in [*get_all_ipv4(), "127.0.0.1"]:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    log.info("Generated self-signed cert for mic mode (%s).", cert_path.name)
    return cert_path, key_path


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def make_on_startup(device_index: int | None, mic_device_index: int | None):
    async def on_startup(app: web.Application) -> None:
        loop = asyncio.get_running_loop()
        capture = LoopbackCapture(loop, device_index)
        capture.start()
        app["capture"] = capture
        app["mic_playback"] = MicPlayback(loop, mic_device_index)
        app["mic_tasks"] = set()

    return on_startup


async def on_shutdown(app: web.Application) -> None:
    for task in list(app.get("mic_tasks", ())):
        task.cancel()
    await asyncio.gather(*(pc.close() for pc in app["pcs"]), return_exceptions=True)
    app["pcs"].clear()
    if "capture" in app:
        app["capture"].stop()
    if "mic_playback" in app:
        app["mic_playback"].stop()


def print_banner(app: web.Application, port: int, scheme: str) -> None:
    capture = app["capture"]
    playback = app.get("mic_playback")
    primary = get_lan_ip()
    others = [ip for ip in get_all_ipv4() if ip != primary]
    print()
    print("=" * 66)
    print(f"  PC audio  : [{capture.device['index']}] {capture.device['name']}")
    print(f"              {capture.rate} Hz, {capture.channels} ch, {CHUNK_MS} ms chunks")
    if playback is not None:
        print(f"  Phone mic : plays into [{playback.device['index']}] {playback.device['name']}")
    print("-" * 66)
    print("  Open in Safari on your iPhone (Listen / Mic toggle on the page):")
    print(f"      {scheme}://{primary}:{port}")
    if scheme == "https":
        print("      → accept the certificate warning once (self-signed; needed so")
        print("        the browser will grant microphone access for Mic mode)")
    else:
        print("      Mic mode is UNAVAILABLE without HTTPS (cert could not be made).")
    if others:
        print("  Other network IPs, if that doesn't load:")
        for ip in others:
            print(f"      {scheme}://{ip}:{port}")
    print("=" * 66)
    print("  If Windows Firewall prompts, ALLOW Python on private networks,")
    print("  otherwise the phone cannot reach this server.")
    print(flush=True)


async def run_servers(app: web.Application, port: int) -> None:
    """Serve BOTH modes from one app on a single port. Mic mode needs a secure
    context, so we serve over HTTPS (self-signed) whenever a cert is available;
    listen mode rides the same HTTPS origin. Only if a cert can't be made do we
    fall back to plain HTTP — listen still works, mic is then unavailable."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()  # fires on_startup -> creates capture + mic playback

    scheme = "http"
    ssl_ctx = None
    cert = ensure_self_signed_cert()
    if cert is not None:
        try:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=str(cert[0]), keyfile=str(cert[1]))
            scheme = "https"
        except Exception as exc:  # bad/corrupt cert
            log.warning("Could not load cert (%s); serving plain HTTP, mic mode off.", exc)
            ssl_ctx = None

    await web.TCPSite(runner, "0.0.0.0", port, ssl_context=ssl_ctx).start()
    print_banner(app, port, scheme)
    try:
        await asyncio.Event().wait()  # run forever; Ctrl+C cancels this
    finally:
        await runner.cleanup()  # fires on_shutdown


def list_loopback_devices() -> None:
    pa = pyaudio.PyAudio()
    try:
        default = pa.get_default_wasapi_loopback()
    except (OSError, LookupError):
        default = None
    print("Capturable loopback devices for --device (each mirrors one output):")
    for device in pa.get_loopback_device_info_generator():
        marker = "   <- current default output" if default and device["index"] == default["index"] else ""
        print(
            f"  [{device['index']:3d}] {device['name']}  "
            f"({int(device['defaultSampleRate'])} Hz, {device['maxInputChannels']} ch){marker}"
        )

    try:
        default_out = pa.get_default_output_device_info()
    except (OSError, LookupError):
        default_out = None
    print()
    print("Output devices for --mic-device (where the phone's mic plays; pick a")
    print("virtual cable such as VB-CABLE to turn the phone into a system mic):")
    for i in range(pa.get_device_count()):
        device = pa.get_device_info_by_index(i)
        if int(device.get("maxOutputChannels", 0)) < 1 or device.get("isLoopbackDevice"):
            continue
        marker = "   <- current default output" if default_out and device["index"] == default_out["index"] else ""
        print(
            f"  [{device['index']:3d}] {device['name']}  "
            f"({int(device['defaultSampleRate'])} Hz, {device['maxOutputChannels']} ch){marker}"
        )
    pa.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream Windows system audio to a browser via WebRTC.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port serving both modes (HTTPS if a cert is available; default {DEFAULT_PORT})")
    parser.add_argument("--device", type=int, default=None,
                        help="loopback device index to capture (default: mirror of the default output)")
    parser.add_argument("--mic-device", type=int, default=None,
                        help="output device index to play the phone's mic into (default: system default "
                             "output). Point at a virtual cable (VB-CABLE) to make the phone a real microphone.")
    parser.add_argument("--list-devices", action="store_true", help="list capture + output devices and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.list_devices:
        list_loopback_devices()
        return

    tune_windows_scheduling()
    app = web.Application()
    app["pcs"] = set()
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_post("/mic-offer", mic_offer)
    app.on_startup.append(make_on_startup(args.device, args.mic_device))
    app.on_shutdown.append(on_shutdown)
    try:
        asyncio.run(run_servers(app, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
