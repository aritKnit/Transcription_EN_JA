"""Watch the final-text feed as its own consumer group and show each sentence as it arrives.

    python -m experiments.redis_live.watch              # group live-watch, consumer watcher-1
    python -m experiments.redis_live.watch --from-start # also what final-text still holds

For each message it shows how long it took from being published to arriving here (made_at to now), and
every 10 s the group's state in Redis. Because the group remembers where it stopped, you can stop this
(even with kill -9) and start it again: it carries on from the next message.
"""
import argparse
import sys
import threading
import time
from datetime import datetime, timezone

from gateway.feed import Feed
from gateway.schema import FINAL_TEXT

GROUP = "live-watch"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--redis", default=None, help="Redis URL (default: $REDIS_URL or redis://localhost:6379/0)")
    parser.add_argument("--consumer", default="watcher-1")
    parser.add_argument("--from-start", action="store_true", help="a new group reads what the feed still holds")
    args = parser.parse_args()

    board = Feed.from_url(args.redis)
    print(f"watching {FINAL_TEXT} as group {GROUP}, consumer {args.consumer}; Ctrl+C to stop\n", flush=True)
    stop = threading.Event()

    def report():
        while not stop.wait(10):
            try:
                info = next((g for g in board.client.xinfo_groups(FINAL_TEXT) if g["name"] == GROUP), None)
                if info:
                    print(f"  [redis] {FINAL_TEXT}: {board.client.xlen(FINAL_TEXT)} messages held; "
                          f"group {GROUP}: pending {info['pending']}, lag {info.get('lag')}", flush=True)
            except Exception as e:
                print(f"  [redis] unavailable: {e}", flush=True)

    threading.Thread(target=report, daemon=True).start()
    transits = []
    try:
        for delivery in board.consume(GROUP, args.consumer, feed=FINAL_TEXT, start="0" if args.from_start else "$"):
            m = delivery.message
            transit_ms = (datetime.now(timezone.utc) - m.made_at).total_seconds() * 1000
            transits.append(transit_ms)
            print(f"{m.session} #{m.segment} [{m.lang}] {m.start:6.1f}-{m.end:6.1f}s  "
                  f"(in Redis for {transit_ms:.1f} ms)  {m.text}", flush=True)
            delivery.ack()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    if transits:
        transits.sort()
        print(f"\n{len(transits)} messages; publish-to-arrival median {transits[len(transits) // 2]:.1f} ms, "
              f"worst {transits[-1]:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
