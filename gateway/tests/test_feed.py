"""Feed tests against a real Redis (the compose service: docker compose up -d redis).

They use database 15 and feed names of their own, and skip when Redis cannot be reached.
Set REDIS_TEST_URL to use another server.
"""
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import redis

from gateway.feed import Feed, FeedUnavailable
from gateway.schema import KIND_FINAL_TEXT, KIND_TRANSLATION
from gateway.tests.helpers import ROOT, URL, RedisCase, message, run_consumer, segments


class TestPublishAndConsume(RedisCase):
    def test_a_message_goes_through_the_feed_unchanged(self):
        sent = message(7, text="こんにちは")
        self.board.publish(self.feed, sent)
        [delivery] = self.consume(1)
        self.assertEqual(delivery.message, sent)
        delivery.ack()
        self.assertEqual(self.pending(), 0)

    def test_every_group_sees_every_message(self):
        for i in range(5):
            self.board.publish(self.feed, message(i))
        for group in ("translators", "exporters"):
            self.assertEqual(segments(self.consume(5, group=group)), [0, 1, 2, 3, 4])

    def test_consumers_in_a_group_share_the_work(self):
        for i in range(20):
            self.board.publish(self.feed, message(i))
        handled, lock = [], threading.Lock()

        def work(name):
            for d in run_consumer(self.board, self.feed, "g", name, count=20, seconds=3, ack=True, start="0", batch=3):
                with lock:
                    handled.append(d.message.segment)

        threads = [threading.Thread(target=work, args=(name,)) for name in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(handled), list(range(20)))

    def test_a_group_created_at_the_end_ignores_older_messages(self):
        self.board.publish(self.feed, message(1))
        self.board.create_group(self.feed, "g", "$")
        self.board.publish(self.feed, message(2))
        self.assertEqual(segments(self.consume(2, seconds=1.5)), [2])


class TestNothingIsLostOrRepeated(RedisCase):
    def test_unacked_messages_come_back_first_after_a_restart(self):
        for i in range(3):
            self.board.publish(self.feed, message(i))
        self.consume(2)                                  # handed out, never acked
        self.assertEqual(segments(self.consume(3)), [0, 1, 2])   # same consumer name: a restarted service

    def test_acked_messages_are_not_handed_out_again(self):
        for i in range(4):
            self.board.publish(self.feed, message(i))
        self.consume(2, ack=True)
        self.assertEqual(segments(self.consume(4, seconds=1.5)), [2, 3])

    def test_another_consumer_takes_over_a_dead_ones_message(self):
        self.board.publish(self.feed, message(1))
        self.consume(1, consumer="dead")                 # never acked: the consumer died
        time.sleep(0.3)
        [adopted] = self.consume(1, consumer="alive", claim_idle_ms=100, claim_every_s=0, ack=True)
        self.assertEqual(adopted.message.segment, 1)
        self.assertEqual(self.pending(), 0)

    def test_a_repeat_of_a_handled_message_is_skipped(self):
        self.board.publish(self.feed, message(5))
        self.board.publish(self.feed, message(5))        # same session, segment and kind
        got = self.consume(2, ack=True, seconds=1.5)
        self.assertEqual(segments(got), [5])
        self.assertEqual(self.board.skipped_repeats, 1)
        self.assertEqual(self.pending(), 0)

    def test_a_translation_of_a_segment_is_not_a_repeat_of_its_text(self):
        self.board.publish(self.feed, message(5))
        self.board.publish(self.feed, message(5, kind=KIND_TRANSLATION))
        got = self.consume(2, ack=True)
        self.assertEqual([d.message.kind for d in got], [KIND_FINAL_TEXT, KIND_TRANSLATION])

    def test_without_repeat_protection_a_repeat_is_delivered(self):
        board = Feed.from_url(URL, dedupe_ttl_s=0)
        board.publish(self.feed, message(5))
        board.publish(self.feed, message(5))
        got = run_consumer(board, self.feed, "g", "c1", 2, ack=True, start="0")
        self.assertEqual(segments(got), [5, 5])

    def test_garbage_in_the_feed_is_skipped_and_does_not_block_it(self):
        self.board.client.xadd(self.feed, {"data": "not json"})
        self.board.client.xadd(self.feed, {"other": "field"})
        self.board.client.xadd(self.feed, {"data": '{"kind": "final_text"}'})
        self.board.publish(self.feed, message(9))
        got = self.consume(1, ack=True)
        self.assertEqual(segments(got), [9])
        self.assertEqual(self.board.skipped_invalid, 3)
        self.assertEqual(self.pending(), 0)


class TestKillAndRestart(RedisCase):
    """The check from the plan: kill a consumer mid-stream, restart it, and it carries on."""

    def start_worker(self, handled_key, delay=0.05):
        return subprocess.Popen(
            [sys.executable, "-m", "gateway.tests.consumer_worker", URL, self.feed, "g", "worker", handled_key, str(delay)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def handled(self, key):
        return [int(x) for x in self.board.client.lrange(key, 0, -1)]

    def wait_until(self, condition, seconds, what):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def test_a_killed_consumer_carries_on_without_repeating_what_it_finished(self):
        total, key = 60, f"handled-{self.feed}"
        for i in range(total):
            self.board.publish(self.feed, message(i))

        worker = self.start_worker(key)
        try:
            self.wait_until(lambda: len(self.handled(key)) >= 15, 30, "the first worker to get going")
        finally:
            worker.send_signal(signal.SIGKILL)           # no chance to clean up
            worker.wait()
        before = len(self.handled(key))
        self.assertLess(before, total, "the worker finished before it was killed; make it slower")

        worker = self.start_worker(key)
        try:
            self.wait_until(lambda: len(set(self.handled(key))) >= total, 60, "the restarted worker to finish")
        finally:
            worker.send_signal(signal.SIGKILL)
            worker.wait()

        handled = self.handled(key)
        self.assertEqual(set(handled), set(range(total)), "some messages were never handled")
        repeats = len(handled) - len(set(handled))
        # at most the one message that was in flight when the worker was killed may be handled twice
        self.assertLessEqual(repeats, 1, f"{repeats} messages were handled twice")
        self.assertGreater(len(handled) - before, 0)
        self.assertEqual(self.pending(), 0)


class TestWhenRedisMisbehaves(RedisCase):
    def test_publishing_with_redis_down_raises_feed_unavailable(self):
        down = Feed.from_url("redis://localhost:1/0")
        with self.assertRaises(FeedUnavailable):
            down.publish(self.feed, message(1))

    def test_consume_waits_out_a_lost_connection_and_carries_on(self):
        self.board.publish(self.feed, message(1))
        self.board.publish(self.feed, message(2))
        real = self.board.client.xreadgroup
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise redis.ConnectionError("connection lost")
            return real(*args, **kwargs)

        with patch.object(self.board.client, "xreadgroup", side_effect=flaky):
            got = self.consume(2, ack=True)
        self.assertEqual(segments(got), [1, 2])
        self.assertGreaterEqual(calls["n"], 3)

    def test_consume_makes_the_group_again_if_redis_lost_its_data(self):
        def lose_and_publish():
            self.board.client.delete(self.feed)          # as if Redis restarted without its saved data
            self.board.publish(self.feed, message(3))

        threading.Timer(0.6, lose_and_publish).start()
        got = self.consume(1, start="$", ack=True)
        self.assertEqual(segments(got), [3])

    def test_a_block_longer_than_the_socket_timeout_is_refused(self):
        with self.assertRaises(ValueError):
            next(self.board.consume(self.feed, "g", "c", block_ms=60_000))

    def test_the_stop_event_ends_consuming(self):
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        self.assertEqual(list(self.board.consume(self.feed, "g", "c", stop=stop, block_ms=200)), [])
        self.assertLess(time.monotonic() - started, 3)

    def test_a_feed_is_trimmed_so_it_cannot_grow_without_limit(self):
        small = Feed.from_url(URL, maxlen=10)
        for i in range(600):
            small.publish(self.feed, message(i))
        self.assertLess(self.board.client.xlen(self.feed), 300)


if __name__ == "__main__":
    unittest.main()
