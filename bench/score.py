"""Turn a replay's raw events into text, delays and error rates.

    python bench/score.py bench/results/<run>      # re-score a finished run
"""
import argparse
import json
import re
import statistics
import sys
import unicodedata
from pathlib import Path

import jiwer
import pandas as pd
from whisper_normalizer.english import EnglishTextNormalizer

_english = EnglishTextNormalizer()
_SPACES = re.compile(r"\s+")


def hypothesis(events):
    """Full transcript the engine produced: finished segments plus the last unfinished one."""
    finished = {}
    partial = None
    for event in events:
        for seg in event["segments"]:
            if seg.get("completed"):
                finished[(seg["start"], seg["end"])] = seg["text"]
            else:
                partial = seg["text"]
    texts = [finished[key] for key in sorted(finished, key=lambda k: (float(k[0]), float(k[1])))]
    if partial and (not texts or partial.strip() != texts[-1].strip()):
        texts.append(partial)
    return " ".join(t.strip() for t in texts)


def delays(events, audio_s):
    """First-draft delay, draft lag and the delay of every finished segment, in seconds.

    The recording is replayed at real speed, so a message at ``t`` has seen ``t`` seconds of
    audio. First draft: ``t`` minus where that first text starts. Draft lag: while the
    recording plays, ``t`` minus where the newest text ends, i.e. how stale the screen is.
    Finished segment: ``t`` the first time it shows up as finished, minus where it ends. The
    engine only finishes a segment once the next one starts, so this one is long by design.
    """
    first_draft = None
    final = []
    draft_lag = []
    seen = set()
    for event in events:
        segments = event["segments"]
        if event["t"] <= audio_s and segments and segments[-1]["text"].strip():
            draft_lag.append(event["t"] - float(segments[-1]["end"]))
        if first_draft is None and any(s["text"].strip() for s in segments):
            first_draft = event["t"] - float(segments[0]["start"])
        for seg in segments:
            key = (seg["start"], seg["end"])
            if seg.get("completed") and key not in seen:
                seen.add(key)
                final.append(event["t"] - float(seg["end"]))
    return {"first_draft_s": first_draft, "draft_lags_s": draft_lag, "final_delays_s": final}


def normalize(text, lang):
    """English: Whisper's English normalizer. Japanese: NFKC, then keep letters, digits and marks only."""
    if lang == "en":
        return _SPACES.sub(" ", _english(text)).strip()
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in "LNM")


def error_rate(refs, hyps, lang):
    """Pooled WER (English) or CER (Japanese) over parallel lists of texts."""
    refs = [normalize(r, lang) for r in refs]
    hyps = [normalize(h, lang) for h in hyps]
    return (jiwer.wer if lang == "en" else jiwer.cer)(refs, hyps)


def metric_name(lang):
    return "WER" if lang == "en" else "CER"


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))] if values else float("nan")


def summarize(runs):
    """One row per language: error rate mean and std over repeats, delays, backlog."""
    rows = []
    for lang in sorted({r["lang"] for r in runs}):
        lang_runs = [r for r in runs if r["lang"] == lang]
        per_repeat = []
        for repeat in sorted({r["repeat"] for r in lang_runs}):
            batch = [r for r in lang_runs if r["repeat"] == repeat]
            per_repeat.append(error_rate([r["reference"] for r in batch], [r["hypothesis"] for r in batch], lang))
        final = [d for r in lang_runs for d in r["final_delays_s"]]
        lag = [d for r in lang_runs for d in r["draft_lags_s"]]
        first = [r["first_draft_s"] for r in lang_runs if r["first_draft_s"] is not None]
        rows.append({
            "language": lang,
            "metric": metric_name(lang),
            "error_rate_%": round(100 * statistics.mean(per_repeat), 2),
            "std_%": round(100 * statistics.stdev(per_repeat), 2) if len(per_repeat) > 1 else 0.0,
            "repeats": len(per_repeat),
            "recordings": len({r["name"] for r in lang_runs}),
            "first_draft_median_s": round(statistics.median(first), 2) if first else None,
            "draft_lag_median_s": round(statistics.median(lag), 2) if lag else None,
            "draft_lag_p95_s": round(percentile(lag, 0.95), 2) if lag else None,
            "final_delay_median_s": round(statistics.median(final), 2) if final else None,
            "final_delay_p95_s": round(percentile(final, 0.95), 2) if final else None,
            "max_backlog_s": round(max(r["max_backlog_s"] for r in lang_runs), 2),
        })
    return pd.DataFrame(rows)


def load_runs(run_dir):
    return [json.loads(line) for line in (Path(run_dir) / "runs.jsonl").read_text(encoding="utf-8").splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir")
    args = parser.parse_args()
    runs = load_runs(args.run_dir)
    for run in runs:
        run.update(delays(run["events"], run["audio_s"]))
        run["hypothesis"] = hypothesis(run["events"])
    table = summarize(runs)
    table.to_csv(Path(args.run_dir) / "summary.csv", index=False)
    print(table.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
