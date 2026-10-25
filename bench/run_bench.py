"""Replay the whole test set against the engine, several times, and print the scores.

Runs in real time, one recording at a time, so 3 repeats of the default set take about 40 minutes.

    python bench/run_bench.py --tag baseline
    python bench/run_bench.py --languages en --limit 2 --repeats 1     # quick check
"""
import argparse
import csv
import json
import time
import urllib.request
from pathlib import Path

import score
from replay import EngineBusy, replay

ROOT = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--languages", nargs="+", default=["en", "ja"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None, help="recordings per language")
    parser.add_argument("--tag", default="run")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--health-url", default="http://localhost:8000/health")
    args = parser.parse_args()

    with open(ROOT / "manifest.csv", encoding="utf-8") as f:
        manifest = [row for row in csv.DictReader(f) if row["lang"] in args.languages]
    if args.limit:
        manifest = [row for lang in args.languages
                    for row in [m for m in manifest if m["lang"] == lang][: args.limit]]

    with urllib.request.urlopen(args.health_url, timeout=5) as response:
        engine = json.load(response)
    run_dir = ROOT / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.tag}"
    run_dir.mkdir(parents=True)
    (run_dir / "engine.json").write_text(json.dumps(engine, indent=2))
    print(f"engine: backend {engine['backend']}, model {engine['model']}", flush=True)
    runs = []
    with open(run_dir / "runs.jsonl", "w", encoding="utf-8") as out:
        for repeat in range(args.repeats):
            for row in manifest:
                try:
                    result = replay(ROOT / row["wav"], row["lang"], args.host, args.port, args.health_url)
                except EngineBusy as e:
                    raise SystemExit(f"{e}; stop other sessions and retry")
                run = {"name": row["name"], "lang": row["lang"], "repeat": repeat,
                       "reference": row["reference"], "audio_s": result["audio_s"],
                       "max_backlog_s": result["max_backlog_s"], "events": result["events"]}
                out.write(json.dumps(run, ensure_ascii=False) + "\n")
                out.flush()
                run.update(score.delays(run["events"], run["audio_s"]))
                run["hypothesis"] = score.hypothesis(run["events"])
                runs.append(run)
                error = score.error_rate([run["reference"]], [run["hypothesis"]], run["lang"])
                print(f"repeat {repeat} {row['name']}: {score.metric_name(row['lang'])} {100 * error:.1f}%  "
                      f"first draft {run['first_draft_s']:.2f}s  max backlog {run['max_backlog_s']:.1f}s", flush=True)

    table = score.summarize(runs)
    table.to_csv(run_dir / "summary.csv", index=False)
    print()
    print(table.to_string(index=False))
    print(f"\nraw events and summary: {run_dir}")


if __name__ == "__main__":
    main()
