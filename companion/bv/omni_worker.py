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


def run(model_path):
    # wsl.exe can outlive its Windows parent. Watching EOF also cancels an in-flight GPU call.
    requests = queue.Queue()

    def read_requests():
        for raw in sys.stdin.buffer:
            requests.put(raw)
        os._exit(0)

    threading.Thread(target=read_requests, daemon=True, name="windows-stdin").start()
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

        torch.set_num_threads(2)
        if not torch.cuda.is_available():
            raise RuntimeError("В WSL недоступна CUDA")
        model = OmniVoice.from_pretrained(model_path, device_map="cuda:0", dtype=torch.float16)

        @functools.lru_cache(maxsize=24)
        def prompt(path, reference):
            return model.create_voice_clone_prompt(ref_audio=path, ref_text=reference)

        torch.cuda.empty_cache()
        reply(dict(gpu=torch.cuda.get_device_name(), rate=model.sampling_rate))
        while True:
            try:
                request = json.loads(requests.get())
                start = time.perf_counter()
                with torch.inference_mode():
                    voice = prompt(request["path"], request["reference"])
                    item = model.generate(text=request["text"], language="ru",
                                          voice_clone_prompt=voice, speed=request["speed"],
                                          num_step=profile_steps(request.get("profile")))[0]
                samples = np.asarray(item, dtype="<f4").reshape(-1).tobytes()
                reply(dict(bytes=len(samples), rate=model.sampling_rate,
                           seconds=time.perf_counter() - start), samples)
            except (OSError, RuntimeError, ValueError) as error:
                reply(dict(error=str(error)))


if __name__ == "__main__":
    run(sys.argv[1])
