# Experiment: Redis on the live pipeline

Branch `experiment/redis-live`. Shows the message board working with live speech: what you say goes to the speech engine,
each finished sentence is published to the real `final-text` feed in Redis, and a separate process reads it from there as it arrives.
Nothing here is part of `main`.

```
microphone ──> bridge.py ──WebSocket──> speech engine
                   │  finished sentence: publish(msg)
                   v
              Redis final-text ──consume("live-watch", ...)──> watch.py
```

`bridge.py` stands in for the Phase 4 gateway: it numbers each finished sentence of a session and publishes it with `Feed.publish(msg)`.
`watch.py` reads `final-text` as its own consumer group (`live-watch`), so it does not take messages away from the `translator` group.

## Run it

One-time: the engine and Redis running, the feeds created.

```bash
docker compose up -d engine redis
curl -i localhost:8000/ready                  # 200 when the model is warm
source .venv/bin/activate                     # the environment with gateway/requirements.txt and the client installed
python -m gateway.feed init
```

Terminal 1, the reader:

```bash
python -m experiments.redis_live.watch
```

Terminal 2, speak (about a minute each, Ctrl+C to stop):

```bash
python -m experiments.redis_live.bridge --language en
python -m experiments.redis_live.bridge --language ja
```

Instead of the microphone, `--file recording.wav` replays a recording at real speed.

What you see:

- **Terminal 2** prints each finished sentence when it is published, with how long the publish took (`#3 -> final-text in 2.1 ms`). When you stop, the sentence you were still saying is published too (`end of session`).
- **Terminal 1** prints the same sentences as they arrive from Redis, with the session, the sentence number, its time in the audio and how long it spent in Redis between being published and arriving (`in Redis for 3.4 ms`). Every 10 s it shows how many messages `final-text` holds and the group's pending count and lag.

## Things to try while it runs

| Do this | What should happen |
|---|---|
| Stop the watcher (Ctrl+C, or `kill -9`), keep talking, start it again | It carries on with the next sentence it had not seen; the ones published while it was down arrive at once (with a longer "in Redis for"). Nothing is shown twice. |
| `docker compose restart redis` while talking | The watcher logs a retry or two and carries on; sentences keep arriving. |
| `docker compose stop redis` for ~10 s while finishing a sentence, then `docker compose start redis` | The bridge prints `NOT published (Redis unavailable ...)` for sentences finished during the outage and keeps transcribing; later sentences are published and arrive normally. |

Afterwards, remove the experiment's sentences and the watcher's group from Redis (other sessions are left alone):

```bash
python -m experiments.redis_live.cleanup
```

## What it showed (2026-10-25, replaying FLEURS recordings through the engine)

- English and Japanese sessions: every finished sentence was published to `final-text` (1 to 4 ms per publish) and reached the watcher 2 to 5 ms after being published.
- Watcher killed with `kill -9` mid-session and restarted: the sentence published while it was down was waiting in Redis and arrived on restart (it had been there 2.7 s); nothing lost or repeated.
- Redis restarted mid-session: the watcher logged two retries, reconnected, and the next sentences arrived 3 to 4 ms after publishing.
- Redis stopped for 13 s: the sentence finished during the outage was reported as not published and the bridge carried on; the watcher backed off (0.2 s growing to 5 s), reconnected when Redis came back, and received the later sentences.
- **Finding for Phase 4:** a sentence finished while Redis is down never reaches the feed, so translation and add-ons miss it. The design says they should catch up after an outage; for that the gateway needs to keep sentences it could not publish and post them once Redis is back.
- Not checked by me: a real microphone; that run is yours.
