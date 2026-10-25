"""Phase 3 check: kill a consumer mid-stream, restart it, and it continues without repeats.

Runs against the real feed and group: it publishes test messages to ``final-text`` under a session id of its
own, and a worker process reads them as a consumer in the ``translator`` group. The worker is killed with
SIGKILL several times while it works and restarted under the same name each time. The worker writes its
result (here, the segment number in a Redis list) in the same transaction as the ack, as a real worker
posting a translation would. Afterwards every message must have been handled exactly once and nothing may
be left pending. The test messages and their traces are removed at the end.

    docker compose up -d redis
    python -m gateway.check_restart                    # 100 messages, killed 3 times
    python -m gateway.check_restart --messages 300 --kills 6
"""
import argparse
import signal
import subprocess
import sys
import time
import uuid

from gateway.feed import Feed, FeedUnavailable
from gateway.schema import FINAL_TEXT, KIND_FINAL_TEXT, FeedMessage

GROUP, CONSUMER = "translator", "check-worker"


def worker(url, session, handled_key, delay):
    """The consumer being killed: handles only this check's messages, leaves anything else for others."""
    board = Feed.from_url(url)
    for delivery in board.consume(GROUP, CONSUMER, block_ms=200):
        if delivery.message.session != session:
            continue          # not ours: left pending, so a real translator takes it over
        time.sleep(delay)     # the work
        segment = delivery.message.segment
        try:
            delivery.ack(also=lambda pipe: pipe.rpush(handled_key, segment))
        except FeedUnavailable:
            pass              # handed out again later


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None, help="Redis URL (default: $REDIS_URL or redis://localhost:6379/0)")
    parser.add_argument("--messages", type=int, default=100)
    parser.add_argument("--kills", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.03, help="seconds of 'work' per message")
    parser.add_argument("--worker", nargs=3, metavar=("SESSION", "HANDLED_KEY", "DELAY"), help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        worker(args.url, args.worker[0], args.worker[1], float(args.worker[2]))
        return 0

    board = Feed.from_url(args.url)
    client = board.client
    board.ensure_feeds()
    groups = {g["name"] for g in client.xinfo_groups(FINAL_TEXT)}
    print(f"feed {FINAL_TEXT}: groups {sorted(groups)}")
    if GROUP not in groups:
        print(f"FAIL: group {GROUP} missing")
        return 1

    session, total = f"s-check-{uuid.uuid4().hex[:8]}", args.messages
    handled_key = f"check:handled:{session}"
    ids = [board.publish(FeedMessage.new(kind=KIND_FINAL_TEXT, session=session, segment=i, start=i, end=i + 1,
                                         lang="en", text=f"check message {i}", source="check_restart"))
           for i in range(total)]
    print(f"published {total} messages to {FINAL_TEXT} as session {session}")

    def handled():
        return [int(x) for x in client.lrange(handled_key, 0, -1)]

    def start():
        cmd = [sys.executable, "-m", "gateway.check_restart", "--worker", session, handled_key, str(args.delay)]
        if args.url:
            cmd[3:3] = ["--url", args.url]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_for(count, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and len(set(handled())) < count:
            time.sleep(0.02)
        return len(set(handled())) >= count

    kill_points = [round(total * (k + 1) / (args.kills + 1)) for k in range(args.kills)]
    try:
        for point in kill_points:
            proc = start()
            ok = wait_for(point, 60)
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            print(f"  killed the consumer (SIGKILL) after {len(handled())} of {total}; restarting it")
            if not ok:
                print("FAIL: the consumer stopped making progress")
                return 1
        proc = start()
        finished = wait_for(total, 120)
        proc.send_signal(signal.SIGKILL)
        proc.wait()

        done = handled()
        repeats = len(done) - len(set(done))
        missing = sorted(set(range(total)) - set(done))
        ours = set(ids)
        pending = [p for p in client.xpending_range(FINAL_TEXT, GROUP, min="-", max="+", count=100_000)
                   if p["message_id"] in ours]
        print(f"handled {len(set(done))} of {total} messages, {repeats} repeats, {len(missing)} missing, "
              f"{len(pending)} still pending, after {len(kill_points)} kills")
        passed = finished and not missing and repeats == 0 and not pending
        print("PASS: it continued after every kill, handled everything once, and repeated nothing"
              if passed else "FAIL")
        return 0 if passed else 1
    finally:
        client.xdel(FINAL_TEXT, *ids)
        client.delete(handled_key, *client.keys(f"done:*{session}*"))
        # remove the check's consumer from the group, but only if it holds nothing: deleting a consumer
        # drops whatever is still pending with it
        for c in client.xinfo_consumers(FINAL_TEXT, GROUP):
            if c["name"] == CONSUMER and c["pending"] == 0:
                client.xgroup_delconsumer(FINAL_TEXT, GROUP, CONSUMER)


if __name__ == "__main__":
    sys.exit(main())
