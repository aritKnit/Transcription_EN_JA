# Results

Measurements from the real-time test bench ([bench/](bench/README.md)), newest last. Every run uses the same recordings
(10 English and 10 Japanese, about 35 s each, from Google FLEURS), replayed at real speed, 3 repeats, one seat.
Error rate is WER for English and CER for Japanese; lower is better. Columns are explained in [bench/README.md](bench/README.md#what-is-measured).

Host for all runs: WSL2, 16 logical CPUs, about 6 GB RAM, engine container capped at 4 CPUs.

## Run 1: faster-whisper `base` (baseline), 2026-10-24

Engine from commit `7286891`, `Systran/faster-whisper-base` (multilingual, INT8), `ENGINE_THREADS=4`, `ENGINE_MEM=2g`.
Command: `python bench/run_bench.py --tag baseline-base-v1`.

| Language | Metric | Error rate (mean ± std over 3 repeats) | First draft (median) | Draft lag (median / p95) | Final delay (median / p95) | Max backlog |
|---|---|---|---|---|---|---|
| English | WER | **12.05% ± 0.22** | 3.52 s | 0.94 s / 5.67 s | 4.56 s / 11.65 s | 7.6 s |
| Japanese | CER | **30.22% ± 1.81** | 2.03 s | 1.15 s / 9.74 s | 5.98 s / 12.46 s | 13.3 s |

How to read it:

- **Repeats agree.** The standard deviation is small (0.22 and 1.81 points), so a change of a few points in a later run is real, not noise.
- **English is usable, Japanese is weak.** The best English recording scored 1.8% WER and the worst 27%. Japanese ranged from 17% to 46% CER.
  The `base` model is small, and Japanese scoring is strict (a correct word in a different script counts as errors), so part of that gap is the metric.
- **The engine falls behind on some recordings.** The median draft lag is about one second, but the p95 is 5.7 s (English) and 9.7 s (Japanese).
  The backlog went above 5 s in 5 of 30 English runs and 8 of 30 Japanese runs, peaking at 13 s. Each pass re-transcribes the unfinished part of the sentence,
  so long sentences cost more and more until they are finished.
- **Final delay is long on purpose.** WhisperLive marks a sentence as finished only once the next one starts, so 4.6 s and 6.0 s are mostly that design, not compute.
  The draft lag is the better measure of how live it feels.
- Not compared against published numbers for this model, so there is no claim about how good these scores are in absolute terms.

## Run 2: SenseVoice-Small, 2026-10-24

Same engine image family and host as Run 1, only the model changed: `BACKEND=sensevoice`, `FunAudioLLM/SenseVoiceSmall` (FunASR, fp32 on CPU) with the shared Silero VAD splitting speech pieces,
`ENGINE_THREADS=4`, `ENGINE_MEM=3g`. Command: `python bench/run_bench.py --tag sensevoice-small-v1`.

| Language | Metric | Error rate (mean ± std over 3 repeats) | First draft (median) | Draft lag (median / p95) | Final delay (median / p95) | Max backlog |
|---|---|---|---|---|---|---|
| English | WER | **6.83% ± 0.08** | 0.86 s | 0.45 s / 2.34 s | 3.68 s / 6.12 s | 1.7 s |
| Japanese | CER | **10.72% ± 0.14** | 0.85 s | 0.50 s / 4.65 s | 5.58 s / 9.81 s | 1.5 s |

Against Run 1 (faster-whisper `base`):

| | Run 1 `base` | Run 2 SenseVoice | Change |
|---|---|---|---|
| English WER | 12.05% | 6.83% | 43% fewer errors |
| Japanese CER | 30.22% | 10.72% | 65% fewer errors |
| Draft lag median, en / ja | 0.94 s / 1.15 s | 0.45 s / 0.50 s | about half |
| Draft lag p95, en / ja | 5.67 s / 9.74 s | 2.34 s / 4.65 s | about half |
| Max backlog, en / ja | 7.6 s / 13.3 s | 1.7 s / 1.5 s | the engine keeps up |
| Runs with backlog above 5 s | 5 of 30 en, 8 of 30 ja | 0 of 30, 0 of 30 | |

How to read it:

- **More accurate and faster at the same time.** Both claims hold on this test set. The repeats agree closely (std 0.08 and 0.14 points), so the gap to Run 1 is far outside noise.
- **The engine now keeps up.** The backlog never passed 2 s, where Run 1 reached 13 s. Each pass costs little because SenseVoice is not autoregressive (about 0.07 s of compute per second of audio on this CPU).
- **Still not perfect.** Best recordings score 0%; the worst were `en_08` at 16.9% WER and `ja_03` at 23.2% CER. I have not examined what goes wrong in those.
- **Final delay is still long on purpose** (3.7 s and 5.6 s): WhisperLive only finishes a sentence once the next one starts. Draft lag is the better measure of how live it feels.
- **Memory:** the container peaked at about 2.4 GiB of its 3 GiB cap in `docker stats` samples taken every 30 s, so the true peak may be slightly higher.
- **Caveats.** This is clean read speech (FLEURS), replayed from files; real microphone speech in a noisy room can differ. I do not know whether SenseVoice's training data overlaps with FLEURS-style data. A quick 3-recordings-per-language check before this run showed 0.95% WER and 4.99% CER, which was too optimistic: the first recordings happen to be easy, and only the full set counts.
