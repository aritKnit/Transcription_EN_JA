"""Build the test set from Google FLEURS (CC-BY-4.0): English (en_us) and Japanese (ja_jp).

Each recording joins a few consecutive utterances with a pause, so the replay exercises
sentence segmentation like live speech does. Output goes to bench/audio/ and
bench/manifest.csv; both are git-ignored apart from the manifest.

    python bench/prepare.py                       # 10 recordings of 3 utterances per language
    python bench/prepare.py --recordings 4 --per-recording 2
"""
import argparse
import csv
import io
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
import soxr
from huggingface_hub import hf_hub_download

ROOT = Path(__file__).parent
SAMPLE_RATE = 16000
LANGUAGES = {"en": "en_us", "ja": "ja_jp"}


def load_audio(blob):
    audio, rate = sf.read(io.BytesIO(blob), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != SAMPLE_RATE:
        audio = soxr.resample(audio, rate, SAMPLE_RATE)
    return audio


def build_language(code, fleurs_name, recordings, per_recording, pause_s, seed):
    parquet = hf_hub_download(
        "google/fleurs", f"parquet-data/{fleurs_name}/test-00000-of-00001.parquet",
        repo_type="dataset", local_dir=ROOT / "data",
    )
    rows = pq.read_table(parquet, columns=["audio", "raw_transcription"]).to_pylist()
    picks = np.random.default_rng(seed).permutation(len(rows))[: recordings * per_recording]
    pause = np.zeros(int(pause_s * SAMPLE_RATE), dtype=np.float32)
    out_dir = ROOT / "audio" / code
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for n in range(recordings):
        utterances = [rows[i] for i in picks[n * per_recording:(n + 1) * per_recording]]
        pieces = []
        for u in utterances:
            pieces += [load_audio(u["audio"]["bytes"]), pause]
        audio = np.concatenate(pieces[:-1])
        wav = out_dir / f"{code}_{n:02d}.wav"
        sf.write(wav, audio, SAMPLE_RATE, subtype="PCM_16")
        joiner = " " if code == "en" else ""
        manifest.append({
            "name": wav.stem,
            "lang": code,
            "wav": str(wav.relative_to(ROOT)),
            "seconds": round(len(audio) / SAMPLE_RATE, 2),
            "reference": joiner.join(u["raw_transcription"].strip() for u in utterances),
        })
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recordings", type=int, default=10, help="recordings per language")
    parser.add_argument("--per-recording", type=int, default=3, help="utterances joined into one recording")
    parser.add_argument("--pause", type=float, default=0.8, help="silence between utterances, seconds")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    manifest = []
    for code, fleurs_name in LANGUAGES.items():
        manifest += build_language(code, fleurs_name, args.recordings, args.per_recording, args.pause, args.seed)
    with open(ROOT / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "lang", "wav", "seconds", "reference"])
        writer.writeheader()
        writer.writerows(manifest)
    total = sum(m["seconds"] for m in manifest)
    print(f"wrote {len(manifest)} recordings, {total / 60:.1f} min of audio, to {ROOT / 'audio'}")


if __name__ == "__main__":
    main()
