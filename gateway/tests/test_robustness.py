"""Robustness tests for the message board: failures, concurrency, crashes, bad input and load.

Most need the Redis from docker compose (database 15) and are skipped without it. The failure tests
put a misbehaving TCP proxy (faulty_proxy.py) between the library and Redis. About a minute in total.
"""
import collections
import json
import math
import random
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import redis
from pydantic import ValidationError

from gateway.feed import Feed, FeedUnavailable
from gateway.schema import KIND_FINAL_TEXT, FeedMessage
from gateway.tests.faulty_proxy import FaultyProxy
from gateway.tests.helpers import ROOT, URL, RedisCase, message, run_consumer, segments


def wait_until(condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class BackgroundConsumer:
    """A consumer running in a thread, recording what it handled, acking each message."""

    def __init__(self, board, feed, group="g", name="c1", **kwargs):
        self.handled, self.errors = [], []
        self.stop = threading.Event()
        self._lock = threading.Lock()
        kwargs.setdefault("block_ms", 300)
        kwargs.setdefault("start", "0")
        self._thread = threading.Thread(target=self._run, args=(board, feed, group, name, kwargs), daemon=True)
        self._thread.start()

    def _run(self, board, feed, group, name, kwargs):
        try:
            for delivery in board.consume(feed, group, name, stop=self.stop, **kwargs):
                with self._lock:
                    self.handled.append((delivery.message.session, delivery.message.segment))
                try:
                    delivery.ack()
                except FeedUnavailable:
                    pass  # it is handed out again later
        except Exception as e:  # anything escaping consume() is a failure of the library
            self.errors.append(e)

    def segments(self):
        with self._lock:
            return [segment for _, segment in self.handled]

    def close(self, seconds=5):
        self.stop.set()
        self._thread.join(seconds)
        return not self._thread.is_alive()


# ---------------------------------------------------------------- failures between the library and Redis

class ProxyCase(RedisCase):
    def setUp(self):
        super().setUp()
        self.proxy = FaultyProxy()
        self.addCleanup(self.proxy.close)

    def proxied(self, **kwargs):
        kwargs.setdefault("socket_timeout", 1.0)
        return Feed.from_url(self.proxy.url(), **kwargs)


class TestRedisFailures(ProxyCase):
    def test_publish_fails_fast_while_redis_is_down_and_works_again_after(self):
        board = self.proxied()
        board.publish(self.feed, message(0))
        self.proxy.down()
        started = time.monotonic()
        with self.assertRaises(FeedUnavailable):
            board.publish(self.feed, message(1))
        self.assertLess(time.monotonic() - started, 1.5, "publish must not stall the caller")
        self.proxy.up()
        board.publish(self.feed, message(2))
        self.assertEqual(self.board.client.xlen(self.feed), 2)

    def test_publish_gives_up_within_its_timeout_when_redis_hangs(self):
        board = self.proxied(socket_timeout=1.0)
        board.publish(self.feed, message(0))
        self.proxy.silent()
        started = time.monotonic()
        with self.assertRaises(FeedUnavailable):
            board.publish(self.feed, message(1))
        self.assertLess(time.monotonic() - started, 2.5, "publish must give up after about one socket timeout")
        self.proxy.up()
        board.publish(self.feed, message(2))
        # The timed-out publish may still reach Redis once the connection drains; a consumer skips the repeat
        # and sees each message at most once.
        got = segments(self.consume(3, ack=True, seconds=2))
        self.assertIn(0, got)
        self.assertIn(2, got)
        self.assertEqual(len(got), len(set(got)))

    def test_a_consumer_survives_repeated_connection_drops_and_loses_nothing(self):
        consumer = BackgroundConsumer(self.proxied(socket_timeout=2.0), self.feed)
        try:
            for i in range(200):
                self.board.publish(self.feed, message(i))
                if i % 20 == 10:
                    self.proxy.cut()
                time.sleep(0.005)
            self.assertTrue(wait_until(lambda: len(set(consumer.segments())) >= 200, 40),
                            f"only {len(set(consumer.segments()))} of 200 handled; errors: {consumer.errors!r}")
        finally:
            self.assertTrue(consumer.close())
        self.assertEqual(consumer.errors, [])
        handled = consumer.segments()
        self.assertEqual(set(handled), set(range(200)))
        # a drop between handling and acking can hand one message out again, never more per drop
        self.assertLessEqual(len(handled) - len(set(handled)), self.proxy.cuts)

    def test_a_consumer_waits_out_a_hung_redis_and_carries_on(self):
        consumer = BackgroundConsumer(self.proxied(socket_timeout=1.0), self.feed)
        try:
            self.board.publish(self.feed, message(1))
            self.assertTrue(wait_until(lambda: consumer.segments() == [1], 5))
            self.proxy.silent()
            self.board.publish(self.feed, message(2))     # published while the consumer's link hangs
            time.sleep(2.5)
            self.proxy.up()
            self.assertTrue(wait_until(lambda: 2 in consumer.segments(), 15), "message 2 never arrived")
        finally:
            self.assertTrue(consumer.close())
        self.assertEqual(consumer.errors, [])

    def test_stop_ends_a_consumer_quickly_even_while_redis_is_down(self):
        board = self.proxied(socket_timeout=1.0)
        self.proxy.down()
        stop = threading.Event()
        thread = threading.Thread(target=lambda: list(board.consume(self.feed, "g", "c", block_ms=300, stop=stop)))
        thread.start()
        time.sleep(4)                                     # long enough for the retry wait to have grown
        stop.set()
        started = time.monotonic()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 1.0, "a service must be able to shut down during an outage")

    def test_ack_during_an_outage_raises_feed_unavailable_and_the_message_comes_back(self):
        board = self.proxied(socket_timeout=1.0)
        self.board.publish(self.feed, message(1))
        [delivery] = run_consumer(board, self.feed, "g", "c1", 1, start="0")
        self.proxy.down()
        with self.assertRaises(FeedUnavailable):
            delivery.ack()
        self.proxy.up()
        [again] = run_consumer(board, self.feed, "g", "c1", 1, start="0", ack=True)
        self.assertEqual(again.message.segment, 1)
        self.assertEqual(self.pending(), 0)

    def test_redis_refusing_writes_counts_as_unavailable(self):
        for reply in ("OOM command not allowed when used memory > 'maxmemory'.",
                      "MISCONF Redis is configured to save RDB snapshots, but it's currently unable to persist to disk.",
                      "READONLY You can't write against a read only replica."):
            with self.subTest(reply=reply.split()[0]):
                with patch.object(self.board.client, "xadd", side_effect=redis.ResponseError(reply)):
                    with self.assertRaises(FeedUnavailable):
                        self.board.publish(self.feed, message(1))

    def test_a_consumer_waits_while_redis_is_loading_or_full_and_carries_on(self):
        # after a restart Redis answers LOADING until its data is read back; when full it refuses writes
        # (XREADGROUP counts as one) with OOM. Neither may kill a consumer.
        for reply in ("LOADING Redis is loading the dataset in memory",
                      "OOM command not allowed when used memory > 'maxmemory'."):
            with self.subTest(reply=reply.split()[0]):
                feed = f"{self.feed}-{reply.split()[0]}"
                self.addCleanup(self.board.client.delete, feed)
                self.board.publish(feed, message(1))
                real, calls = self.board.client.xreadgroup, {"n": 0}

                def refusing(*args, **kwargs):
                    calls["n"] += 1
                    if calls["n"] <= 2:
                        raise redis.ResponseError(reply)
                    return real(*args, **kwargs)

                with patch.object(self.board.client, "xreadgroup", side_effect=refusing):
                    got = run_consumer(self.board, feed, "g", "c1", 1, ack=True, start="0")
                self.assertEqual(segments(got), [1])

    def test_ack_while_redis_is_full_raises_feed_unavailable(self):
        self.board.publish(self.feed, message(1))
        [delivery] = self.consume(1)
        with patch.object(self.board.client, "pipeline", side_effect=redis.ResponseError("OOM command not allowed")):
            with self.assertRaises(FeedUnavailable):
                delivery.ack()

    def test_a_client_that_returns_bytes_is_refused_up_front(self):
        with self.assertRaises(ValueError):
            Feed(redis.Redis.from_url(URL))                # without decode_responses every message would look invalid

    def test_a_feed_name_used_by_something_else_is_an_error_not_an_outage(self):
        self.board.client.set(self.feed, "not a stream")
        with self.assertRaises(redis.ResponseError) as caught:
            self.board.publish(self.feed, message(1))
        self.assertNotIsInstance(caught.exception, FeedUnavailable)


# ---------------------------------------------------------------- many things at once

class TestConcurrency(RedisCase):
    def test_many_publishers_and_consumers_at_once_lose_nothing_and_repeat_nothing(self):
        publishers, per = 8, 250
        consumers = [BackgroundConsumer(self.board, self.feed, name=f"c{i}", batch=5) for i in range(3)]

        def publish(p):
            for i in range(per):
                self.board.publish(self.feed, message(i, session=f"pub-{p}"))

        threads = [threading.Thread(target=publish, args=(p,)) for p in range(publishers)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            total = publishers * per
            self.assertTrue(wait_until(lambda: sum(len(c.handled) for c in consumers) >= total, 60))
            time.sleep(0.5)                               # give any repeat time to show up
        finally:
            for c in consumers:
                self.assertTrue(c.close())
        handled = collections.Counter(item for c in consumers for item in c.handled)
        expected = collections.Counter((f"pub-{p}", i) for p in range(publishers) for i in range(per))
        self.assertEqual(handled, expected)
        self.assertEqual([e for c in consumers for e in c.errors], [])

    def test_one_consumer_sees_each_publishers_messages_in_order(self):
        def publish(p):
            for i in range(200):
                self.board.publish(self.feed, message(i, session=f"pub-{p}"))

        threads = [threading.Thread(target=publish, args=(p,)) for p in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = self.consume(800, ack=True, seconds=30)
        by_session = collections.defaultdict(list)
        for d in got:
            by_session[d.message.session].append(d.message.segment)
        self.assertEqual(len(got), 800)
        for session, order in by_session.items():
            self.assertEqual(order, list(range(200)), session)

    def test_twenty_groups_each_see_every_message_in_order(self):
        for i in range(100):
            self.board.publish(self.feed, message(i))
        for g in range(20):
            self.assertEqual(segments(self.consume(100, group=f"group-{g}", ack=True)), list(range(100)))


# ---------------------------------------------------------------- processes killed at random

class TestChaos(RedisCase):
    def start_worker(self, name, key):
        return subprocess.Popen(
            [sys.executable, "-m", "gateway.tests.consumer_worker", URL, self.feed, "g", name, key, "0.03", "3000", "5"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_workers_killed_at_random_still_handle_every_message(self):
        total, key, names = 300, f"handled-{self.feed}", ["w0", "w1", "w2"]
        for i in range(total):
            self.board.publish(self.feed, message(i))
        handled = lambda: [int(x) for x in self.board.client.lrange(key, 0, -1)]
        workers = {name: self.start_worker(name, key) for name in names}
        rng, kills = random.Random(7), 0
        try:
            deadline = time.monotonic() + 90
            while len(set(handled())) < total and time.monotonic() < deadline:
                time.sleep(0.4)
                if kills < 8:
                    name = rng.choice(names)
                    workers[name].send_signal(signal.SIGKILL)
                    workers[name].wait()
                    kills += 1
                    workers[name] = self.start_worker(name, key)   # restarted under the same name
        finally:
            for w in workers.values():
                w.send_signal(signal.SIGKILL)
                w.wait()
        done = handled()
        self.assertEqual(set(done), set(range(total)), f"{total - len(set(done))} messages never handled")
        self.assertEqual(kills, 8)
        self.assertLessEqual(len(done) - len(set(done)), kills, "more repeats than kills")


# ---------------------------------------------------------------- bad and unusual input

VALID = json.loads(message(0).to_json())


def garbage_entries():
    """Makers of feed entries that are not valid messages, each taking a random generator."""
    return [
        lambda r: {b"data": bytes(r.randrange(256) for _ in range(r.randrange(1, 64)))},     # random bytes, mostly not UTF-8
        lambda r: {b"data": "".join(chr(r.randrange(32, 0x2FFF)) for _ in range(30)).encode()},
        lambda r: {b"data": json.dumps(r.choice([[], 1, "x", None, True, 3.5])).encode()},
        lambda r: {b"data": json.dumps({"kind": "final_text", "segment": "x"}).encode()},
        lambda r: {b"data": json.dumps(dict(VALID, segment=-1)).encode()},
        lambda r: {b"data": json.dumps(dict(VALID, made_at="yesterday")).encode()},
        lambda r: {b"data": json.dumps(VALID).encode() + b"trailing"},
        lambda r: {b"data": b"{" * 5000},
        lambda r: {b"data": b"x" * 200_000},
        lambda r: {b"data": b""},
        lambda r: {b"other": b"1"},
    ]


class TestBadInput(RedisCase):
    def test_bytes_that_are_not_utf8_do_not_crash_consumers(self):
        redis.Redis.from_url(URL).xadd(self.feed, {b"data": b"\xff\xfe\xfa"})
        self.board.publish(self.feed, message(1))
        got = self.consume(1, ack=True)
        self.assertEqual(segments(got), [1])
        self.assertEqual(self.pending(), 0)

    def test_random_garbage_mixed_into_a_feed_is_skipped_and_order_is_kept(self):
        raw, rng, makers = redis.Redis.from_url(URL), random.Random(3), garbage_entries()
        expected, garbage = [], 0
        for _ in range(300):
            if rng.random() < 0.35:
                self.board.publish(self.feed, message(len(expected)))
                expected.append(len(expected))
            else:
                raw.xadd(self.feed, rng.choice(makers)(rng))
                garbage += 1
        self.board.publish(self.feed, message(len(expected)))   # a last valid message, so all garbage is read
        expected.append(len(expected))
        got = self.consume(len(expected), ack=True, seconds=30)
        self.assertEqual(segments(got), expected)
        self.assertEqual(self.board.skipped_invalid, garbage)
        self.assertEqual(self.pending(), 0)

    def test_messages_trimmed_away_while_pending_do_not_break_recovery(self):
        for i in range(20):
            self.board.publish(self.feed, message(i))
        self.consume(20)                                   # handed out, never acked
        self.board.client.xtrim(self.feed, maxlen=0, approximate=False)
        for i in range(20, 23):
            self.board.publish(self.feed, message(i))
        got = self.consume(3, ack=True)                   # the same consumer, restarted
        self.assertEqual(segments(got), [20, 21, 22])
        self.assertEqual(self.board.skipped_invalid, 20)
        self.assertEqual(self.pending(), 0)

    def test_large_and_unusual_text_arrives_exactly_as_sent(self):
        texts = [
            "x" * 1_000_000,
            "",
            "日本語のテキスト、句読点。「引用」",
            "emoji 👩🏽‍💻🇯🇵 and combining é",
            'quotes " \' backslash \\ newline \n tab \t',
            "nul \x00 bell \x07",
            "مرحبا right to left",
            '{"kind": "final_text"} looks like JSON',
        ]
        for i, text in enumerate(texts):
            self.board.publish(self.feed, message(i, text=text))
        got = self.consume(len(texts), ack=True)
        self.assertEqual([d.message.text for d in got], texts)

    def test_names_containing_separators_do_not_collide(self):
        # group "g" + kind "a:b" and group "g:a" + kind "b" once mapped to the same repeat-skipping key
        one = FeedMessage.new(kind="a:b", session="c", segment=1, start=0, end=1, lang="en", text="one", source="t")
        two = FeedMessage.new(kind="b", session="c", segment=1, start=0, end=1, lang="en", text="two", source="t")
        self.board.publish(self.feed, one)
        self.board.publish(self.feed, two)
        self.assertEqual(len(self.consume(2, group="g:a", ack=True)), 2)
        self.assertEqual([d.message.text for d in self.consume(2, group="g", ack=True)], ["one", "two"])

    def test_the_same_message_in_two_feeds_is_not_a_repeat(self):
        # two feeds may each be read by a group with the same name; handling one must not skip the other
        other = f"{self.feed}-other"
        self.addCleanup(self.board.client.delete, other)
        self.board.publish(self.feed, message(1))
        self.board.publish(other, message(1))
        self.assertEqual(segments(self.consume(1, ack=True)), [1])
        self.assertEqual(segments(run_consumer(self.board, other, "g", "c1", 1, ack=True, start="0")), [1])

    def test_acking_twice_or_after_a_trim_is_harmless(self):
        self.board.publish(self.feed, message(1))
        [d] = self.consume(1)
        d.ack()
        d.ack()
        self.board.publish(self.feed, message(2))
        [e] = self.consume(1)
        self.board.client.xtrim(self.feed, maxlen=0, approximate=False)
        e.ack()
        self.assertEqual(self.pending(), 0)

    def test_repeat_protection_ends_after_its_time_limit(self):
        board = Feed.from_url(URL, dedupe_ttl_s=1)
        board.publish(self.feed, message(5))
        run_consumer(board, self.feed, "g", "c1", 1, ack=True, start="0")
        time.sleep(1.3)
        board.publish(self.feed, message(5))              # the same message, long after
        self.assertEqual(segments(run_consumer(board, self.feed, "g", "c1", 1, ack=True, start="0")), [5])


# ---------------------------------------------------------------- the message shape under fuzzing

TRICKY = [None, "", " ", "x", "日本", "42", "-1", "1e400", "NaN", 0, -1, 1, 2 ** 63, 2 ** 80, 0.5, -0.0, 1e308,
          float("inf"), float("-inf"), float("nan"), True, False, [], {}, [1], {"a": 1},
          "2026-10-01T10:15:03.120Z", "2026-10-01T10:15:03", "2026-02-30T00:00:00Z", "2026-10-01T10:15:03+09:00"]
FIELDS = ["version", "kind", "session", "segment", "start", "end", "lang", "text", "made_at", "source"]


def strict_json(text):
    """Parse JSON the way a strict reader in another language would: no NaN or Infinity."""
    def refuse(constant):
        raise ValueError(f"not JSON: {constant}")
    return json.loads(text, parse_constant=refuse)


class TestSchemaFuzz(unittest.TestCase):
    def test_infinite_and_missing_numbers_are_rejected(self):
        for name in ("start", "end"):
            for value in (float("inf"), float("nan")):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValidationError):
                        FeedMessage.model_validate(dict(VALID, **{name: value, "end": 1e9 if name == "start" else value}))

    def test_fuzzed_fields_either_fail_validation_or_round_trip_exactly(self):
        rng = random.Random(11)
        accepted = 0
        for _ in range(3000):
            fields = dict(VALID)
            for name in rng.sample(FIELDS, rng.randrange(1, 4)):
                fields[name] = rng.choice(TRICKY)
            try:
                m = FeedMessage.model_validate(fields)
            except ValidationError:
                continue
            accepted += 1
            text = m.to_json()
            strict_json(text)                              # what we write must be readable by any JSON parser
            self.assertEqual(FeedMessage.from_json(text), m)
        self.assertGreater(accepted, 100)                  # the fuzzing did reach the accepting side

    def test_random_bytes_only_ever_raise_validation_errors(self):
        rng = random.Random(5)
        samples = [bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200))) for _ in range(2000)]
        samples += [b"{" * 100_000, b"[" * 100_000, b'{"version":' * 1000, b"\xef\xbb\xbf{}"]
        for data in samples:
            try:
                FeedMessage.from_json(data)
            except ValidationError:
                pass


# ---------------------------------------------------------------- load and idling

class TestLoad(RedisCase):
    def test_throughput_is_far_above_what_live_speech_needs(self):
        # live speech produces about one finished sentence every few seconds per session
        started = time.monotonic()
        for i in range(2000):
            self.board.publish(self.feed, message(i))
        published = time.monotonic() - started
        started = time.monotonic()
        got = self.consume(2000, ack=True, seconds=60, batch=100)
        consumed = time.monotonic() - started
        self.assertEqual(len(got), 2000)
        self.assertLess(published, 10, f"publishing 2000 took {published:.1f} s")
        self.assertLess(consumed, 20, f"consuming 2000 took {consumed:.1f} s")
        print(f"\n  publish {2000 / published:.0f}/s, consume and ack {2000 / consumed:.0f}/s")

    def test_an_idle_consumer_waits_quietly(self):
        with self.assertNoLogs("feed", level="WARNING"):
            self.assertEqual(self.consume(1, seconds=6), [])


if __name__ == "__main__":
    unittest.main()
