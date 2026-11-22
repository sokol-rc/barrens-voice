"""Score the full (unmuted) PCM retained by tts_bench, outside the timed process."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "companion"))



def waveform_checks(pcm, rate, expected):
    import numpy as np
    import parselmouth
    from bv.intonation import is_yes_no, pitch_range

    sounding = np.flatnonzero(pcm)
    if not sounding.size or not np.isfinite(pcm).all():
        raise ValueError("Silent or non-finite recording")
    # Check both edges of each exact digital pause of at least 20 ms, including joins.
    boundaries = np.flatnonzero(np.diff(sounding) >= rate * .02)
    indices = np.r_[sounding[0], sounding[-1], sounding[boundaries], sounding[boundaries + 1]]
    result = dict(peak=float(np.abs(pcm).max()), rms=float(np.sqrt(np.mean(pcm**2))),
                  boundary_step=float(np.abs(pcm[indices]).max()))
    if is_yes_no(expected):
        snd = parselmouth.Sound(pcm, sampling_frequency=rate)
        low, high = pitch_range(snd)
        f0 = snd.to_pitch(pitch_floor=low, pitch_ceiling=high).selected_array["frequency"]
        voiced = f0[f0 > 0]
        if len(voiced) > 15:
            result["end_rise_st"] = float(12 * np.log2(np.median(voiced[-5:]) / np.median(voiced[:-5])))
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hf", type=Path, required=True)
    parser.add_argument("wav_dirs", type=Path, nargs="+")
    args = parser.parse_args()
    os.environ.update(HF_HOME=str(args.hf.resolve()), HF_HUB_OFFLINE="1")
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
                                  **waveform_checks(pcm, rate, expected)),
                             ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
