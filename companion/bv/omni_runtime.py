"""The managed Windows CUDA environment for OmniVoice, installed alongside the ONNX fallback."""
from __future__ import annotations

import hashlib
import shutil
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


def ready() -> bool:
    if INSTALL_DIR is None or not (PROJECT / "uv.lock").is_file():
        return False
    return (environment() / ".ready").is_file() and interpreter().is_file() and all(
        (MODEL_DIR / file).is_file() for file in MODEL_FILES)


def selected(config: dict) -> bool:
    backend = config.get("race_voice_backend", "auto")
    return backend == "omnivoice" or (backend == "auto" and ready())


def prepare(log) -> bool:
    """Install the pinned runtime and model once. Expose them to Auto only after the GPU probe passes."""
    from .install import runtime_of, uv_env

    if INSTALL_DIR is None or runtime_of(INSTALL_DIR) != "cuda":
        return False
    if ready():
        log("OmniVoice для Windows CUDA уже подготовлен.")
        return True
    if shutil.disk_usage(INSTALL_DIR).free < 11 * 1024**3:
        log("Для установки OmniVoice нужно 11 ГиБ свободного места после установки F5. Доступны F5 и голоса Windows.")
        return False

    env = uv_env(INSTALL_DIR, PROJECT)
    env["UV_PROJECT_ENVIRONMENT"] = str(environment())
    log("устанавливаю OmniVoice для Windows CUDA …")
    subprocess.run([str(INSTALL_DIR / "uv.exe"), "sync", "--frozen", "--no-dev", "--project", str(PROJECT),
                    "--python", "3.12"], env=env, check=True)
    subprocess.run([str(interpreter()), "-c",
                    "import torch, torchaudio; torch.ones(1, device='cuda'); print(torch.cuda.get_device_name())"],
                   check=True, timeout=120)

    from huggingface_hub import snapshot_download

    log("скачиваю модель OmniVoice …")
    snapshot_download(MODEL_REPO, revision=MODEL_REVISION, allow_patterns=MODEL_FILES, local_dir=MODEL_DIR)
    missing = [name for name in MODEL_FILES if not (MODEL_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(f"не скачаны файлы OmniVoice: {', '.join(missing)}")
    (environment() / ".ready").touch()
    log("OmniVoice для Windows CUDA подготовлен.")
    return True
