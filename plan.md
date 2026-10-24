# Real-time transcription: build plan

Sequential plan for building the system in `Real-Time Transcription HLD.pdf`. Each phase ends with a check;
do not start the next phase until it passes. Status: `[ ]` todo, `[~]` in progress, `[x]` done.

Repo layout (monorepo, one git repo at this root):

```
WhisperLive/      existing server, extended in place (the speech engine)
engine/           Dockerfile and settings for the speech engine container
gateway/          session gateway (FastAPI)
frontend/         Vite + Svelte web app, served by Caddy
worker-template/  template for feed workers (translation, add-ons)
monitoring/       Prometheus, Grafana, cAdvisor config
models/           model store, one subfolder per version (git-ignored)
bench/            replay client and scoring (Phase 2)
docker-compose.yml
```

## Phase 0: Repo layout and decisions `[x]`
- Monorepo folders created; WhisperLive vendored into it.
- Python 3.11 for every backend image (the HLD's choice).
- Shared message shape (HLD page 4) is defined once in `gateway/schema.py` (Phase 3).
- One `.env` per deployment, loaded with pydantic-settings; commit only `.env.example`.
- SenseVoice backend is parked; the HLD uses faster-whisper INT8 only.

## Phase 1: Speech engine, measured alone `[x]`
Goal: one container that loads a model from the model store at start, warms it up, says when it is ready, and exposes a backlog meter.
1. [x] `engine/Dockerfile` (Python 3.11, CPU torch, minimal `engine/requirements.txt`), built from the repo root.
2. [x] Model preload and warm-up at start (`--preload`), so the first session does not wait for a model load.
3. [x] `/ready` endpoint: 503 until the model is warm, then 200. `/health` stays as the liveness check and also reports backlog.
4. [x] Backlog meter: seconds of audio the engine has received but not yet looked at, as a Prometheus gauge (`whisperlive_backlog_seconds`).
5. [x] Seat check and busy message: the existing `WAIT` status already tells a refused client to come back; the gateway turns it into "server busy, try again" (Phase 4).
6. [x] Model in `models/base-v1/` (`Systran/faster-whisper-base`; INT8 is applied when it loads). One subfolder per version.
7. [x] `docker-compose.yml` with the engine service only: CPU and memory caps, read-only model volume, health check on `/ready`, `restart: unless-stopped`.

Check (passed): `docker compose up -d engine`; `/ready` went 503 to 200 after warm-up (about 3 s for the base model); a second client got `{"status": "WAIT"}` while the first stayed connected.

How to run it: `cp .env.example .env`, `docker compose up -d engine`, then `curl localhost:8000/ready` and `curl localhost:8000/health`. Settings (model folder, seats, threads, caps) live in `.env`.

## Phase 2: Test bench `[x]`
- `bench/`: replay a WAV at real speed over the websocket (soundfile + soxr to 16 kHz mono).
- Log timestamps for first draft, draft lag and finished segments. Score English WER and Japanese CER (jiwer), three repeats, mean and std (pandas, scipy). Japanese replaced Mandarin as the second language.
- Test set: 10 recordings per language (3 FLEURS sentences each) with reference text.

Check (passed): baseline in [bench/BASELINE.md](bench/BASELINE.md): English 12.05% WER, Japanese 30.22% CER with the `base` model; the engine falls behind on some long sentences (backlog up to 13 s). Every later change is compared against these.

## Phase 3: Redis and the message board `[ ]`
- `redis:7` in Compose, append-only file, volume.
- Streams `final-text` and `translations`, with consumer groups.
- `gateway/schema.py` (message model) and a small feed library: `publish(msg)`, `consume(group, name)`.

Check: kill a consumer mid-stream, restart it, and it continues without repeats.

## Phase 4: Session gateway `[ ]`
FastAPI with `/ws`, `/api`, `/health`, `/ready`. In order:
1. Access key check, session id.
2. Proxy audio to the engine and results back.
3. Segment numbers for final text; publish to `final-text`.
4. Resume record in Redis (2-minute TTL keeps the seat); resend only missed segments.
5. Duplicate filter (session + segment).
6. Busy notices (engine `WAIT`, backlog above a limit).
7. Forward every feed in `FORWARD_FEEDS` to the browser.
8. Direct path: if Redis is down, results still reach the browser.

Check: replay with the bench through the gateway and compare with Phase 2; drop the connection mid-file and see no repeated lines after resume.

## Phase 5: Frontend `[ ]`
Vite + Svelte (TypeScript). In order: AudioWorklet capture (16 kHz mono, ~100 ms chunks), connection keeper (retry 1, 2, 4, up to 30 s), transcript store keyed by segment number, screens (language picker, translation switch, status bar, privacy notice), file replay, "extra results" panel.

Check: speak and see grey drafts become final lines; reload mid-session and resume works.

## Phase 6: Front door and full Compose `[ ]`
- Caddy serves the built app and forwards `/ws` and `/api` to the gateway; laptop binds to `127.0.0.1`; HTTPS with a domain on the server.
- Every service gets `restart: unless-stopped`, a health check, `cpus` and `mem_limit`.

Check: `docker compose up` starts everything from a clean machine.

## Phase 7: Monitoring `[ ]`
Prometheus, Grafana, cAdvisor. Panels: delay to first draft and final, speed ratio, backlog, memory per container, reconnects, busy notices. Alert at 75% memory.

## Phase 8: Failure tests `[ ]`
Prove each card on HLD page 5: network drop, gateway crash, engine crash, engine behind, extra client, translation stopped, Redis down, memory limit, bad model rollback, 30-minute soak with silence and noise.

## Phase 9: Translation worker (optional) `[ ]`
Worker template, Opus-MT en<->zh converted to CTranslate2 INT8 into the model store, own CPU cap, `translations` added to `FORWARD_FEEDS`.

## Phase 10: Offline parts (optional) `[ ]`
DVC for recordings and scores; LoRA run (PyTorch + PEFT) only if errors are systematic; convert the result into a new model-store folder and keep the old one for rollback.

## Notes
- This machine (WSL2) has 16 logical CPUs and about 6 GB of RAM. Size container memory caps to that, and keep one seat until the two-stream test passes.
- Browsers allow microphone access only on `localhost` or HTTPS.
