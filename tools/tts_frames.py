"""GPU contention proxy from DEVELOPMENT.md: a 60 Hz client in a separate Windows process from F5."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np


def frame_work():
    import onnx
    import onnxruntime as ort
    from onnx import TensorProto, helper

    ort.preload_dlls()
    n = 1536
    def tensor(name):
        return helper.make_tensor_value_info(name, TensorProto.FLOAT, [n, n])
    nodes = [helper.make_node("MatMul", ["x" if i == 0 else f"v{i-1}", "w"], [f"v{i}"]) for i in range(6)]
    graph = helper.make_graph(nodes, "frame", [tensor("x"), tensor("w")], [tensor("v5")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    sess = ort.InferenceSession(model.SerializeToString(), providers=["CUDAExecutionProvider"])
    if sess.get_providers()[0] != "CUDAExecutionProvider":
        raise RuntimeError("Frame workload requires CUDA")
    binding = sess.io_binding()
    rng = np.random.default_rng(42)
    inputs = [ort.OrtValue.ortvalue_from_numpy(rng.standard_normal((n, n), dtype=np.float32) / np.sqrt(np.float32(n)),
                                               "cuda", 0) for _ in range(2)]
    for name, value in zip(("x", "w"), inputs):
        binding.bind_ortvalue_input(name, value)
    output = ort.OrtValue.ortvalue_from_shape_and_type((n, n), np.float32, "cuda", 0)
    binding.bind_ortvalue_output("v5", output)
    def draw():
        sess.run_with_iobinding(binding)
        binding.synchronize_outputs()
    return draw


def measure(draw, running, record):
    durations = []
    due = time.perf_counter()
    while running():
        time.sleep(max(0, due - time.perf_counter()))
        start = time.perf_counter()
        draw()
        end = time.perf_counter()
        if record():
            durations.append((end - start) * 1000)
        due = max(end, due + 1/60)
    if not durations:
        raise ValueError("No measured frames")
    return dict(n=len(durations), p50_ms=float(np.median(durations)),
                p95_ms=float(np.percentile(durations, 95)), p99_ms=float(np.percentile(durations, 99)),
                max_ms=max(durations), over_16ms=sum(t > 1000/60 for t in durations),
                over_33ms=sum(t > 1000/30 for t in durations))


def main():
    if "--help" in sys.argv or len(sys.argv) == 1:
        print("tts_frames.py accepts tts_bench.py arguments. It reports an idle and a concurrent frame sample.")
        return
    draw = frame_work()
    for _ in range(20):
        draw()
    until = time.perf_counter() + 10
    idle = measure(draw, lambda: time.perf_counter() < until, lambda: True)
    active = threading.Event()
    with subprocess.Popen([sys.executable, "-u", str(Path(__file__).with_name("tts_bench.py")), *sys.argv[1:]],
                          stdout=subprocess.PIPE, text=True, encoding="utf-8",
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"}) as child:
        def receive():
            for line in child.stdout:
                row = json.loads(line)
                if row.get("size") == "cold":
                    active.set()
        reader = threading.Thread(target=receive)
        reader.start()
        busy = measure(draw, lambda: child.poll() is None, active.is_set)
        reader.join()
        if child.returncode:
            raise SystemExit(child.returncode)
    print(json.dumps(dict(idle=idle, concurrent=busy)))


if __name__ == "__main__":
    main()
