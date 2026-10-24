"""Replay a recording against the engine at real speed and log when every message arrives.

    python bench/replay.py bench/audio/en/en_00.wav --language en
"""
import argparse
import json
import threading
import time
import urllib.request

import numpy as np
import soundfile as sf
import soxr
from websockets.sync.client import connect

SAMPLE_RATE = 16000


class EngineBusy(RuntimeError):
    """The engine refused the session because all seats are taken."""


def read_wav(path):
    audio, rate = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != SAMPLE_RATE:
        audio = soxr.resample(audio, rate, SAMPLE_RATE)
    return audio


class BacklogPoller(threading.Thread):
    """Samples the engine's /health once a second and keeps the highest backlog seen."""

    def __init__(self, health_url):
        super().__init__(daemon=True)
        self.health_url = health_url
        self.max_backlog_s = 0.0
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.wait(1.0):
            try:
                with urllib.request.urlopen(self.health_url, timeout=2) as response:
                    self.max_backlog_s = max(self.max_backlog_s, json.load(response)["backlog_seconds"])
            except Exception:
                pass

    def stop(self):
        self._stop_event.set()


def replay(wav_path, language, host="localhost", port=9090, health_url="http://localhost:8000/health",
           model="base", chunk_ms=100, tail_silence_s=6.0, uid="bench"):
    """Stream one recording and return what the engine sent back, with arrival times.

    Time zero is the moment the first chunk is sent, so a message arriving at ``t`` has had
    ``t`` seconds of audio to work with. The recording is followed by silence so the engine
    can settle its last sentence, then ``END_OF_AUDIO``.

    Returns:
        dict: ``events`` (list of ``{"t": seconds, "segments": [...]}``), ``audio_s``
        (recording length) and ``max_backlog_s`` (highest engine backlog sampled).
    """
    audio = read_wav(wav_path)
    tail = np.zeros(int(tail_silence_s * SAMPLE_RATE), dtype=np.float32)
    stream = np.concatenate([audio, tail])
    chunk = int(chunk_ms / 1000 * SAMPLE_RATE)

    events = []
    poller = BacklogPoller(health_url)
    with connect(f"ws://{host}:{port}", max_size=None) as ws:
        ws.send(json.dumps({"uid": uid, "language": language, "task": "transcribe",
                            "model": model, "use_vad": True}))
        while True:
            reply = json.loads(ws.recv(timeout=30))
            if reply.get("status") == "WAIT":
                raise EngineBusy(f"engine is full, retry in {reply.get('message')} min")
            if reply.get("status") == "ERROR":
                raise RuntimeError(reply.get("message"))
            if reply.get("message") == "SERVER_READY":
                break

        t0 = time.monotonic()

        def receive():
            for raw in ws:
                message = json.loads(raw)
                if "segments" in message:
                    events.append({"t": time.monotonic() - t0, "segments": message["segments"]})

        receiver = threading.Thread(target=receive, daemon=True)
        receiver.start()
        poller.start()
        for i, start in enumerate(range(0, len(stream), chunk)):
            delay = t0 + i * chunk_ms / 1000 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            ws.send(stream[start:start + chunk].tobytes())
        ws.send(b"END_OF_AUDIO")
        receiver.join(timeout=10)
    poller.stop()
    return {"events": events, "audio_s": len(audio) / SAMPLE_RATE, "max_backlog_s": poller.max_backlog_s}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wav")
    parser.add_argument("--language", default="en")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--health-url", default="http://localhost:8000/health")
    args = parser.parse_args()
    result = replay(args.wav, args.language, args.host, args.port, args.health_url)
    for event in result["events"]:
        last = event["segments"][-1]
        print(f"{event['t']:6.2f}s  [{last['start']}-{last['end']}] {last['text']}")
    print(f"max backlog {result['max_backlog_s']:.2f}s")


if __name__ == "__main__":
    main()
