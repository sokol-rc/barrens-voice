"""Install the optional, pinned NVIDIA Windows runtime into the installation's cache."""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .paths import CACHE_DIR

VERSION = "10.16.1.11"
WHEEL = f"https://pypi.nvidia.com/tensorrt-cu13-libs/tensorrt_cu13_libs-{VERSION}-py3-none-win_amd64.whl"
SHA256 = "96262c3e8c64a45abd29aa3482d99480fac6845bd420b6de125699bb1ae365ff"
DLLS = ("nvinfer_10.dll", "nvinfer_plugin_10.dll", "nvonnxparser_10.dll")


def runtime(directory: Path = CACHE_DIR, log=print) -> Path:
    target = directory / "tensorrt" / VERSION
    libraries = target / "tensorrt_libs"
    if all((libraries / dll).is_file() for dll in DLLS):
        return libraries
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=target.parent) as work:
        archive = Path(work) / "runtime.whl"
        digest = hashlib.sha256()
        log(f"Скачиваю TensorRT {VERSION} (около 1,8 ГиБ)…")
        with urllib.request.urlopen(WHEEL, timeout=60) as source, archive.open("wb") as output:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != SHA256:
            raise ValueError("Контрольная сумма пакета TensorRT не совпала")
        log("Распаковываю библиотеки TensorRT…")
        staging = Path(work) / "ready"
        (staging / "tensorrt_libs").mkdir(parents=True)
        with zipfile.ZipFile(archive) as wheel:
            for name in wheel.namelist():
                prefix, _, filename = name.partition("/")
                if prefix == "tensorrt_libs" and filename and "/" not in filename:
                    with wheel.open(name) as source, (staging / prefix / filename).open("wb") as output:
                        shutil.copyfileobj(source, output)
        if not all((staging / "tensorrt_libs" / dll).is_file() for dll in DLLS):
            raise ValueError("В пакете TensorRT отсутствуют библиотеки")
        try:
            staging.rename(target)
        except OSError:
            if not all((libraries / dll).is_file() for dll in DLLS):
                raise
    return libraries


if __name__ == "__main__":
    if sys.platform == "win32" and shutil.which("nvidia-smi"):
        runtime()
