"""Whether this version can start: run by the updater of the version installed before it, with this version's
own Python, after building it and before making it current (bv/update.py version_runs).

The contract with every launcher already installed (0.4.0 on), kept by every release: the updater runs
`<version>\\.venv\\Scripts\\python.exe -m bv.selfcheck` in `<version>\\companion` when
`<version>\\companion\\bv\\selfcheck.py` exists (else its own fixed list of imports, the one below). Exit 0: the
version starts; anything else refuses it, the last line on stderr saying why. So a release decides here what
it needs to start - a launcher, companion or updater moved elsewhere is checked where it is now - and only
this module's name and place stay fixed.
"""
from __future__ import annotations


def main() -> int:
    # what the launcher needs to start and to update again: Tk, the window's code, the companion, the updater
    # with its signature check (a release whose launcher cannot start could never update itself again)
    import tkinter

    import bv.__main__  # noqa: F401
    import bv.launcher.app  # noqa: F401
    import bv.update  # noqa: F401
    from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    tkinter.Tcl()
    # the sound: one way only since 0.4.1 (a stream, audio.StreamOut), no winsound to fall back to. Found, not imported:
    # importing it starts PortAudio
    import importlib.util
    import sys
    if sys.platform == "win32" and importlib.util.find_spec("sounddevice") is None:
        print("sounddevice не установлен: звуку не на чем играть", file=sys.stderr)
        return 1
    from bv import omni_runtime
    if omni_runtime.native_only() and not omni_runtime.ready():
        print("Для перехода на OmniVoice / CUDA запусти BarrensVoice-Setup.exe из выпуска на GitHub.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
