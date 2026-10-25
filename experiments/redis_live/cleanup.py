"""Remove what this experiment left in Redis: the bridge's sentences (sessions s-live-...) from final-text,
their repeat-skipping keys, and the watcher's group. Messages of other sessions are not touched.

    python -m experiments.redis_live.cleanup
"""
import sys

from gateway.feed import Feed
from gateway.schema import FINAL_TEXT, FeedMessage

from experiments.redis_live.watch import GROUP


def main(url=None):
    client = Feed.from_url(url).client
    ours = []
    for entry_id, fields in client.xrange(FINAL_TEXT):
        try:
            if FeedMessage.from_json(fields["data"]).session.startswith("s-live-"):
                ours.append(entry_id)
        except Exception:
            pass
    if ours:
        client.xdel(FINAL_TEXT, *ours)
    keys = client.keys('done:*"s-live-*')
    if keys:
        client.delete(*keys)
    groups = {g["name"] for g in client.xinfo_groups(FINAL_TEXT)} if client.exists(FINAL_TEXT) else set()
    if GROUP in groups:
        client.xgroup_destroy(FINAL_TEXT, GROUP)
    print(f"removed {len(ours)} experiment messages from {FINAL_TEXT}, {len(keys)} keys"
          + (f", and the group {GROUP}" if GROUP in groups else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
