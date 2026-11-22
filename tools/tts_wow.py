"""Live input for tts_bench: click the addon's local voice test in a foreground WoW client."""
import dataclasses
import time


def button_lines(config, button, place, count):
    import win32api
    import win32con
    import win32gui
    from bv import capture, locate, protocol, window

    window.make_dpi_aware()
    cap = capture.Capturer(config, lambda *_: None)
    if place:
        geometry = locate.parse_place(place)
        if not geometry:
            raise ValueError("--wow-place expects x,y,width,height,screen_width,screen_height")
        cap.set_place(geometry)
    found = cap.find()
    if not found or not cap.wgc:
        raise RuntimeError("A visible WoW client with Windows Graphics Capture is required")
    hwnd, (left, top, width, height) = found
    x, y = button
    # One snapshot is published by the WGC thread. The main thread reads it while polling at
    # the companion's regular cadence. Only the first timestamp of each strip sequence counts.
    latest = None

    def observe(strip, ticks):
        nonlocal latest
        frame = protocol.decode(protocol.Image(*strip))
        if frame and (latest is None or frame.seq != latest[0]):
            latest = (frame.seq, ticks / 10_000_000)

    cap.wgc.observe_strip = observe

    try:
        for i in range(count):
            time.sleep(3 if i else .5)
            point = (left + x, top + y)
            if not (0 <= x < width and 0 <= y < height and win32gui.GetForegroundWindow() == hwnd
                    and win32gui.WindowFromPoint(point) == hwnd and window.find_wow_window() == found):
                raise RuntimeError("The test point must be inside the same foreground WoW client")
            previous = latest[0] if latest else None
            win32api.SetCursorPos(point)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            try:
                time.sleep(.035)
            finally:
                clicked = time.perf_counter()
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            deadline = clicked + 5
            while time.perf_counter() < deadline:
                if win32gui.GetForegroundWindow() != hwnd:
                    raise RuntimeError("WoW lost focus during capture")
                raw = cap.grab()
                frame = protocol.decode(protocol.Image(*raw)) if raw else None
                stamp = latest
                if frame and frame.seq != previous and stamp and frame.seq == stamp[0] and stamp[1] >= clicked:
                    line = protocol.parse(frame.payload)
                    if line and line.kind == "test" and line.sender.split("-")[0] == "Громмаш":
                        yield -1, 0, dataclasses.replace(line, voice="f5", volume=100), dict(
                            trial=i + 1, click_s=clicked, strip_s=stamp[1], seq=frame.seq)
                        if win32gui.GetForegroundWindow() != hwnd:
                            raise RuntimeError("WoW lost focus during synthesis")
                        break
                time.sleep(config["poll_interval"])
            else:
                raise RuntimeError("No first local test line received after click")
    finally:
        if cap.wgc:
            cap.wgc.stop()
