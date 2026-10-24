# Baseline: base model, one seat

First measurement of the engine through the real-time bench. Every later change (gateway, other models, settings) is compared against these numbers.

**Setup:** engine from commit `7286891`, model `Systran/faster-whisper-base` (multilingual, INT8), `MAX_CLIENTS=1`, `ENGINE_THREADS=4`, `ENGINE_CPUS=4`, `ENGINE_MEM=2g`.
Host: WSL2, 16 logical CPUs, about 6 GB RAM. Test set: 10 English and 10 Japanese recordings of about 35 s from Google FLEURS (see [README.md](README.md)), replayed at real speed, 3 repeats.
Run: `python bench/run_bench.py --tag baseline-base-v1` on 2026-10-24.

| Language | Metric | Error rate (mean ± std over 3 repeats) | First draft (median) | Draft lag (median / p95) | Final delay (median / p95) | Max backlog |
|---|---|---|---|---|---|---|
| English | WER | **12.05% ± 0.22** | 3.52 s | 0.94 s / 5.67 s | 4.56 s / 11.65 s | 7.6 s |
| Japanese | CER | **30.22% ± 1.81** | 2.03 s | 1.15 s / 9.74 s | 5.98 s / 12.46 s | 13.3 s |

## Reading the numbers

- **Repeats agree.** The standard deviation is small (0.22 and 1.81 points), so the differences between runs are not noise and a change of a few points in a later run is real.
- **English is usable, Japanese is weak.** The best English recording scored 1.8% WER and the worst 27%. Japanese ranged from 17% to 46% CER. The `base` model is small; Japanese scoring is also strict (a correct word in a different script counts as errors), so part of that gap is the metric, not only the model. Next step to try: `small` (multilingual) and compare with this table.
- **The engine falls behind on some recordings.** The median draft lag is about one second, but the p95 is 5.7 s (English) and 9.7 s (Japanese). The backlog went above 5 s in 5 of 30 English runs and 8 of 30 Japanese runs, peaking at 13 s. Each pass re-transcribes the unfinished part of the sentence, so long sentences cost more and more until they are finished. This is the number to beat when tuning threads, model size or chunking, and the one the busy notice in Phase 4 should use.
- **Final delay is long on purpose.** WhisperLive marks a sentence as finished only once the next one starts, so 4.6 s and 6.0 s are mostly that design, not compute. The draft lag is the better measure of how live it feels.
- Not yet compared against published numbers for this model, so there is no claim here about how good these scores are in absolute terms.

Raw per-message data for this run was kept in `bench/results/` on the machine that ran it (git-ignored).
