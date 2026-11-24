"""The managed Windows CUDA environment for OmniVoice, installed alongside the ONNX fallback."""
from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path

from .paths import CACHE_DIR, CODE_DIR, INSTALL_DIR

MODEL_REPO = "k2-fsa/OmniVoice"
MODEL_REVISION = "3fc979d1cd72492cd679c5ac2611e0d7ac047ba1"
MODEL_FILES = (
    "config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja",
    "audio_tokenizer/config.json", "audio_tokenizer/model.safetensors", "audio_tokenizer/preprocessor_config.json",
    "audio_tokenizer/LICENSE",
)
PROJECT = CODE_DIR / "omni-runtime"
MODEL_DIR = CACHE_DIR / "omnivoice" / MODEL_REVISION


@lru_cache(maxsize=1)
def environment() -> Path:
    version = hashlib.sha256((PROJECT / "uv.lock").read_bytes()).hexdigest()[:12]
    return CACHE_DIR / "omnivoice" / f"runtime-{version}"


def interpreter() -> Path:
    return environment() / "Scripts" / "python.exe"


def native_only() -> bool:
    from .install import runtime_of

    return INSTALL_DIR is not None and runtime_of(INSTALL_DIR) == "cuda"


def ready() -> bool:
    if INSTALL_DIR is None or not (PROJECT / "uv.lock").is_file():
        return False
    return (environment() / ".ready").is_file() and interpreter().is_file() and all(
        (MODEL_DIR / file).is_file() for file in MODEL_FILES)


def selected(config: dict) -> bool:
    backend = config.get("race_voice_backend", "auto")
    return native_only() or backend == "omnivoice" or (backend == "auto" and ready())


@lru_cache(maxsize=1)
def preload_whisper_cuda():
    """Share the locked PyTorch wheel's CUDA/cuDNN libraries with ONNX in the companion process."""
    import os
    import onnxruntime as ort

    directory = environment() / "Lib" / "site-packages" / "torch" / "lib"
    handle = os.add_dll_directory(str(directory))
    try:
        ort.preload_dlls(directory=str(directory))
    except BaseException:
        handle.close()
        raise
    return handle  # keep DLL lookup active for cuDNN kernels loaded on demand


def prepare(log) -> bool:
    """Install the pinned runtime and model once, then verify the CUDA device."""
    from .install import runtime_of, uv_env

    if INSTALL_DIR is None or runtime_of(INSTALL_DIR) != "cuda":
        return False
    prepared = ready()
    env = uv_env(INSTALL_DIR, PROJECT)
    env["UV_PROJECT_ENVIRONMENT"] = str(environment())
    print("BV_SETUP: cuda", flush=True)
    log("устанавливаю OmniVoice для Windows CUDA …")
    subprocess.run([str(INSTALL_DIR / "uv.exe"), "sync", "--frozen", "--no-dev", "--project", str(PROJECT),
                    "--python", "3.12"], env=env, check=True)
    try:
        subprocess.run([str(interpreter()), str(Path(__file__).with_name("omni_worker.py")), "--probe"],
                       check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print("BV_SETUP: cuda-error", flush=True)
        raise RuntimeError("Проверка CUDA FP16 не прошла. Проверь драйвер NVIDIA и доступную видеопамять.") from error
    if prepared:
        log("OmniVoice для Windows CUDA уже подготовлен.")
        return True

    from huggingface_hub import snapshot_download

    print("BV_SETUP: model", flush=True)
    log("скачиваю модель OmniVoice …")
    snapshot_download(MODEL_REPO, revision=MODEL_REVISION, allow_patterns=MODEL_FILES, local_dir=MODEL_DIR)
    missing = [name for name in MODEL_FILES if not (MODEL_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(f"не скачаны файлы OmniVoice: {', '.join(missing)}")
    (environment() / ".ready").touch()
    log("OmniVoice для Windows CUDA подготовлен.")
    return True
