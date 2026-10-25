"""A consumer in its own process, for the kill-and-restart tests.

Handles each message slowly, records its segment number in a Redis list, then acks it.

    python -m gateway.tests.consumer_worker URL FEED GROUP CONSUMER HANDLED_KEY DELAY [CLAIM_IDLE_MS] [BATCH]
"""
import sys
import time

from gateway.feed import Feed, FeedUnavailable


def main(url, feed, group, consumer, handled_key, delay, claim_idle_ms=30_000, batch=10):
    board = Feed.from_url(url)
    for delivery in board.consume(feed, group, consumer, block_ms=200, start="0", batch=batch,
                                  claim_idle_ms=claim_idle_ms, claim_every_s=0.5):
        time.sleep(delay)
        board.client.rpush(handled_key, delivery.message.segment)
        try:
            delivery.ack()
        except FeedUnavailable:
            pass  # it is handed out again later


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args[0], args[1], args[2], args[3], args[4], float(args[5]),
         int(args[6]) if len(args) > 6 else 30_000, int(args[7]) if len(args) > 7 else 10)
