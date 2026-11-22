"""Summarize complete comparable runs from tts_bench as Markdown (p50/p95, linear interpolation)."""
import argparse
import gzip
import json
from pathlib import Path

import numpy as np


def read(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        header, *rows = [json.loads(line) for line in stream]
    cold = [r for r in rows if r["size"] == "cold"]
    warm = [r for r in rows if r["size"] != "cold"]
    if [(r["id"], r["size"]) for r in warm] != [(i, s) for i in range(30) for s in ("short", "long")]:
        raise ValueError(f"{path}: expected all 30 short and 30 long messages in corpus order")
    if len(cold) != 1 or any("dac_ms" not in r for r in rows):
        raise ValueError(f"{path}: missing cold message or device timestamps")
    for row in warm:
        row["total_rtf"] = row["ready_ms"][-1] / (1000 * row["audio_s"])
    identity = (header["bank_sha256"], header["corpus_sha256"], header["gpu"][:2], header["gpu"][3:],
                header["ort"], header["device"], header["provider"], header["full_playback"],
                [r["voice"] for r in warm])
    return identity, cold[0], warm


def pair(values):
    p50, p95 = np.percentile(values, [50, 95])
    return f"{p50:.2f} / {p95:.2f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    reference = None
    for path in args.runs:
        identity, cold, warm = read(path)
        if reference is not None and identity != reference:
            raise ValueError(f"{path}: different corpus, voices, device, runtime or playback mode")
        reference = identity
        print(f"\n### {path.name}\n\nProcess start to first scheduled speech: {cold['start_to_dac_ms']:.0f} ms.\n")
        print("| Measure | Short p50 / p95 | Long p50 / p95 |\n|---|---:|---:|")
        groups = [[r for r in warm if r["size"] == size] for size in ("short", "long")]
        for key in ("dac_ms", "callback_ms", "synth_queue_ms", "play_queue_ms", "total_rtf", "process_vram_mib", "rss_peak_mib"):
            cells = [pair([r[key] for r in group]) for group in groups]
            print(f"| {key} | {' | '.join(cells)} |")
        for key in ("text", "numbers", "ruaccent", "pre", "transformer", "decode", "intonation", "pcm"):
            cells = [pair([sum(r["stages"][key]) for r in group]) for group in groups]
            print(f"| {key} (ms per line) | {' | '.join(cells)} |")
        cells = [pair([step for r in group for step in r["stages"]["transformer"]]) for group in groups]
        print(f"| Transformer (ms per step) | {' | '.join(cells)} |")


if __name__ == "__main__":
    main()
