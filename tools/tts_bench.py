"""Replay isolated messages on the native Windows companion and time their first speech block."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "companion"))
clock = time.perf_counter


def run(args, work):
    started = clock()
    os.environ.update(HF_HOME=str(args.hf.resolve()), HF_HUB_OFFLINE="1", BV_DATA=str(work))
    import numpy as np
    import onnxruntime as ort
    import sounddevice as sd
    import win32api
    import win32process
    from bv import __main__ as app, audio, f5engine, gamevoices, share, yo
    from bv.dictionary import Dictionary
    from bv.morph import Inflector
    from bv.protocol import Line

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    def gpu():
        return subprocess.check_output(["nvidia-smi", "-i", "0", "--query-gpu=name,driver_version,memory.used,memory.total",
                                        "--format=csv,noheader,nounits"], text=True).strip().split(", ")

    initial_gpu = gpu()
    config = dict(app.DEFAULT_CONFIG, f5_profile=args.profile)
    share.limit_onnx(config["onnx_threads"])
    share.lower_priority(log)
    ort.set_seed(42)
    manifest = args.bank.read_bytes()
    slots = gamevoices.load(args.bank.parent, json.loads(manifest)["stamp"])
    if not slots:
        raise ValueError("A complete voice bank with readable reference files is required")
    # Use the installed references read-only. The synthetic players' assignments live in work.
    f5engine.make_bank = lambda *a: slots
    dictionary = Dictionary(config, log, ROOT / "addon" / "BarrensVoice")
    dictionary.from_addon = "benchmark"
    dictionary.refresh()
    inflector = Inflector(log)
    inflector.load()
    yo.table()
    ready = threading.Event()
    completed = threading.Condition()
    played_chunks = 0
    row = {}
    stages = defaultdict(list)
    chunks = []
    first_block = None
    output_stream = sd.OutputStream

    def stream(**kwargs):
        callback = kwargs.pop("callback")

        def observe(out, frames, times, status):
            nonlocal first_block
            now = clock()
            try:
                callback(out, frames, times, status)
            finally:
                if first_block is None:
                    audible = np.flatnonzero(np.abs(out[:, 0]) > 0.001)
                    if len(audible):
                        first_block = now
                        row["callback_ms"] = (now - row["received_s"]) * 1000
                        if times.outputBufferDacTime > 0 and times.currentTime > 0:
                            row["dac_ms"] = row["callback_ms"] + 1000 * (
                                times.outputBufferDacTime - times.currentTime + audible[0] / kwargs["samplerate"])
                out.fill(0)  # keep real device timing without speaking the corpus through the user's speakers
        return output_stream(callback=observe, **kwargs)

    sd.OutputStream = stream
    output = audio.StreamOut()

    class ProbeOutput:
        def play(self, data, level, stopped, length):
            nonlocal played_chunks
            row.setdefault("play_queue_ms", (clock() - chunks[0][0]) * 1000)
            samples, sr = audio.samples_of(data)
            # Isolated messages: submit the first 1/2 second to the normal device, retain the full PCM for QA.
            prefix = audio.wav((samples[:sr // 2] * 32768).clip(-32768, 32767).astype("<i2").tobytes(), sr)
            output.play(prefix, level, stopped, audio.seconds(prefix))
            with completed:
                played_chunks += 1
                completed.notify()

    class Input(queue.Queue):
        def get(self, *a, **kw):
            ready.set()  # all chunks of the previous message have now been handed to Player
            line = super().get(*a, **kw)
            if line is not None:
                row["synth_queue_ms"] = (clock() - row["queued_s"]) * 1000
            return line

    class Player(audio.Player):
        def play(self, data, generation=None):
            chunks.append((clock(), data))
            super().play(data, generation)

    player = Player(out=ProbeOutput(), log=log)
    player.start()
    speaker = f5engine.F5Speaker(config, log, work, player=player)
    speaker.lines = Input()
    speaker.start()
    if not speaker.ready.wait(300) or speaker.error or not ready.wait(5):
        raise RuntimeError(speaker.error or "F5 load timeout")
    if not speaker.accentor.enabled:
        raise RuntimeError("RUAccent is unavailable")

    def timed(obj, attr, label):
        call = getattr(obj, attr)
        def measured(*a, **kw):
            t = clock()
            result = call(*a, **kw)
            stages[label].append((clock() - t) * 1000)
            return result
        setattr(obj, attr, measured)

    for obj, attr, label in [(speaker.accentor, "accent", "ruaccent"), (f5engine, "numbers_to_words", "numbers"),
                             (f5engine, "shape", "intonation"), (f5engine, "wav_bytes", "pcm"),
                             (speaker.onnx.pre, "run", "pre"), (speaker.onnx.pre, "run_with_iobinding", "pre"),
                             (speaker.onnx.net, "run_with_iobinding", "transformer"),
                             (speaker.onnx.dec, "run", "decode"), (speaker.onnx.dec, "run_with_iobinding", "decode")]:
        timed(obj, attr, label)

    corpus = Path(__file__).with_name("tts-corpus.txt").read_bytes()
    header = dict(profile=args.profile, startup_s=clock() - started, gpu=initial_gpu, ort=ort.__version__,
                  device=str(sd.query_devices(output.device)["name"]), provider=speaker.provider,
                  steps=speaker.onnx.steps, bank_sha256=hashlib.sha256(manifest).hexdigest(),
                  corpus_sha256=hashlib.sha256(corpus).hexdigest())
    print(json.dumps(header), flush=True)
    pairs = [line.split("|") for line in corpus.decode().splitlines()]
    cast = [("Громмаш", "Orc", "2"), ("Джайна", "Human", "3"), ("Брэнн", "Dwarf", "2")]
    for i, pair in enumerate(pairs[:args.pairs]):
        for col, raw in enumerate(pair):
            sender, race, sex = cast[(i + col) % len(cast)]
            line = Line("say", sender, race, sex, "", "", raw)
            voice = speaker.describe(line)
            ready.clear()
            played_chunks = 0
            stages.clear()
            chunks.clear()
            first_block = None
            row = dict(id=i, size=("short", "long")[col], voice=voice, received_s=clock())
            line = dataclasses.replace(line, text=app.speech_text(raw, dictionary, inflector, True, yo.restore))
            row["queued_s"] = clock()
            row["text_ms"] = (row["queued_s"] - row["received_s"]) * 1000
            speaker.say(line)
            if not ready.wait(90):
                raise RuntimeError(f"Synthesis timeout for {i}/{col}")
            with completed:
                if not completed.wait_for(lambda: played_chunks == len(chunks), timeout=30) or first_block is None:
                    raise RuntimeError(f"No completed speech for {i}/{col}")
            pcm = [audio.samples_of(data) for _, data in chunks]
            sr = pcm[0][1]
            samples = np.concatenate([part for part, rate in pcm if rate == sr])
            if not np.isfinite(samples).all() or np.max(np.abs(samples)) < 0.01:
                raise ValueError(f"Invalid PCM for {i}/{col}")
            if args.wav_dir:
                import soundfile as sf
                args.wav_dir.mkdir(exist_ok=True, parents=True)
                sf.write(args.wav_dir / f"{i:02}-{col}.wav", samples, sr)
            elapsed = chunks[-1][0] - row["queued_s"]
            row.update(stages=dict(stages), audio_s=len(samples)/sr, rtf=elapsed/(len(samples)/sr),
                       rss_peak_mib=win32process.GetProcessMemoryInfo(win32api.GetCurrentProcess())["PeakWorkingSetSize"]/2**20,
                       gpu_used_mib=int(gpu()[2]), chunks=len(chunks))
            print(json.dumps(row, ensure_ascii=False), flush=True)
    speaker.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, required=True)
    parser.add_argument("--hf", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=30, choices=range(1, 31))
    parser.add_argument("--profile", choices=("quality", "balanced", "fastest"), default="quality")
    parser.add_argument("--wav-dir", type=Path)
    with tempfile.TemporaryDirectory() as tmp:
        run(parser.parse_args(), Path(tmp))
