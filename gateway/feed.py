"""The message board: publish to a feed, read from it as part of a consumer group.

A feed is a Redis stream. Many services can read the same feed, each as its own group, and every group
sees every message. Inside a group, each message goes to one consumer, and the group remembers what was
handled, so a service that restarts carries on instead of starting over.

    board = Feed.from_url("redis://localhost:6379/0")
    board.ensure_feeds()                       # final-text and translations, with their consumer groups
    board.publish(message)                     # to the feed for message.kind (final_text -> final-text)

    for delivery in board.consume("translator", "translator-1"):   # the group's feed, from schema.GROUPS
        handle(delivery.message)
        delivery.ack()        # until it is acked, a message is handed out again after a restart

Delivery is at least once: a crash between handling and ``ack()`` hands that one message out again. To stop
that causing repeats, ``ack()`` also remembers the message (feed, group, ``kind``, ``session``, ``segment``) for ``dedupe_ttl_s`` seconds,
and a later delivery of the same message is acked and skipped without reaching the caller.

A handler whose result is itself a Redis write (posting a translation, for example) can put that write
in the same transaction as the ack: ``delivery.ack(also=lambda pipe: board.add_publish(pipe, translation))``.
Then the result and the ack happen together or not at all, and a crash can neither lose the message nor
repeat its result.

Run it by hand:

    python -m gateway.feed init
    python -m gateway.feed publish --text "hello"
    python -m gateway.feed tail --group translator --consumer me
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional

import redis
from pydantic import ValidationError

from gateway.schema import FEED_FOR_KIND, GROUPS, KIND_FINAL_TEXT, FeedMessage

log = logging.getLogger("feed")

DEFAULT_URL = "redis://localhost:6379/0"

# Replies meaning Redis is there but cannot serve right now: loading its data after a restart, full,
# unable to save to disk, a read-only replica, busy running a script, or without a primary.
TRANSIENT_REPLIES = ("LOADING", "OOM", "MISCONF", "READONLY", "BUSY", "MASTERDOWN")


class FeedUnavailable(Exception):
    """Redis cannot be used right now. Callers that must not stall (the gateway) catch this and carry on."""


def _transient(error: redis.ResponseError) -> bool:
    return str(error).split(" ", 1)[0] in TRANSIENT_REPLIES


@dataclass
class Delivery:
    """One message handed to a consumer. Call ``ack()`` once it has been handled."""
    id: str
    feed: str
    group: str
    message: FeedMessage
    _board: "Feed"

    def ack(self, also=None):
        """Mark the message handled. Raises ``FeedUnavailable`` if Redis cannot be used; the message is then
        handed out again later, so the caller can simply carry on.

        ``also(pipe)`` may add Redis writes (a result) to the same transaction, so they and the ack happen
        together or not at all.
        """
        self._board._ack(self.feed, self.group, self.id, self.message, also)


class Feed:
    def __init__(self, client: redis.Redis, maxlen: int = 10_000, dedupe_ttl_s: int = 3600):
        """
        Args:
            client: A redis-py client created with ``decode_responses=True`` and ``protocol=2``
                (``Feed.from_url`` does that).
            maxlen: Roughly how many messages a feed keeps; older ones are trimmed.
            dedupe_ttl_s: How long a handled message is remembered so a repeat is skipped. 0 turns it off.
        """
        if not client.connection_pool.connection_kwargs.get("decode_responses"):
            raise ValueError("the Redis client must be created with decode_responses=True (Feed.from_url does that)")
        self.client = client
        self.maxlen = maxlen
        self.dedupe_ttl_s = dedupe_ttl_s
        self.skipped_invalid = 0
        self.skipped_repeats = 0

    @classmethod
    def from_url(cls, url: Optional[str] = None, *, socket_timeout: float = 5.0, **kwargs) -> "Feed":
        client = redis.Redis.from_url(
            url or os.environ.get("REDIS_URL", DEFAULT_URL), decode_responses=True, protocol=2,
            # bytes that are not UTF-8 become U+FFFD, so such an entry is skipped as invalid instead of
            # making every read of the feed fail
            encoding_errors="replace",
            socket_timeout=socket_timeout, socket_connect_timeout=2.0,
        )
        return cls(client, **kwargs)

    @staticmethod
    def feed_for(message: FeedMessage) -> str:
        """The feed a message is published to, from its kind."""
        try:
            return FEED_FOR_KIND[message.kind]
        except KeyError:
            raise ValueError(f"no feed for kind '{message.kind}'; add it to FEED_FOR_KIND or pass feed=") from None

    def ensure_feeds(self) -> dict:
        """Create every feed and consumer group declared in ``schema.GROUPS`` (safe to run again).

        A group created here starts at the end of its feed, so create groups before anything is published.
        Returns ``{feed: [groups]}``.
        """
        created = {}
        for group, feed in GROUPS.items():
            self.create_group(feed, group, "$")
            created.setdefault(feed, []).append(group)
        return created

    def publish(self, message: FeedMessage, feed: Optional[str] = None) -> str:
        """Add a message to its feed (from ``message.kind``, or ``feed`` if given) and return its id.

        Raises ``FeedUnavailable`` if Redis cannot be reached or cannot accept writes right now. A publish that
        timed out may still have reached Redis; consumers skip such a repeat.
        """
        try:
            return self.client.xadd(feed or self.feed_for(message), {"data": message.to_json()},
                                    maxlen=self.maxlen, approximate=True)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            raise FeedUnavailable(str(e)) from e
        except redis.ResponseError as e:
            if _transient(e):
                raise FeedUnavailable(str(e)) from e
            raise

    def add_publish(self, pipe, message: FeedMessage, feed: Optional[str] = None):
        """Queue a publish on a pipeline, for ``delivery.ack(also=...)``: the result is posted together with
        the ack, or neither happens."""
        pipe.xadd(feed or self.feed_for(message), {"data": message.to_json()}, maxlen=self.maxlen, approximate=True)

    def create_group(self, feed: str, group: str, start: str = "$"):
        """Make sure a group exists (and the feed). ``start="$"`` means only messages published from now on;
        ``"0"`` means everything the feed still holds."""
        try:
            self.client.xgroup_create(feed, group, id=start, mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def consume(self, group: str, name: str, *, feed: Optional[str] = None, batch: int = 10, block_ms: int = 1000,
                claim_idle_ms: int = 30_000, claim_every_s: float = 5.0, start: str = "$",
                stop: Optional[threading.Event] = None) -> Iterator[Delivery]:
        """Yield the messages of a group's feed for the consumer ``name``, forever (until ``stop`` is set).

        The feed is the one ``schema.GROUPS`` declares for ``group``; pass ``feed`` for any other.

        Order: first the messages this consumer was handed before but never acked (so a restarted service
        picks up where it was), then messages another consumer was handed more than ``claim_idle_ms`` ago and
        never acked (so a dead one's work is taken over), then new messages. Messages that are not valid
        feed messages are acked and skipped. If Redis goes away, is loading or is full, this waits (waking up
        at once if ``stop`` is set) and carries on.
        """
        if feed is None:
            if group not in GROUPS:
                raise ValueError(f"group '{group}' is not in schema.GROUPS; add it there or pass feed=")
            feed = GROUPS[group]
        consumer = name
        timeout = self.client.connection_pool.connection_kwargs.get("socket_timeout")
        if timeout and block_ms / 1000 >= timeout:
            raise ValueError(f"block_ms ({block_ms}) must be shorter than the client's socket timeout ({timeout} s)")
        backoff = 0.2
        recovering, pending_cursor = True, "0"
        last_claim, claim_cursor = 0.0, "0-0"
        group_ready, group_start = False, start
        while not (stop is not None and stop.is_set()):
            try:
                if not group_ready:
                    self.create_group(feed, group, group_start)
                    group_ready = True
                if recovering:
                    entries = self._read(feed, group, consumer, pending_cursor, batch, None)
                    if entries:
                        pending_cursor = entries[-1][0]
                        yield from self._deliveries(feed, group, entries)
                        continue
                    recovering = False
                if time.monotonic() - last_claim >= claim_every_s:
                    last_claim = time.monotonic()
                    claim_cursor, entries = self._claim(feed, group, consumer, claim_cursor, claim_idle_ms, batch)
                    if entries:
                        yield from self._deliveries(feed, group, entries)
                        continue
                entries = self._read(feed, group, consumer, ">", batch, block_ms)
                yield from self._deliveries(feed, group, entries)
                backoff = 0.2
            except (redis.ConnectionError, redis.TimeoutError) as e:
                backoff = self._wait(feed, e, backoff, stop)
                recovering, pending_cursor, group_ready = True, "0", False
            except redis.ResponseError as e:
                if _transient(e):
                    backoff = self._wait(feed, e, backoff, stop)
                    recovering, pending_cursor, group_ready = True, "0", False
                elif "NOGROUP" in str(e) or "UNBLOCKED" in str(e):
                    # The group is gone (NOGROUP), or the feed was deleted while this call waited on it
                    # (UNBLOCKED): Redis came back without its data. Everything in the feed now is new to this
                    # group. The group is made again at the top of the loop, where errors are handled.
                    group_ready, group_start = False, "0"
                else:
                    raise

    @staticmethod
    def _wait(feed, error, backoff, stop):
        """Wait before retrying, waking up early if asked to stop. Returns the next, longer wait."""
        log.warning("feed %s: Redis unavailable (%s); retrying in %.1fs", feed, error, backoff)
        if stop is not None:
            stop.wait(backoff)
        else:
            time.sleep(backoff)
        return min(backoff * 2, 5.0)

    def _read(self, feed, group, consumer, cursor, count, block_ms):
        result = self.client.xreadgroup(group, consumer, {feed: cursor}, count=count, block=block_ms)
        return result[0][1] if result else []

    def _claim(self, feed, group, consumer, cursor, idle_ms, count):
        result = self.client.xautoclaim(feed, group, consumer, min_idle_time=idle_ms, start_id=cursor, count=count)
        next_cursor, entries = result[0], result[1]
        return next_cursor, entries

    def _deliveries(self, feed, group, entries) -> Iterator[Delivery]:
        for entry_id, fields in entries:
            try:
                message = FeedMessage.from_json(fields["data"])
            except (KeyError, TypeError, ValidationError, ValueError) as e:
                # trimmed away while pending, or not a feed message: never let it block the feed
                log.warning("feed %s: skipping %s: %s", feed, entry_id, type(e).__name__)
                self.skipped_invalid += 1
                self.client.xack(feed, group, entry_id)
                continue
            if self.dedupe_ttl_s and self.client.exists(self._done_key(feed, group, message)):
                self.skipped_repeats += 1
                self.client.xack(feed, group, entry_id)
                continue
            yield Delivery(entry_id, feed, group, message, self)

    @staticmethod
    def _done_key(feed: str, group: str, message: FeedMessage) -> str:
        # Per feed and group (two feeds may each have a group with the same name), as a JSON list so names
        # that contain the separator cannot run into each other.
        parts = [feed, group, message.kind, message.session, message.segment]
        return "done:" + json.dumps(parts, ensure_ascii=False, separators=(",", ":"))

    def _ack(self, feed, group, entry_id, message, also=None):
        try:
            pipe = self.client.pipeline(transaction=True)
            if also is not None:
                also(pipe)
            pipe.xack(feed, group, entry_id)
            if self.dedupe_ttl_s:
                pipe.set(self._done_key(feed, group, message), 1, ex=self.dedupe_ttl_s)
            pipe.execute()
        except (redis.ConnectionError, redis.TimeoutError) as e:
            raise FeedUnavailable(str(e)) from e
        except redis.ResponseError as e:
            if _transient(e):
                raise FeedUnavailable(str(e)) from e
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None, help=f"Redis URL (default: $REDIS_URL or {DEFAULT_URL})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the feeds and consumer groups declared in schema.GROUPS")
    pub = sub.add_parser("publish", help="post one sample message")
    pub.add_argument("--kind", default=KIND_FINAL_TEXT, help="decides the feed (final_text -> final-text)")
    pub.add_argument("--feed", default=None, help="publish to this feed instead")
    pub.add_argument("--session", default="s-demo")
    pub.add_argument("--segment", type=int, default=0)
    pub.add_argument("--lang", default="en")
    pub.add_argument("--text", default="hello")
    tail = sub.add_parser("tail", help="print messages as they arrive, as a consumer in a group")
    tail.add_argument("--group", default="translator")
    tail.add_argument("--consumer", default="me")
    tail.add_argument("--feed", default=None, help="for a group not in schema.GROUPS")
    args = parser.parse_args()

    board = Feed.from_url(args.url)
    try:
        if args.command == "init":
            for feed, groups in board.ensure_feeds().items():
                print(f"{feed}: groups {', '.join(groups)}")
        elif args.command == "publish":
            message = FeedMessage.new(kind=args.kind, session=args.session, segment=args.segment, start=0.0,
                                      end=1.0, lang=args.lang, text=args.text, source="cli")
            print(board.publish(message, feed=args.feed))
        else:
            for delivery in board.consume(args.group, args.consumer, feed=args.feed):
                m = delivery.message
                print(f"{delivery.id}  {m.session}#{m.segment} [{m.kind}/{m.lang}] {m.text}", flush=True)
                delivery.ack()
    except FeedUnavailable as e:
        raise SystemExit(f"Cannot reach Redis: {e}")
    except redis.ConnectionError as e:
        raise SystemExit(f"Cannot reach Redis: {e}")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
