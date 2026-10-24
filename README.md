# Real-time transcription service

A live speech-to-text service built on [WhisperLive](WhisperLive/) (faster-whisper, INT8, CPU only).
Audio goes in over a WebSocket; draft text and finished sentences come back as you speak.
English and Japanese are the tested languages; any language the Whisper model supports works.

The target design is in `Real-Time Transcription HLD.pdf`, and the build order is in [plan.md](plan.md).
[updates.md](updates.md) lists what has changed so far.

## What works today

| Part | Status |
|---|---|
| Speech engine (Docker): model preload, `/ready`, backlog meter, seat limit | Built |
| Test bench: real-time replay, WER / CER, delays | Built ([bench/](bench/)) |
| Redis message board, session gateway, web app, Caddy, monitoring, translation | Not built yet (plan.md phases 3 to 9) |

So for now you talk to the engine directly with a WebSocket client (the WhisperLive client, the bench, or your own code).
There is no browser page yet.

## Requirements

- Docker with Compose v2. On Windows, Docker Desktop with the WSL2 backend.
- About 6 GB of RAM for the engine at the default settings (it is capped at 2 GB; Docker itself needs more).
- Python 3.10 or newer on the host, only for the client and the bench.
- Disk: about 2.2 GB for the engine image, plus the model (about 150 MB for `base`).

## Setup

```bash
git clone <this repo> && cd Whisper

# 1. Settings (model folder, seats, threads, CPU and memory caps)
cp .env.example .env

# 2. Put a model in models/<MODEL_DIR>. This one is multilingual (English, Japanese, ...)
python3 -m venv .venv && source .venv/bin/activate
pip install huggingface_hub
python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-base', local_dir='models/base-v1')"

# 3. Build and start the engine (the first build downloads about 1 GB and can take 15 to 25 minutes)
docker compose up -d --build engine

# 4. Wait until it is ready (503 while the model loads, 200 when warm)
curl -i localhost:8000/ready
curl localhost:8000/health
```

`.env` points `MODEL_DIR` at the folder under `models/`. To change the model, put a new CTranslate2 model in
another folder (for example `models/small-v1`), set `MODEL_DIR=small-v1` and run `docker compose up -d engine`.
To roll back, set it back. Old folders are never touched.

Install the client tools on the host:

```bash
pip install -r WhisperLive/requirements/client.txt     # microphone and file client
pip install -r bench/requirements.txt                  # only for the test bench
```

## Use it

### Transcribe a file

```bash
python WhisperLive/run_client.py --server localhost --port 9090 --lang en \
    --files path/to/audio.wav --mute_audio_playback
```

Use `--lang ja` for Japanese. The engine uses the model from `.env`, so the client's `--model` option does not matter here.
`--mute_audio_playback` stops the client from playing the file out loud (needed on WSL2, which has no sound card).

To see timing as well, replay the file at real speed with the bench client; it prints each update with its arrival time:

```bash
python bench/replay.py path/to/audio.wav --language ja
```

### Live microphone

```bash
python WhisperLive/run_client.py --server localhost --port 9090 --lang en
```

Draft text updates about once a second and finished sentences are kept. Stop with Ctrl+C.

On WSL2 the microphone comes from Windows through WSLg:

1. Windows: Settings > Privacy & security > Microphone, allow desktop apps. Set your headset as the default input in Settings > System > Sound.
2. In WSL: `sudo apt install -y pulseaudio-utils libasound2-plugins alsa-utils portaudio19-dev`
3. Make it permanent:
   ```bash
   echo 'export PULSE_SERVER=unix:/mnt/wslg/PulseServer' >> ~/.bashrc
   printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > ~/.asoundrc
   ```
4. Check: `pactl list short sources` shows a source, and `parecord --channels=1 /tmp/t.wav` then `paplay /tmp/t.wav` plays your voice back.
5. If you change the default device in Windows, run `wsl --shutdown` and reopen the terminal.

The ALSA and JACK lines printed at start are noise from missing sound cards; they are harmless as long as the client says `* recording` and does not print `Unable to access microphone`.

### Live streams (RTSP or HLS)

The WhisperLive client can read a live stream directly. `run_client.py` has no flag for it, so use Python:

```python
from whisper_live.client import TranscriptionClient

client = TranscriptionClient("localhost", 9090, lang="en", use_vad=True)
client(rtsp_url="rtsp://camera.local:554/stream")     # RTSP over TCP
# or
client(hls_url="https://example.com/live/playlist.m3u8")
```

Run it from `WhisperLive/` with `pip install -r WhisperLive/requirements/client.txt` done. The client decodes the stream, resamples it to 16 kHz
mono and sends it; the engine does not care where the audio came from.

For any other source (a meeting app's audio, a capture card, your own code), open a WebSocket to `ws://localhost:9090` and:

1. Send one JSON text message: `{"uid": "my-session", "language": "en", "task": "transcribe", "model": "base", "use_vad": true}`.
2. Wait for `{"message": "SERVER_READY"}`. A reply with `"status": "WAIT"` means the engine is full.
3. Send audio as binary messages: 16 kHz mono float32 samples, in chunks of about 100 ms.
4. Read JSON messages with a `segments` list. Each segment has `start`, `end`, `text` and `completed`. Segments with `completed: false` are drafts that may still change.
5. Send the bytes `END_OF_AUDIO` when you are done.

[bench/replay.py](bench/replay.py) is a complete, small example of this.

A single engine has one seat by default, so one live stream at a time. Sessions end after `MAX_CONNECTION_TIME` seconds (one hour by default).

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `MODEL_DIR` | `base-v1` | Folder under `models/` the engine loads |
| `MAX_CLIENTS` | `1` | Seats. Raise to 2 only after the two-stream test passes |
| `MAX_CONNECTION_TIME` | `3600` | Longest session, in seconds |
| `ENGINE_THREADS` | `4` | CPU threads used by the model |
| `ENGINE_CPUS` | `4` | CPU cap for the container |
| `ENGINE_MEM` | `2g` | Memory cap for the container |

Ports (bound to `127.0.0.1` only): `9090` WebSocket audio, `8000` `/health` and `/ready`. Prometheus metrics are on port `9091`
inside the container (including `whisperlive_backlog_seconds`); publish it in `docker-compose.yml` if you want to scrape it.

## Test bench

Replays recordings at real speed against the engine and scores accuracy and delay. See [bench/README.md](bench/README.md).

```bash
python bench/prepare.py                  # builds 10 English and 10 Japanese recordings from Google FLEURS
python bench/run_bench.py --tag mytest   # about 40 minutes for 3 repeats
```

## Troubleshooting

**`426 Upgrade Required` / `invalid Connection header: keep-alive` in the engine log.**
Something sent a normal HTTP request to the WebSocket port (a browser tab, `curl`, a health probe). Harmless. Use port `8000` for HTTP.

**`Connection refused` on port 9090 right after starting.**
The engine opens the audio port only after the model is loaded and warm. Wait for `curl localhost:8000/ready` to return 200.
If it never does: `docker compose logs engine`.

**Container keeps restarting (`docker compose ps` shows `Restarting`).**
Read `docker compose logs engine`. The usual causes: `MODEL_DIR` points at a folder that does not exist or is not a CTranslate2 model
(it needs `model.bin`, `config.json`, `tokenizer.json`), or the container was killed for memory (exit code 137): raise `ENGINE_MEM` or use a smaller model.

**Client prints `WAIT` or the bench says "engine is full".**
All seats are taken. Close the other session, or wait; the message carries the minutes until the oldest session times out.

**`address already in use` on 9090 or 8000.**
An older container is using the port. `docker ps`, then `docker rm -f <name>` (for example `whisperlive` from an earlier test).

**`run_client.py: error: unrecognized arguments: --host`.**
The option is `--server`, not `--host`.

**`Unable to access microphone`, `no default output device`, or `Unanticipated host error` with `overrun`.**
Follow the WSL2 microphone steps above. In file mode add `--mute_audio_playback`. An overrun at the start usually means the engine was still loading; start the client after `/ready` is 200.

**Text is delayed or very behind.**
The model is too big for the CPU. Look at `docker stats`; if the engine sits at its CPU cap, use a smaller model (`tiny` or `base`) or raise `ENGINE_CPUS` and `ENGINE_THREADS` together.
Check the backlog: `curl localhost:8000/health` shows `backlog_seconds`. Near 0 is healthy; a number that keeps growing means the engine cannot keep up.

**Wrong or poor transcription.**
Use a clear headset microphone (a Bluetooth headset in hands-free mode only gives telephone quality). Set `--lang` to the language being spoken.
Do not use a `.en` model for Japanese. Small models make more mistakes in Japanese than in English; try `small` and compare with the bench.

**First `docker compose build` takes very long.**
It downloads torch and the Python packages. Later builds reuse the cache; a code-only change rebuilds in seconds.
