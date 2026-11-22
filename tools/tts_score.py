"""Score the full (unmuted) PCM retained by tts_bench, outside the timed process."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "companion"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hf", type=Path, required=True)
    parser.add_argument("wav_dirs", type=Path, nargs="+")
    args = parser.parse_args()
    os.environ.update(HF_HOME=str(args.hf.resolve()), HF_HUB_OFFLINE="1")
    import numpy as np
    import soundfile as sf
    from bv import f5engine, share
    from bv.whisper_onnx import OnnxChecker

    share.limit_onnx()
    provider = f5engine.provider({})
    f5engine.preload(provider)
    checker = OnnxChecker(lambda msg: print(msg, file=sys.stderr), provider)
    if not checker.load():
        raise RuntimeError("Whisper is unavailable")
    corpus = Path(__file__).with_name("tts-corpus.txt").read_text(encoding="utf-8").splitlines()
    for folder in args.wav_dirs:
        files = sorted(folder.glob("*.wav"))
        if not files:
            raise ValueError(f"No recordings in {folder}")
        for path in files:
            i, col = map(int, path.stem.split("-"))
            expected = corpus[i].split("|")[col]
            pcm, rate = sf.read(path, dtype="float32")
            similarity, heard = checker.score(pcm, rate, expected)
            print(json.dumps(dict(run=folder.name, id=i, size=("short", "long")[col], heard=heard,
                                  similarity=similarity, ending=f5engine.ending_heard(expected, heard),
                                  peak=float(np.max(np.abs(pcm))), last_sample=float(pcm[-1])),
                             ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
