"""Binary stdin/stdout transport for the isolated OmniVoice CUDA environment."""
from __future__ import annotations

import contextlib
import functools
import json
import os
import queue
import sys
import threading
import time


def profile_steps(profile):
    return 24 if profile == "balanced" else 32


def cuda_device(torch):
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA недоступна для OmniVoice")
    devices = sorted(range(torch.cuda.device_count()),
                     key=lambda index: torch.cuda.get_device_properties(index).total_memory, reverse=True)
    for index in devices:
        try:
            torch.ones(1, device=f"cuda:{index}", dtype=torch.float16).sum().item()
        except RuntimeError as error:
            print(f"CUDA {index}: {error}", file=sys.stderr, flush=True)
            continue
        return index
    raise RuntimeError("Ни одна из видеокарт NVIDIA не выполнила проверку CUDA FP16")


def run(model_path):
    # wsl.exe can outlive its Windows parent. Watching EOF also cancels an in-flight GPU call.
    requests = queue.Queue()

    def read_requests():
        for raw in sys.stdin.buffer:
            requests.put(raw)
        os._exit(0)

    reader = threading.Thread(target=read_requests, daemon=True, name="windows-stdin")
    if os.name != "nt":
        reader.start()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    pipe = sys.stdout.buffer

    def reply(message, samples=b""):
        pipe.write(json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n" + samples)
        pipe.flush()

    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import torch
        from omnivoice import OmniVoice

        # The Windows NumPy DLL can hang during import if a thread is already reading a pipe.
        if os.name == "nt":
            reader.start()
        torch.set_num_threads(2)
        index = cuda_device(torch)
        with torch.cuda.device(index):
            model = OmniVoice.from_pretrained(model_path, device_map=f"cuda:{index}", dtype=torch.float16)

        @functools.lru_cache(maxsize=24)
        def prompt(path, reference):
            return model.create_voice_clone_prompt(ref_audio=path, ref_text=reference)

        torch.cuda.empty_cache()
        reply(dict(gpu=torch.cuda.get_device_name(index), rate=model.sampling_rate))
        while True:
            try:
                request = json.loads(requests.get())
                start = time.perf_counter()
                with torch.inference_mode():
                    voice = prompt(request["path"], request["reference"])
                    item = model.generate(text=request["text"], language="ru",
                                          voice_clone_prompt=voice, speed=request["speed"],
                                          num_step=profile_steps(request.get("profile")),
                                          position_temperature=0.0)[0]
                samples = np.asarray(item, dtype="<f4").reshape(-1).tobytes()
                reply(dict(bytes=len(samples), rate=model.sampling_rate,
                           seconds=time.perf_counter() - start), samples)
            except (OSError, RuntimeError, ValueError) as error:
                reply(dict(error=str(error)))


if __name__ == "__main__":
    if sys.argv[1] == "--probe":
        import torch
        import torchaudio  # noqa: F401 - verify its native DLLs too

        print(torch.cuda.get_device_name(cuda_device(torch)))
    else:
        run(sys.argv[1])
