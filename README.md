# Real-time transcription service

A live speech-to-text service built on [WhisperLive](WhisperLive/), CPU only.
Audio goes in over a WebSocket; draft text and finished sentences come back as you speak.
The speech model is a setting, not part of the design: the engine runs either
[SenseVoice-Small](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) (the default) or any
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) Whisper model, and everything around it
(client, bench, and the gateway and web app to come) speaks the same WebSocket protocol for both.
English and Japanese are the tested languages. How the models compare is in [results.md](results.md).

The target design is in `Real-Time Transcription HLD.pdf`, and the build order is in [plan.md](plan.md).
[updates.md](updates.md) lists what has changed so far.

## What works today

| Part | Status |
|---|---|
| Speech engine (Docker): swappable model (SenseVoice-Small or faster-whisper), preload, `/ready`, backlog meter, seat limit | Built |
| Live microphone through the engine (the WhisperLive client, see "Live microphone") | Works |
| Test bench: real-time replay, WER / CER, delays | Built ([bench/](bench/)) |
| Redis message board, session gateway, web app, Caddy, monitoring, translation | Not built yet (plan.md phases 3 to 9) |

So for now you talk to the engine directly with a WebSocket client (the WhisperLive client, the bench, or your own code).
There is no browser page yet.

## Requirements

- Docker with Compose v2. On Windows, Docker Desktop with the WSL2 backend.
- About 6 GB of RAM. The engine is capped at 3 GB by default (SenseVoice needs about 2 GB; a faster-whisper `base` model about 1 GB).
- Python 3.10 or newer on the host, only for the client and the bench.
- Disk: a few GB for the engine image (PyTorch and FunASR are large), plus the model (SenseVoice-Small about 0.9 GB, faster-whisper `base` about 150 MB).

## Setup

```bash
git clone <this repo> && cd Whisper

# 1. Settings (model folder, seats, threads, CPU and memory caps)
cp .env.example .env

# 2. Put a model in models/<MODEL_DIR> (the default is SenseVoice-Small, about 0.9 GB)
python3 -m venv .venv && source .venv/bin/activate
pip install huggingface_hub
python -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/SenseVoiceSmall', local_dir='models/sensevoice-small-v1')"

# 3. Build and start the engine (the first build downloads PyTorch and FunASR and can take 20 to 30 minutes)
docker compose up -d --build engine

# 4. Wait until it is ready (503 while the model loads, 200 when warm)
curl -i localhost:8000/ready
curl localhost:8000/health
```

`.env` holds `BACKEND` (which kind of model) and `MODEL_DIR` (which folder under `models/`). See "Choosing a model" below to switch.

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
With SenseVoice, `--lang` can be `en`, `ja`, `zh`, `yue` or `ko`; any other value means automatic language detection.
`--mute_audio_playback` stops the client from playing the file out loud (needed on WSL2, which has no sound card).

To see timing as well, replay the file at real speed with the bench client; it prints each update with its arrival time:

```bash
python bench/replay.py path/to/audio.wav --language ja
```

### Live microphone

**Start the live pipeline** (works from a cold start; on WSL2 do the microphone setup below once first):

```bash
# 1. Start the engine and wait until it is ready: 503 while the model loads, 200 when it is warm
docker compose up -d engine
curl -i localhost:8000/ready

# 2. In the environment where the client is installed, speak and watch the text appear
source .venv/bin/activate
python WhisperLive/run_client.py --server localhost --port 9090 --lang en --enable_timestamps --n_display_segments 6
```

For Japanese use `--lang ja` (SenseVoice also takes `zh`, `yue` and `ko`). Stop with **Ctrl+C**, not by closing the terminal: that saves the transcript as `output.srt` in the folder you ran it from.
The client prints `* recording`, then `Server Ready!`, and then the text block starts updating.

What you should see: a draft appears within about a second of you starting to speak and is rewritten as you go on (it can change words as more audio arrives).
A sentence stays a draft until you pause or the next one starts, so the last sentence you say may take a few seconds to settle after you stop.
On WSL2 the microphone can take a couple of seconds to start delivering audio, so give it a moment after `Server Ready!` before you speak.

**A one-minute check, in English and in Japanese.** Run the command twice, once per language, for about a minute each, and add `--save_output_recording --output_file ./live_en.wav`
(and `./live_ja.wav` for Japanese) to keep what the microphone heard:

```bash
python WhisperLive/run_client.py --server localhost --port 9090 --lang en --enable_timestamps --n_display_segments 6 \
    --save_output_recording --output_file ./live_en.wav
python WhisperLive/run_client.py --server localhost --port 9090 --lang ja --enable_timestamps --n_display_segments 6 \
    --save_output_recording --output_file ./live_ja.wav
```

Speak naturally with pauses between sentences, and include a few numbers and names. Things worth noticing:

- How soon the first words appear, and how far the draft trails behind you while you speak.
- Whether finished sentences are right, and what goes wrong (names, numbers, short words, English words inside Japanese).
- What happens in pauses and when you stop (does the last sentence settle?).
- Whether your headset microphone, not the laptop's, is the one being used (see the setup below).

A saved recording can be replayed through the engine as many times as you like, with timing, using `python bench/replay.py live_en.wav --language en`.

On WSL2 the microphone comes from Windows through WSLg:

1. Windows: Settings > Privacy & security > Microphone, allow desktop apps. Set your headset as the default input in Settings > System > Sound.
2. In WSL: `sudo apt install -y pulseaudio-utils libasound2-plugins alsa-utils portaudio19-dev`
3. Make it permanent:
   ```bash
   echo 'export PULSE_SERVER=unix:/mnt/wslg/PulseServer' >> ~/.bashrc
   printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > ~/.asoundrc
   ```
4. Check: `pactl list short sources` shows a source (`RDPSource` is the Windows microphone), and `parecord --channels=1 /tmp/t.wav` then `paplay /tmp/t.wav` plays your voice back.
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

## Choosing a model

The engine loads one model at start and keeps it warm. Which one is decided by two settings in `.env`:

| `BACKEND` | `MODEL_DIR` example | Model | Languages | Notes |
|---|---|---|---|---|
| `sensevoice` (default) | `sensevoice-small-v1` | [FunAudioLLM/SenseVoiceSmall](https://huggingface.co/FunAudioLLM/SenseVoiceSmall), run with FunASR | `en`, `ja`, `zh`, `yue`, `ko` | Much more accurate than Whisper `base` on the test set and keeps up in real time ([results.md](results.md)). No translation, no word timestamps. Licence: FunASR Model License (commercial use allowed with attribution) |
| `faster_whisper` | `base-v1` | Any CTranslate2 Whisper model, run with faster-whisper (INT8) | All Whisper languages | Also serves the OpenAI-style REST endpoint `/v1/audio/transcriptions` on port 8000 |

To switch, change both settings and restart the engine. The model folder must already be in `models/`:

```bash
# faster-whisper example
python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-base', local_dir='models/base-v1')"
# then in .env: BACKEND=faster_whisper  and  MODEL_DIR=base-v1
docker compose up -d engine
curl localhost:8000/health        # shows the backend and model the engine is running
```

To roll back, set them back; old model folders are never touched. Clients, the bench and the WebSocket protocol do not change.

**How it works.** SenseVoice is not a streaming model, so the engine cuts the incoming audio into speech pieces with Silero VAD and runs the model on the
current sentence over and over as it grows; this is why drafts update word by word and why it is fast enough to keep up. Whisper models go through
the same loop with faster-whisper's own decoding. Both produce the same JSON segments (`start`, `end`, `text`, `completed`).

**Adding another model.** A backend is one file in [WhisperLive/whisper_live/backend/](WhisperLive/whisper_live/backend/) that subclasses `ServeClientBase` and
implements `transcribe_audio` (audio in, segments with `start`, `end`, `text` out) and `handle_transcription_output`.
[sensevoice_backend.py](WhisperLive/whisper_live/backend/sensevoice_backend.py) is the shortest example. Then register it: add a value to `BackendType` and a branch in `initialize_client`
and in the `--preload` step of [server.py](WhisperLive/whisper_live/server.py), and add its dependencies to [engine/requirements.txt](engine/requirements.txt).

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `BACKEND` | `sensevoice` | `sensevoice` or `faster_whisper` |
| `MODEL_DIR` | `sensevoice-small-v1` | Folder under `models/` the engine loads |
| `MAX_CLIENTS` | `1` | Seats. Raise to 2 only after the two-stream test passes |
| `MAX_CONNECTION_TIME` | `3600` | Longest session, in seconds |
| `ENGINE_THREADS` | `4` | CPU threads used by the model |
| `ENGINE_CPUS` | `4` | CPU cap for the container |
| `ENGINE_MEM` | `3g` | Memory cap for the container |

Ports (bound to `127.0.0.1` only): `9090` WebSocket audio, `8000` `/health` and `/ready`. Prometheus metrics are on port `9091`
inside the container (including `whisperlive_backlog_seconds`); publish it in `docker-compose.yml` if you want to scrape it.

## Test bench

Replays recordings at real speed against the engine and scores accuracy and delay. See [bench/README.md](bench/README.md).

```bash
python bench/prepare.py                  # builds 10 English and 10 Japanese recordings from Google FLEURS
python bench/run_bench.py --tag mytest   # about 40 minutes for 3 repeats
```

Measured results for each model are in [results.md](results.md).

## Troubleshooting

**`426 Upgrade Required` / `invalid Connection header: keep-alive` in the engine log.**
Something sent a normal HTTP request to the WebSocket port (a browser tab, `curl`, a health probe). Harmless. Use port `8000` for HTTP.

**`Connection refused` on port 9090 right after starting.**
The engine opens the audio port only after the model is loaded and warm. Wait for `curl localhost:8000/ready` to return 200.
If it never does: `docker compose logs engine`.

**Container keeps restarting (`docker compose ps` shows `Restarting`).**
Read `docker compose logs engine`. The usual causes:
- `MODEL_DIR` points at a folder that does not exist, or `BACKEND` does not match the folder. `sensevoice` needs the files from `FunAudioLLM/SenseVoiceSmall`
  (`model.pt`, `config.yaml`, `am.mvn`, ...); `faster_whisper` needs a CTranslate2 model (`model.bin`, `config.json`, `tokenizer.json`).
- The container was killed for memory (exit code 137, check with `docker inspect`): raise `ENGINE_MEM`.

**Client prints `WAIT` or the bench says "engine is full".**
All seats are taken. Close the other session, or wait; the message carries the minutes until the oldest session times out.

**`address already in use` on 9090 or 8000.**
An older container is using the port. `docker ps`, then `docker rm -f <name>` (for example `whisperlive` from an earlier test).

**`run_client.py: error: unrecognized arguments: --host`.**
The option is `--server`, not `--host`.

**The client says `* recording` but no text ever appears.**
Check that the engine is ready (`curl localhost:8000/ready` is 200) and that the microphone really records: `parecord --channels=1 /tmp/t.wav`, speak, Ctrl+C, then `paplay /tmp/t.wav`.
If the playback is silent, the Windows microphone is muted, not the default input, or blocked in Windows privacy settings. Also check `--lang` matches what you are saying.

**The first words I said are missing.**
On WSL2 the microphone can take a couple of seconds to start delivering audio. Wait a moment after `Server Ready!` before speaking.

**`Unable to access microphone`, `no default output device`, or `Unanticipated host error` with `overrun`.**
Follow the WSL2 microphone steps above. In file mode add `--mute_audio_playback`. An overrun at the start usually means the engine was still loading; start the client after `/ready` is 200.

**Text is delayed or very behind.**
The model is too big for the CPU. Look at `docker stats`; if the engine sits at its CPU cap, raise `ENGINE_CPUS` and `ENGINE_THREADS` together, or with `faster_whisper` use a smaller model (`tiny` or `base`).
Check the backlog: `curl localhost:8000/health` shows `backlog_seconds`. Near 0 is healthy; a number that keeps growing means the engine cannot keep up.

**Wrong or poor transcription.**
Use a clear headset microphone (a Bluetooth headset in hands-free mode only gives telephone quality). Set `--lang` to the language being spoken.
With `faster_whisper`, do not use a `.en` model for Japanese; small Whisper models make many more mistakes in Japanese than in English (see [results.md](results.md)).
With `sensevoice`, a language outside `en`, `ja`, `zh`, `yue`, `ko` is detected automatically and may be wrong; only those five are supported.

**`501` from `/v1/audio/transcriptions`.**
That REST endpoint only exists with the `faster_whisper` backend. For SenseVoice use the WebSocket on port 9090.

**The container is up but `/ready` stays 503 for a long time.**
It is loading the model; SenseVoice takes a few seconds on a warm disk, longer the first time the model files are read. `docker compose logs engine` shows `Loading SenseVoice model` and then `Model warm-up took ...`.

**First `docker compose build` takes very long.**
It downloads PyTorch, torchaudio and FunASR with its dependencies. Later builds reuse the cache; a code-only change rebuilds in seconds.
