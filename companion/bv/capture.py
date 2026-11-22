"""Getting the strip's pixels off the WoW window.

Windows Graphics Capture (the `windows-capture` package, as OBS uses) reads the game's own window,
so it works while another window covers the game. When it is not available, a grab of that part of
the screen (mss) - which sees only what is on top. Neither sees a minimised game: WoW draws nothing
then.

What is kept of each frame is the strip's rectangle in the client area - the top rows by default,
or where the addon says the strip is (locate.py) - and, when asked, the whole client area, to look
for a strip that is not where it was said to be.
"""
from __future__ import annotations

import threading

from .window import find_wow_window, frame_offset


class WindowCapture:
    """Windows Graphics Capture of one window; the newest strip rectangle kept for latest(), the
    whole client area for full() when asked."""

    def __init__(self, hwnd: int, client: tuple[int, int, int, int], rect: tuple[int, int, int, int]):
        from windows_capture import WindowsCapture

        self.hwnd, self.client = hwnd, client
        self.rect = rect               # x, y, width, height of the strip in the client area
        self.lock = threading.Lock()
        self.strip: tuple[bytes, int, int] | None = None
        self.observe_strip = None  # optional live benchmark observer, on the WGC thread
        self.whole: tuple[bytes, int, int] | None = None
        self.want_whole = False
        self.closed = False
        self.offset = frame_offset(hwnd)
        self.session = WindowsCapture(cursor_capture=False, draw_border=False, minimum_update_interval=40,
                                      window_hwnd=hwnd)

        @self.session.event
        def on_frame_arrived(frame, control):
            if self.closed:
                control.stop()
                return
            buf = frame.frame_buffer                      # rows x columns x BGRA
            ox, oy = self.offset
            x, y, w, h = self.rect
            w = min(w, buf.shape[1] - ox - x)
            h = min(h, buf.shape[0] - oy - y)
            if w <= 0 or h <= 0:
                return
            rows = buf[oy + y:oy + y + h, ox + x:ox + x + w]
            whole = None
            if self.want_whole:
                cw, ch = min(self.client[2], buf.shape[1] - ox), min(self.client[3], buf.shape[0] - oy)
                whole = (buf[oy:oy + ch, ox:ox + cw].tobytes(), cw, ch)
            strip = (rows.tobytes(), w, h)
            if self.observe_strip is not None:
                self.observe_strip(strip, frame.timespan)
            with self.lock:
                self.strip = strip
                if whole is not None:
                    self.whole, self.want_whole = whole, False

        @self.session.event
        def on_closed():
            self.closed = True

        self.session.start_free_threaded()

    def latest(self):
        with self.lock:
            return self.strip

    def full(self):
        """The whole client area from a frame after the previous call (None the first time)."""
        with self.lock:
            whole, self.whole, self.want_whole = self.whole, None, True
        return whole

    def stop(self) -> None:
        self.closed = True


class Capturer:
    """The strip of the current WoW window: Windows Graphics Capture when it works, else mss."""

    def __init__(self, config: dict, log):
        self.config, self.log = config, log
        self.height = config["capture_height"]
        self.window = None            # (hwnd, client rect)
        self.place = None             # where the addon says the strip is (locate.Place); None: the top
        self.rect = (0, 0, 0, 0)      # the strip's rectangle in the client area
        self.wgc: WindowCapture | None = None
        self.wgc_failed = not config.get("window_capture", True)
        self.screen = None

    def find(self):
        found = find_wow_window()
        if found != self.window:
            if self.wgc:
                self.wgc.stop()
                self.wgc = None
            self.window = found
            self._fit()
            if found and not self.wgc_failed:
                try:
                    self.wgc = WindowCapture(found[0], found[1], self.rect)
                    self.log("захват окна WoW: Windows Graphics Capture (работает и под другими окнами)")
                except Exception as e:  # noqa: BLE001 - fall back to grabbing the screen
                    self.wgc_failed = True
                    self.log("захват окна недоступен: держи игру поверх окон")
                    self.log(f"    Windows Graphics Capture: {e}")
        return found

    def _fit(self) -> None:
        """The strip's rectangle for the current window: where the addon says, else the top rows."""
        if self.window is None:
            return
        from .locate import region
        cw, ch = self.window[1][2], self.window[1][3]
        self.use(region(self.place, cw, ch, self.height) if self.place else (0, 0, cw, min(self.height, ch)))

    def set_place(self, place) -> None:
        """The strip's place as the addon gave it (locate.Place), or None for the top edge."""
        self.place = place
        self._fit()

    def remember(self, place) -> None:
        """Keep the strip's place (locate.Place) for a restart before the game saves it: the addon's
        saved variables are written only at /reload and logout (locate.known_place)."""
        from .dictionary import game_of
        from .locate import STORE, remember
        remember(STORE, game_of(self.config), place)

    def use(self, rect: tuple[int, int, int, int]) -> None:
        """Grab this rectangle of the client area (x, y, width, height) from now on."""
        self.rect = rect
        if self.wgc is not None:
            self.wgc.rect = rect

    def grab_full(self):
        """(raw BGRA bytes, width, height) of the whole client area, or None (Windows Graphics
        Capture: from a frame after the previous call - None the first time)."""
        if self.window is None:
            return None
        if self.wgc is not None:
            return self.wgc.full()
        if self.screen is None:
            return None
        left, top, width, height = self.window[1]
        shot = self.screen.grab({"left": left, "top": top, "width": width, "height": height})
        return bytes(shot.raw), shot.width, shot.height

    def grab(self):
        """(raw BGRA bytes, width, height) of the strip, or None."""
        if self.window is None:
            return None
        if self.wgc is not None:
            if self.wgc.closed:
                self.wgc = None
                self.window = None        # found again (and a new session made) on the next find()
                return None
            return self.wgc.latest()
        if self.screen is None:
            import mss
            self.screen = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        left, top = self.window[1][:2]
        x, y, w, h = self.rect
        shot = self.screen.grab({"left": left + x, "top": top + y, "width": w, "height": h})
        return bytes(shot.raw), shot.width, shot.height
