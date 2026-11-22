"""Optional native TensorRT 10 for the known F5 Transformer. CUDA still runs Preprocess and Decode."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

PROVIDER = "TensorrtExecutionProvider"


def shapes(frames: int) -> str:
    return ",".join((f"noise:1x{frames}x100", f"rope_cos:2x1x{frames}x64", f"rope_sin:2x1x{frames}x64",
                     f"cat_mel_text:1x{frames}x612", f"cat_mel_text_drop:1x{frames}x612"))


def session(model: Path, directory: Path, options, cuda, max_frames: int):
    import ctypes
    import onnxruntime as ort
    from .f5engine import on_card

    if PROVIDER not in ort.get_available_providers():
        raise RuntimeError("эта сборка ONNX Runtime не поддерживает TensorRT")
    directory = directory.resolve(strict=True)
    libraries = [directory / name for name in ("nvinfer_10.dll", "nvinfer_plugin_10.dll", "nvonnxparser_10.dll")]
    versions = [(dll.stat().st_size, dll.stat().st_mtime_ns) for dll in libraries]
    settings = dict(trt_fp16_enable=True, trt_max_workspace_size=2**30, trt_builder_optimization_level=2,
                    trt_profile_min_shapes=shapes(1), trt_profile_opt_shapes=shapes(min(900, max_frames)),
                    trt_profile_max_shapes=shapes(max_frames))
    # Compiled engines depend on the GPU, driver, runtime and builder options. Without a device
    # identifier we can still build in memory, but cannot safely reuse another machine's cache.
    try:
        adapter = subprocess.check_output(["nvidia-smi", "-i", "0", "--query-gpu=uuid,driver_version",
                                           "--format=csv,noheader"], timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        adapter = None
    if adapter:
        key = hashlib.sha256(repr((adapter, ort.__version__, versions, settings, model.stem)).encode()).hexdigest()[:20]
        cache = model.parent / f"trt-{key}"
        cache.mkdir(exist_ok=True)
        settings.update(trt_engine_cache_enable=True, trt_engine_cache_path=str(cache))
    with os.add_dll_directory(str(directory)):
        libraries_loaded = [ctypes.WinDLL(str(dll)) for dll in libraries]
        net = ort.InferenceSession(str(model), options, providers=[(PROVIDER, settings), *cuda])
        on_card(net, PROVIDER)
    net._bv_trt_libraries = libraries_loaded  # the DLLs must outlive this session
    return net
