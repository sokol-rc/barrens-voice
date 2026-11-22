"""The release manifest's ed25519 signature (docs/update-strategy.md).

    python tools/sign.py keygen [KEY_FILE]   a new key pair: the private key to KEY_FILE (default
                                             ~/.config/barrens-voice/release-signing.key, owner-only), the
                                             public one printed - it goes into bv/update.py PUBLIC_KEY
    python tools/sign.py sign [FILE]         FILE (default dist/manifest.json) -> FILE.sig (`mise run sign`;
                                             dist/dictionary.json for the dictionary online); the key from
                                             BV_SIGNING_KEY (base64, CI's secret) or KEY_FILE

The launcher's updater installs nothing whose manifest does not verify with PUBLIC_KEY, and nothing
whose archive does not match the manifest's SHA-256.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = Path.home() / ".config" / "barrens-voice" / "release-signing.key"
RAW = serialization.Encoding.Raw


def keygen(path: Path) -> None:
    if path.exists():
        sys.exit(f"{path} already exists: a new key would orphan every release signed with it")
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(seed).decode() + "\n")
    public = key.public_key().public_bytes(RAW, serialization.PublicFormat.Raw)
    print(f"private key: {path} (keep it secret; for CI: the secret BV_SIGNING_KEY = its content)")
    print(f"PUBLIC_KEY = \"{base64.b64encode(public).decode()}\"")


def load_key() -> Ed25519PrivateKey:
    text = os.environ.get("BV_SIGNING_KEY") or (KEY_FILE.read_text() if KEY_FILE.exists() else "")
    if not text.strip():
        sys.exit(f"no signing key: BV_SIGNING_KEY is empty and {KEY_FILE} is missing")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(text.strip()))


def sign(manifest: Path) -> Path:
    key = load_key()
    public = os.environ.get("BV_UPDATE_PUBLIC_KEY")
    if public and key.public_key().public_bytes(RAW, serialization.PublicFormat.Raw) != base64.b64decode(public, validate=True):
        sys.exit("BV_SIGNING_KEY is not the private key for BV_UPDATE_PUBLIC_KEY")
    signature = key.sign(manifest.read_bytes())
    out = manifest.with_name(manifest.name + ".sig")
    out.write_text(base64.b64encode(signature).decode() + "\n", encoding="ascii")
    print(f"{out.relative_to(ROOT)}: signed")
    return out


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "keygen":
        keygen(Path(sys.argv[2]) if len(sys.argv) > 2 else KEY_FILE)
    elif command == "sign":
        sign(Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / "dist" / "manifest.json")
    else:
        sys.exit(__doc__)
