"""Shared pieces for the feed tests: a Redis-backed test case, a message maker and a bounded consumer."""
import os
import threading
import unittest
import uuid
from pathlib import Path

import redis

from gateway.feed import Feed
from gateway.schema import KIND_FINAL_TEXT, FeedMessage

URL = os.environ.get("REDIS_TEST_URL", "redis://localhost:6379/15")
ROOT = Path(__file__).resolve().parents[2]


def message(segment, session="s-1", kind=KIND_FINAL_TEXT, text=None):
    return FeedMessage.new(kind=kind, session=session, segment=segment, start=0.0, end=1.0, lang="en",
                           text=text if text is not None else f"text {segment}", source="test")


def run_consumer(board, feed, group, consumer, count, seconds=10, ack=False, **kwargs):
    """Consume in this thread until ``count`` messages arrived or ``seconds`` passed; return the deliveries."""
    stop = threading.Event()
    timer = threading.Timer(seconds, stop.set)
    timer.start()
    kwargs.setdefault("block_ms", 200)
    out = []
    try:
        for delivery in board.consume(group, consumer, feed=feed, stop=stop, **kwargs):
            out.append(delivery)
            if ack:
                delivery.ack()
            if len(out) >= count:
                break
    finally:
        timer.cancel()
        stop.set()
    return out


def segments(deliveries):
    return [d.message.segment for d in deliveries]


class RedisCase(unittest.TestCase):
    """A test with its own feed name in the test database, skipped when Redis is not running."""

    def setUp(self):
        self.board = Feed.from_url(URL)
        try:
            self.board.client.ping()
        except redis.RedisError:
            self.skipTest("no Redis at " + URL)
        self.feed = f"test-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        if hasattr(self, "feed"):
            client = redis.Redis.from_url(URL)
            keys = [self.feed] + client.keys("done:*") + client.keys("handled-*")
            client.delete(*keys)

    def consume(self, count, group="g", consumer="c1", **kwargs):
        kwargs.setdefault("start", "0")
        return run_consumer(self.board, self.feed, group, consumer, count, **kwargs)

    def pending(self, group="g"):
        return self.board.client.xpending(self.feed, group)["pending"]
