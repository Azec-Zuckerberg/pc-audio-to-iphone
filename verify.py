"""verify.py — automated health check for server.py, no phone required.

Connects to the local server exactly like the browser would, then measures the
stream during two phases: (A) while a real tone plays on the PC and (B) while
the PC is silent. Run it whenever you touch the scheduler in server.py.

Healthy output for BOTH phases (anything else is a regression):
    ~50 pkt/s | media span == wall span | pts gaps: 0

Usage (with server.py already running):
    python verify.py [--port 8080]
"""

import argparse
import asyncio
import math
import os
import struct
import tempfile
import time
import wave
import winsound

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

PHASE_S = 4


def make_tone_wav(path: str) -> None:
    """2 s of 440 Hz stereo sine at 48 kHz, looped during the tone phase."""
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        data = bytearray()
        for i in range(48000 * 2):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / 48000))
            data += struct.pack("<hh", v, v)
        w.writeframes(bytes(data))


def report(name: str, rows: list) -> bool:
    if len(rows) < 10:
        print(f"{name}: too few frames ({len(rows)}) -- FAIL")
        return False
    wall = rows[-1][0] - rows[0][0]
    rate = rows[-1][3]
    media = (rows[-1][1] - rows[0][1]) / rate
    gaps = sum(1 for a, b in zip(rows, rows[1:]) if b[1] - a[1] != a[2])
    iat = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]))
    healthy = gaps == 0 and abs(wall - media) < 0.25 and len(rows) / wall > 45
    print(
        f"{name}: {len(rows)} frames | {len(rows) / wall:5.1f} pkt/s | "
        f"wall {wall:.2f}s vs media {media:.2f}s | pts gaps: {gaps} | "
        f"inter-arrival p95 {iat[int(len(iat) * 0.95)] * 1000:.0f} ms, "
        f"max {iat[-1] * 1000:.0f} ms | {'OK' if healthy else 'FAIL'}"
    )
    return healthy


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    tone = os.path.join(tempfile.gettempdir(), "verify_tone.wav")
    make_tone_wav(tone)

    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="recvonly")
    frames = []
    started = asyncio.Event()
    closing = False

    @pc.on("track")
    def on_track(track):
        async def consume():
            started.set()
            while not closing:
                try:
                    f = await track.recv()
                except Exception:
                    return
                frames.append((time.monotonic(), f.pts, f.samples, f.sample_rate))

        asyncio.ensure_future(consume())

    await pc.setLocalDescription(await pc.createOffer())
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{args.port}/offer",
            json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
        ) as resp:
            assert resp.status == 200, f"HTTP {resp.status}"
            answer = await resp.json()
    await pc.setRemoteDescription(RTCSessionDescription(**answer))
    await asyncio.wait_for(started.wait(), timeout=20)
    await asyncio.sleep(1)  # let the connection settle

    winsound.PlaySound(tone, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
    t_tone = time.monotonic()
    await asyncio.sleep(PHASE_S)
    t_split = time.monotonic()
    winsound.PlaySound(None, winsound.SND_PURGE)
    await asyncio.sleep(PHASE_S)
    t_end = time.monotonic()

    closing = True
    snapshot = list(frames)
    await pc.close()
    os.remove(tone)

    ok_tone = report("TONE   ", [r for r in snapshot if t_tone + 0.5 <= r[0] < t_split - 0.1])
    ok_silence = report("SILENCE", [r for r in snapshot if t_split + 0.5 <= r[0] < t_end - 0.1])
    return 0 if (ok_tone and ok_silence) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
