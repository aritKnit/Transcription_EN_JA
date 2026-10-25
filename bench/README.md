# Test bench

Replays recordings against the engine at real speed, logs when every message arrives, and scores accuracy and delay.
English is scored with WER and Japanese with CER (Japanese has no spaces, so characters are the fair unit).

## Run it

The engine must be up (`curl localhost:8000/ready` returns 200) and idle: the bench needs the one seat. It talks only the WebSocket protocol, so it measures
whichever model the engine runs (`BACKEND` and `MODEL_DIR` in `.env`); compare models by running it once per model.

```bash
pip install -r bench/requirements.txt
python bench/prepare.py                       # once: builds bench/audio and bench/manifest.csv
python bench/run_bench.py --tag baseline      # all recordings, 3 repeats, about 40 minutes
python bench/run_bench.py --languages ja --limit 2 --repeats 1      # quick check
python bench/replay.py bench/audio/en/en_00.wav --language en       # one recording, prints every update
python bench/score.py bench/results/<run>     # re-score a finished run
```

Results go to `bench/results/<time>-<tag>/` (git-ignored): `runs.jsonl` has every raw message with its arrival time, `summary.csv` the table,
`engine.json` the backend and model the engine reported when the run started. Add the numbers you want to keep to [../results.md](../results.md).
Scoring is separate from replaying, so you can change how things are scored without running the 40 minutes again.

## Test set

[prepare.py](prepare.py) takes sentences from Google FLEURS (`en_us` and `ja_jp` test splits, CC-BY-4.0) with a fixed random seed.
Each recording joins 3 sentences with 0.8 s of silence (about 35 s), so segmentation behaves like live speech.
10 recordings per language, 11.6 minutes in total. [manifest.csv](manifest.csv) lists them with their reference text; the audio itself is not committed
(`prepare.py` rebuilds it identically). The bench replays each recording followed by 6 s of silence so the engine can settle the last sentence.

## What is measured

| Column | Meaning |
|---|---|
| `error_rate_%`, `std_%` | WER (en) or CER (ja) of the whole set, mean and standard deviation over the repeats |
| `first_draft_median_s` | Seconds from where the first text starts in the audio until it appears on screen |
| `draft_lag_median_s`, `draft_lag_p95_s` | While the recording plays: how far behind the audio the newest text is. This is how live it feels |
| `final_delay_median_s`, `final_delay_p95_s` | Seconds from the end of a sentence until it is marked finished. The engine only finishes a sentence once the next one starts, so this is long by design |
| `max_backlog_s` | Highest `backlog_seconds` the engine reported (sampled once a second): audio received but not yet looked at |

Text normalisation before scoring: English uses Whisper's English normaliser (case, punctuation, spelled-out numbers); Japanese uses NFKC and drops everything
except letters, digits and marks. Japanese scores are strict: a correct word written in a different script (for example `1つ` against `一つ`) counts as errors.

Segment times from the engine are rounded by the model, so delays are good to about a second, not to the millisecond.
