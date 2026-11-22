"""Build the installer (`mise run installer`, Windows): dist/BarrensVoice-Setup.exe (one name for
every version: README links the latest release's file directly).

Takes uv.exe of UV_VERSION from its GitHub release (checked against the release's own SHA-256), the
release archive from tools/release.py, and compiles installer/BarrensVoice.iss with Inno Setup 6
(ISCC.exe: on GitHub's Windows runners it is preinstalled; locally - installed, or INNO_ISCC).
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT / "tools"))
from release import UV_VERSION, UV_ZIP  # noqa: E402 - one place for the uv version (the manifest names it too)
ISCC = [os.environ.get("INNO_ISCC", ""), r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe")]


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "barrens-voice-build"}), timeout=60) as r:  # noqa: S310
        return r.read()


def uv_exe() -> Path:
    target = DIST / "uv.exe"
    data = fetch(UV_ZIP)
    expected = fetch(UV_ZIP + ".sha256").decode().split()[0]
    if hashlib.sha256(data).hexdigest() != expected:
        sys.exit("uv: the archive does not match its published SHA-256")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        target.write_bytes(z.read("uv.exe"))
    print(f"uv {UV_VERSION}: {target.stat().st_size / 1e6:.1f} MB")
    return target


CSC = [r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe", r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"]


def launcher_exe() -> Path:
    """Barrens Voice.exe, the install folder's starter with the app's icon (installer/launcher.cs), by the C#
    compiler of the .NET Framework that every Windows 10/11 has."""
    csc = next((p for p in CSC if Path(p).is_file()), None)
    if csc is None:
        sys.exit("csc.exe (.NET Framework 4) not found")
    out = DIST / "Barrens Voice.exe"
    subprocess.run([csc, "/nologo", "/target:winexe", "/optimize+", f"/win32icon:{ROOT / 'companion' / 'bv' / 'launcher' / 'icon.ico'}",
                    f"/out:{out}", "/r:System.Windows.Forms.dll", "/r:System.Core.dll", str(ROOT / "installer" / "launcher.cs")],
                   check=True)
    print(f"{out.name}: {out.stat().st_size / 1e3:.0f} kB")
    return out


def main() -> int:
    sys.path.insert(0, str(ROOT / "tools"))
    import release
    DIST.mkdir(exist_ok=True)
    launcher_exe()                       # first: the release archive carries it too, for updates
    archive = release.build()
    version = archive.stem.removeprefix("barrens-voice-")
    uv_exe()
    iscc = next((p for p in ISCC if p and Path(p).is_file()), None)
    if iscc is None:
        sys.exit("Inno Setup 6 (ISCC.exe) not found: install it or set INNO_ISCC")
    repo = os.environ.get("BV_RELEASE_REPO", "rgeraskin/barrens-voice")
    subprocess.run([iscc, f"/DAppVersion={version}", f"/DReleaseRepo={repo}",
                    str(ROOT / "installer" / "BarrensVoice.iss")], check=True)
    print(f"dist/BarrensVoice-Setup.exe ({version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
