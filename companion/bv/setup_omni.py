"""Windows Setup entry point. Publish the new version only after preparing CUDA and the models."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

if __package__:
    from . import install
else:
    import install  # both scripts are next to each other in the installer


def prepare(root: Path, archive: Path) -> None:
    for name in ("app", "cache", "data"):
        (root / name).mkdir(parents=True, exist_ok=True)
    marker = root / install.INSTALL_MARK
    previous = marker.read_bytes() if marker.is_file() else None
    marker.write_text(json.dumps({"app": "Barrens Voice", "runtime": "cuda"}) + "\n", encoding="utf-8")
    try:
        version, folder = install.unpack(archive, root)
        install.build(root, folder, "cuda", root / "uv.exe")
        command = [str(folder / ".venv" / "Scripts" / "python.exe"), "-m", "bv", "--prepare-omnivoice"]
        subprocess.run(command, cwd=folder / "companion", check=True)
        install.point_current(root, folder)
    except BaseException:
        if previous is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_bytes(previous)
        raise
    print(f"Barrens Voice {version} установлен в {root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install Barrens Voice with OmniVoice CUDA")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.root, args.archive)
