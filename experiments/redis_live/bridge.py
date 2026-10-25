"""Live audio into Redis: send the microphone (or a WAV file) to the engine and publish every finished
sentence to the final-text feed, the way the Phase 4 gateway will.

    python -m experiments.redis_live.bridge --language en               # microphone, Ctrl+C to stop
    python -m experiments.redis_live.bridge --language ja --file talk.wav  # a recording, at real speed

Each run is one session (``s-live-xxxxxxxx``); its finished sentences are numbered 0, 1, 2, ... in order.
When the run stops, the sentence still being spoken is published too, since the session is over.
If Redis is down the text still shows here and the bridge carries on; those sentences are not published.
"""
import argparse
import contextlib
import json
import os
import sys
import threading
import time
import urllib.request
import uuid

import numpy as np
from websockets.sync.client import connect

from gateway.feed import Feed, FeedUnavailable
from gateway.schema import KIND_FINAL_TEXT, FeedMessage

RATE = 16000


@contextlib.contextmanager
def quiet_stderr():
    """Hide the ALSA and JACK messages PortAudio prints while it looks for devices."""
    sys.stderr.flush()
    saved = os.dup(2)
    try:
        with open(os.devnull, "w") as devnull:
            os.dup2(devnull.fileno(), 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def microphone(chunk):
    """Yield chunks of 16 kHz mono float32 audio from the default input until interrupted."""
    import pyaudio
    with quiet_stderr():
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paFloat32, channels=1, rate=RATE, input=True, frames_per_buffer=chunk)
    try:
        while True:
            yield stream.read(chunk, exception_on_overflow=False)
    finally:
        stream.close()
        pa.terminate()


def wav_file(path, chunk):
    """Yield chunks of a recording, paced at real speed, then a little silence so the last sentence settles."""
    import soundfile as sf
    audio, rate = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != RATE:
        import soxr
        audio = soxr.resample(audio, rate, RATE).astype(np.float32)
    audio = np.concatenate([audio, np.zeros(3 * RATE, dtype=np.float32)])
    started = time.monotonic()
    for i, start in enumerate(range(0, len(audio), chunk)):
        delay = started + i * chunk / RATE - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        yield audio[start:start + chunk].tobytes()


class Publisher:
    """Numbers the engine's finished sentences and publishes each one to final-text exactly once."""

    def __init__(self, board, session, lang, source):
        self.board, self.session, self.lang, self.source = board, session, lang, source
        self.seen = set()
        self.next_segment = 0
        self.published = self.failed = 0
        self.draft = None
        self.lock = threading.Lock()

    def on_segments(self, segments):
        with self.lock:
            for seg in segments:
                key = (seg["start"], seg["end"])
                if seg.get("completed") and key not in self.seen and seg["text"].strip():
                    self.seen.add(key)
                    self._publish(seg)
            last = segments[-1]
            self.draft = None if last.get("completed") else last

    def finish(self):
        """The session is over: what was still a draft is final now."""
        with self.lock:
            if self.draft and self.draft["text"].strip() and (self.draft["start"], self.draft["end"]) not in self.seen:
                self._publish(self.draft, note=" (end of session)")

    def _publish(self, seg, note=""):
        segment, self.next_segment = self.next_segment, self.next_segment + 1
        message = FeedMessage.new(kind=KIND_FINAL_TEXT, session=self.session, segment=segment,
                                  start=float(seg["start"]), end=max(float(seg["start"]), float(seg["end"])),
                                  lang=self.lang, text=seg["text"].strip(), source=self.source)
        t0 = time.monotonic()
        try:
            self.board.publish(message)
            self.published += 1
            print(f"  #{segment} -> final-text in {1000 * (time.monotonic() - t0):.1f} ms{note}: {message.text}", flush=True)
        except FeedUnavailable as e:
            self.failed += 1
            print(f"  #{segment} NOT published (Redis unavailable: {e}){note}: {message.text}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--language", default="en")
    parser.add_argument("--file", help="replay this recording at real speed instead of the microphone")
    parser.add_argument("--engine", default="ws://localhost:9090")
    parser.add_argument("--health-url", default="http://localhost:8000/health")
    parser.add_argument("--redis", default=None, help="Redis URL (default: $REDIS_URL or redis://localhost:6379/0)")
    args = parser.parse_args()

    with urllib.request.urlopen(args.health_url, timeout=5) as response:
        engine = json.load(response)
    if not engine.get("ready"):
        raise SystemExit("The engine is still loading its model; try again in a few seconds.")
    board = Feed.from_url(args.redis)
    board.ensure_feeds()
    session = f"s-live-{uuid.uuid4().hex[:8]}"
    publisher = Publisher(board, session, args.language, f"speech-engine@{engine['backend']}")
    print(f"engine: {engine['backend']} ({engine['model']}); session {session}; publishing to final-text")

    chunk = RATE // 10
    with connect(args.engine, max_size=None) as ws:
        ws.send(json.dumps({"uid": session, "language": args.language, "task": "transcribe",
                            "model": "base", "use_vad": True}))
        while True:
            reply = json.loads(ws.recv(timeout=30))
            if reply.get("status") == "WAIT":
                raise SystemExit("The engine is busy with another session; try again later.")
            if reply.get("message") == "SERVER_READY":
                break

        def receive():
            for raw in ws:
                message = json.loads(raw)
                if message.get("segments"):
                    publisher.on_segments(message["segments"])

        receiver = threading.Thread(target=receive, daemon=True)
        receiver.start()
        print("speak now; Ctrl+C to stop\n" if not args.file else f"replaying {args.file}\n", flush=True)
        try:
            for data in (wav_file(args.file, chunk) if args.file else microphone(chunk)):
                ws.send(data)
        except KeyboardInterrupt:
            pass
        ws.send(b"END_OF_AUDIO")
        receiver.join(timeout=3)
    publisher.finish()
    print(f"\nsession {session}: {publisher.published} sentences published to final-text, {publisher.failed} not published")
    return 0


if __name__ == "__main__":
    sys.exit(main())
