# Updates

Changelog of what has been built, newest first. The sequence and status of each phase is in [plan.md](plan.md).

## Live audio check and how to run it (2026-10-25)

Goal: see the pipeline run on live audio before starting Phase 3.

- [README.md](README.md), "Live microphone": start-to-finish steps for the live pipeline (start the engine, wait for `/ready`, run the client), a plan for a one-minute English and a one-minute Japanese check with `--save_output_recording` so the recording can be replayed through the bench afterwards, what to look at, and two new troubleshooting entries (no text appears; first words missing). The live client is the existing `WhisperLive/run_client.py`; no code changed.
- What I checked myself: the live capture path with a virtual PulseAudio microphone (a null sink on the WSLg Pulse server, recorded by PyAudio through its monitor), English passage `en_01`, a few runs. The microphone client received the transcript and showed it; the text matched the reference except the first word ("MS" came out as "It" or was missing in every run; I did not find out why). With a temporary timing script (not kept in the repo) the newest text was a median 0.39 s behind the audio (95% under 0.77 s), the first text appeared 0.61 s after speech began, a sentence was marked finished about 3 s after it ended, and the engine backlog stayed under 1 s. That matches the file replays (draft lag 0.45 s median), so the live capture path adds no measurable delay of its own.
- Found: with WSLg the microphone stream started delivering audio about 2.0 s after it was opened, hence the "wait a moment" note. My first timing run counted that start-up as lag (2.3 s median); starting the clock at the first audio fixed it.
- Not checked by me: a real microphone (a person speaking) and the Japanese live path; the one-minute English and Japanese check is yours to run. The virtual microphone I created was removed afterwards.

## Model-agnostic engine and SenseVoice-Small (2026-10-24)

Goal: make the speech model a setting and use FunAudioLLM's SenseVoice-Small.

- New backend [sensevoice_backend.py](WhisperLive/whisper_live/backend/sensevoice_backend.py): FunASR runs the original `FunAudioLLM/SenseVoiceSmall` weights; Silero VAD cuts each audio chunk into speech pieces, SenseVoice recognises each piece, and the shared base loop turns them into drafts and finished segments. Model tags such as `<|ja|><|EMO_UNKNOWN|>` are stripped. One model is shared by all clients and loaded once.
- [server.py](WhisperLive/whisper_live/server.py): `sensevoice` added to `BackendType`, a client branch in `initialize_client`, `--preload` and `/ready` work for it, and `/v1/audio/transcriptions` returns 501 unless the backend is `faster_whisper`. [run_server.py](WhisperLive/run_server.py): new generic `--model_path` (the old `-fw` still works).
- Engine image: `torchaudio` (CPU build, same index as torch) and `funasr==1.4.16` added; image is now about 3 GB. [docker-compose.yml](docker-compose.yml) and [.env.example](.env.example) choose the model with `BACKEND` and `MODEL_DIR`; default is `sensevoice` / `sensevoice-small-v1`, memory cap raised to 3 GB (the container peaked near 2.4 GiB).
- Tests: [test_sensevoice_backend.py](WhisperLive/tests/test_sensevoice_backend.py) (tag stripping, language mapping, segment building, preload, handshake, load failure) and two server tests (REST 501, model folder required).
- **Measured ([results.md](results.md), Run 2):** English WER 12.05% to 6.83% and Japanese CER 30.22% to 10.72% against the faster-whisper `base` baseline; draft lag about halved; backlog never above 2 s (was up to 13 s).
- Client-side VAD (a gate that sends only speech) was built and measured on a separate branch, `experiment/client-vad`, and is not part of this branch; see Phase 2c in [plan.md](plan.md) for the result and the engine-side problem it uncovered.
- [results.md](results.md) is new: it holds the baseline (moved from `bench/BASELINE.md`) and the two runs above. [README.md](README.md) now explains how to choose a model, how to add another backend, and the client-side VAD.

## Phase 2: test bench and baseline (2026-10-24)

Goal: measure accuracy and delay of the engine alone, in real time, for English and Japanese.

- [bench/prepare.py](bench/prepare.py): builds the test set from Google FLEURS (`en_us`, `ja_jp`, CC-BY-4.0): 10 recordings per language, 3 sentences each with short pauses, fixed seed. [bench/manifest.csv](bench/manifest.csv) holds the reference text; the audio is git-ignored and rebuilt by the script.
- [bench/replay.py](bench/replay.py): streams one recording at real speed in 100 ms chunks, followed by 6 s of silence, and logs every message with its arrival time. Also samples the engine's `backlog_seconds` once a second.
- [bench/score.py](bench/score.py): builds the transcript, computes WER (English) and CER (Japanese) with normalisation, and the delays: first draft, draft lag, finished-segment delay.
- [bench/run_bench.py](bench/run_bench.py): runs the whole set several times and writes raw events and a summary table to `bench/results/` (git-ignored). Scoring can be redone from the raw events with `score.py`.
- Added a draft-lag metric after the first smoke run showed that "final delay" mostly measures WhisperLive's rule that a sentence is finished only when the next one starts.
- Baseline, `base` model, one seat ([results.md](results.md)): English 12.05% WER, Japanese 30.22% CER; median draft lag about 1 s but the backlog reaches 13 s on some long sentences.
- [README.md](README.md): setup, usage (files, microphone, RTSP/HLS and raw WebSocket streams), settings and troubleshooting.
- `.gitignore`: ignores `bench/data/` (downloaded FLEURS files).

## Phase 1: speech engine in a container (2026-10-24)

Goal: one container that loads a model from the model store at start, warms it up, says when it is ready, and exposes a backlog meter.

### WhisperLive (extended in place)
- `--preload` flag in [run_server.py](WhisperLive/run_server.py). Loads the `-fw` model and runs it once on silence before the WebSocket port opens, so the first session does not wait for a model load.
- `/ready` endpoint in [server.py](WhisperLive/whisper_live/server.py): 503 until the model is warm, then 200. It needs no API key. `/health` stays as the liveness check and now also reports `ready` and `backlog_seconds`.
- Backlog meter: `ServeClientBase.backlog_seconds()` in [base.py](WhisperLive/whisper_live/backend/base.py) counts received audio that no transcription pass has looked at yet. `ClientManager.backlog_seconds()` sums it over clients, and [metrics.py](WhisperLive/whisper_live/metrics.py) exposes it as the Prometheus gauge `whisperlive_backlog_seconds`.
- [faster_whisper_backend.py](WhisperLive/whisper_live/backend/faster_whisper_backend.py): model loading moved out of `create_model` into shared module functions (`select_device`, `load_model`) and a `ServeClientFasterWhisper.preload()` classmethod. Behaviour for normal clients is unchanged.
- Existing behaviour reused as is: the seat check already answers a refused client with `{"status": "WAIT", "message": <minutes>}`, and `--max_clients`, `--max_connection_time`, `--omp_num_threads` and `--metrics_port` already existed.
- Tests: updated the `/health` test and added tests for `/ready`, the per-client backlog and the summed backlog (199 passing across the base-backend, server and metrics test files).

### New files
- [engine/Dockerfile](engine/Dockerfile) and [engine/requirements.txt](engine/requirements.txt): Python 3.11, CPU-only torch, only the faster-whisper path. Built from the repo root. Runs as a non-root user.
- [docker-compose.yml](docker-compose.yml): the `engine` service with the model folder mounted read-only, CPU and memory caps, a health check on `/ready`, and `restart: unless-stopped`.
- [.env.example](.env.example): model folder, seats, session limit, threads and caps. Copy to `.env`.
- [.dockerignore](.dockerignore): keeps the venv, models and git history out of the build context.
- [plan.md](plan.md): the phased build plan for the HLD.
- `models/base-v1/`: `Systran/faster-whisper-base` (git-ignored). INT8 is applied when it loads.

### Verified
- `/ready` went 503 to 200 after warm-up; the container reports healthy.
- A second client got `WAIT` while the first stayed connected.

### Fixed along the way
- `faster-whisper` imports `requests` without declaring it; added `requests` and `huggingface_hub` to the engine requirements.
- The warm-up crashed because the transcriber returns `None` when it finds no speech; it now handles that.

### Known limits
- First image build took about 23 minutes (downloads); rebuilds after code changes take seconds.
- This machine has about 6 GB of RAM, so the engine is capped at 2 GB and 4 CPUs.

## Phase 0: layout and decisions (2026-10-24)
- Monorepo folders: `WhisperLive/`, `engine/`, `gateway/`, `frontend/`, `worker-template/`, `monitoring/`, `models/`.
- Decisions: Python 3.11 for every backend image; one shared message shape (defined in Phase 3); one `.env` per deployment; the SenseVoice backend is parked.
