"""One player for every engine: lines play strictly one after another (two engines must never talk over or cut each
other), each with a short silent tail so the sound device does not clip the last syllable. The volume the addon asks
is applied while a line plays (StreamOut), so the mouse wheel reaches the line already playing. stop() cuts the line being played and
drops everything queued or still being made (the "generation" a line was asked for in)."""
from __future__ import annotations

import io
import queue
import sys
import threading
import time
import wave
from contextlib import closing
from dataclasses import dataclass

from . import protocol

TAIL = 0.25   # seconds of silence after each line
PAUSE_MAX = 90.0   # a pause whose "resume" never came ends by itself


_device_refresh = threading.Lock()


def wav(pcm: bytes, rate: int, volume: float = 1.0, *, tail: float = TAIL) -> bytes:
    """16-bit mono PCM as a WAV file in memory, scaled by `volume` (0..2; over 1 clipped - the
    engines go through f5engine.wav_bytes, which limits softly), with the silent tail."""
    if abs(volume - 1) > 0.001:
        import array
        samples = array.array("h", pcm)
        for i, s in enumerate(samples):
            samples[i] = max(-32768, min(32767, int(s * volume)))
        pcm = samples.tobytes()
    pcm += b"\0\0" * int(rate * tail)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


def _replayed(line) -> bool:
    return getattr(line, "options", {}).get("replay") == "1"


def drain(q: queue.Queue) -> list:
    """Everything waiting in `q`, taken out of it."""
    taken = []
    while True:
        try:
            taken.append(q.get_nowait())
        except queue.Empty:
            return taken


def queue_line(lines: queue.Queue, line, limit: int, log) -> None:
    """A chat line into an engine's queue. With `limit` live lines waiting, the oldest of them is
    dropped - the reading keeps up with the chat - but never a line sent again by /bv last (asked
    for; the addon marks it replay=1): a replay waits whole and does not count toward the limit, so
    the chat that comes meanwhile keeps its own `limit` lines behind it."""
    if lines.qsize() >= limit:
        waiting = drain(lines)
        live = [i for i, x in enumerate(waiting) if x is not None and not _replayed(x)]
        dropped = set()
        while len(live) >= limit and not _replayed(line):
            i = live.pop(0)
            dropped.add(i)
            old = waiting[i]
            log(f"очередь полна ({limit}): не прочитано: {protocol.shown_sender(old.sender)}: {old.text[:80]}")
        for i, x in enumerate(waiting):
            if i not in dropped:
                lines.put(x)
    lines.put(line)


KNEE = 0.8   # peaks above this are bent smoothly toward full scale instead of clipped


def limit(samples):
    """Peaks over KNEE eased toward 1.0 (tanh) instead of cut off: a line played louder than 100 %
    (the addon's volume goes to 200 %) stays clean; below KNEE nothing changes."""
    import numpy as np

    a = np.abs(samples)
    over = a > KNEE
    if not over.any():
        return samples
    out = np.array(samples, dtype="float32")
    out[over] = np.sign(out[over]) * (KNEE + (1 - KNEE) * np.tanh((a[over] - KNEE) / (1 - KNEE)))
    return out


def samples_of(data: bytes):
    """A line's WAV (16-bit mono, as wav() makes it) as float32 samples -1..1, and its rate."""
    import numpy as np
    with wave.open(io.BytesIO(data)) as w:
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768, rate


class StreamBroken(OSError):
    """The stream failed twice for one line, the list of devices read again between: the device is gone for good."""


@dataclass
class PlaybackWindow:
    expires_at: float
    started: bool = False

    def expired(self) -> bool:
        return not self.started and time.monotonic() >= self.expires_at


def check_output(data: bytes) -> None:
    """Send a short, muted piece of the actual warm-up WAV through the Windows output path."""
    samples, rate = samples_of(data)
    probe = wav(bytes(2 * min(len(samples), rate // 4)), rate, tail=0)
    StreamOut().play(probe, lambda: 0, threading.Event(), seconds(probe), low_latency=True)


def preferred_output() -> int | None:
    """Current Windows mapper target. PortAudio's WASAPI default is a startup snapshot."""
    if sys.platform == "win32":
        import ctypes
        device, flags = ctypes.c_uint32(), ctypes.c_uint32()
        try:
            # WAVE_MAPPER, DRVM_MAPPER_PREFERRED_GET (mmddk.h).
            status = ctypes.windll.winmm.waveOutMessage(ctypes.c_void_p(0xffffffff), 0x2015,
                                                       ctypes.byref(device), ctypes.byref(flags))
            if status == 0 and device.value != 0xffffffff:
                return device.value
        except (AttributeError, OSError):
            pass
    return None


def wasapi_output(sd):
    return next(h["default_output_device"] for h in sd.query_hostapis()
                if h["name"] == "Windows WASAPI" and h["default_output_device"] >= 0)


def prime_output(rate: int) -> None:
    """Briefly start shared WASAPI before GPU synthesis resumes after silence in foreground WoW."""
    if not _device_refresh.acquire(blocking=False):
        return
    try:
        import sounddevice as sd

        def silence(out, frames, times, status):
            out[:] = bytes(len(out))

        stream = sd.RawOutputStream(device=wasapi_output(sd), samplerate=rate, channels=1, dtype="float32",
                                    blocksize=0, callback=silence,
                                    extra_settings=sd.WasapiSettings(auto_convert=True))
        with closing(stream):
            stream.start()
            stream.stop()
    except Exception:  # the shared output is optional for synthesis
        pass
    finally:
        _device_refresh.release()


class StreamOut:
    """Plays a line as a stream (PortAudio, through sounddevice): the volume asked is read for every block, so the mouse
    wheel reaches the line playing at once, louder and quieter, 0..2 of it (over 1 the peaks eased, `limit`). Nothing
    is set in Windows: the line itself is multiplied.

    Ordinary playback uses Windows' sound mapper (MME's first output), which follows the default output.
    Low-latency playback first tries shared WASAPI while the preferred output matches the device snapshot.
    PortAudio reads its list of devices once, so when a stream fails or stalls (the device gone) the list is read
    again and the line goes on from where it stopped; only a second failure is the caller's (Player: said, the next
    line tries again)."""

    BLOCK = 1024    # frames a block: ~46 ms at 22 kHz, a volume change heard within it
    STALL = 2.0     # seconds without a block: the device is gone

    def __init__(self, sd=None):
        if sd is None:
            import sounddevice as sd
        self.sd = sd
        self.device = self.mapper()
        self.preferred = preferred_output()

    def mapper(self):
        """Windows' sound mapper's index in PortAudio's list (None: the default device - not Windows, no MME)."""
        try:
            mme = next(i for i, h in enumerate(self.sd.query_hostapis()) if h["name"] == "MME")
            return next(i for i in self.sd.query_hostapis(mme)["devices"]
                        if self.sd.query_devices(i)["max_output_channels"] > 0)
        except Exception:  # noqa: BLE001 - no MME (not Windows), a list that does not read: the default device
            return None

    def reopen(self) -> None:
        """PortAudio's list of devices read again (a device plugged in or out since it started). A start that fails is
        StreamBroken (said, the next line tries again), and a stop of a PortAudio not started (the start before
        failed) is no reason not to start it again."""
        terminate, initialize = getattr(self.sd, "_terminate", None), getattr(self.sd, "_initialize", None)
        if terminate and initialize:
            try:
                terminate()
            except Exception:  # noqa: BLE001 - not started (an earlier initialize failed): start it afresh
                pass
            try:
                initialize()
            except Exception as e:
                raise StreamBroken(f"PortAudio не запустился: {e}") from e
        self.device = self.mapper()
        self.preferred = preferred_output()

    def _open_stream(self, low_latency, **kwargs):
        sd = self.sd
        if low_latency:
            kwargs["latency"] = "low"
            if self.preferred is not None and self.preferred == preferred_output():
                try:
                    return sd.OutputStream(device=wasapi_output(sd), blocksize=0,
                                           extra_settings=sd.WasapiSettings(auto_convert=True), **kwargs)
                except Exception:  # shared stream unavailable at this format or endpoint
                    pass
        return sd.OutputStream(device=self.device, blocksize=512 if low_latency else self.BLOCK, **kwargs)

    def play(self, data: bytes, level, stopped: threading.Event, length: float, *, low_latency=False) -> None:
        """Until the line ends or `stopped` is set (a stop or a pause cuts it). `length`: the outputs' interface (a fake
        waits it out); the stream knows its own end."""
        samples, rate = samples_of(data)
        state = {"pos": 0}
        for attempt in (1, 2):
            try:
                if self._stream(samples, rate, state, level, stopped, low_latency and attempt == 1):
                    return
                raise OSError("звук остановился: устройство пропало?")
            except Exception as e:
                if stopped.is_set():
                    return
                if attempt == 2:
                    raise StreamBroken(str(e)) from e
                with _device_refresh:  # PortAudio must not reset an active priming stream
                    self.reopen()

    def _stream(self, samples, rate, state, level, stopped, low_latency=False) -> bool:
        """One stream from state["pos"]: True when the line ended or was stopped, False when it stalled."""
        import numpy as np
        sd = self.sd
        done = threading.Event()
        beat = {"at": time.monotonic()}

        def callback(out, frames, _time, _status):
            pos = state["pos"]
            chunk = samples[pos:pos + frames]
            state["pos"] = pos + len(chunk)
            beat["at"] = time.monotonic()
            gain, before = max(0.0, level()), state.get("gain")
            if before is None or before == gain:
                scaled = chunk * gain
            else:                                   # a turn of the wheel: a ramp across the block, no click at its edge
                scaled = chunk * np.linspace(before, gain, len(chunk), endpoint=False, dtype="float32")
            state["gain"] = gain
            out[:len(chunk), 0] = limit(scaled)
            out[len(chunk):, 0] = 0
            if len(chunk) < frames:
                raise sd.CallbackStop
        stream = self._open_stream(low_latency, samplerate=rate, channels=1, dtype="float32",
                                   callback=callback, finished_callback=done.set)
        try:
            stream.start()
            beat["at"] = time.monotonic()          # the first block comes a buffer after the start: no stall before it
            while not done.wait(0.02):
                if stopped.is_set():
                    stream.abort()
                    return True
                if time.monotonic() - beat["at"] > self.STALL:
                    stream.abort()
                    return False
            return state["pos"] >= len(samples)     # finished before the end: the device went away
        finally:
            stream.close()


class Player(threading.Thread):
    """The lines in turn, through an output (`out`: the stream, StreamOut; the tests give a fake). The lines come made at
    full loudness (the engines' own evening-out only); the volume the addon asks is applied as they play."""

    def __init__(self, max_queue: int = 8, log=None, out=None, gain: float = 1.0):
        super().__init__(daemon=True, name="player")
        self.items: queue.Queue = queue.Queue()
        self.max_queue = max_queue
        self.generation = 0
        self.stopped = threading.Event()
        self.playing_until = 0.0     # monotonic time the line being played ends (or ended)
        self.log = log or (lambda *a: None)
        self.running = threading.Event()   # cleared while paused (CatQuest speaks)
        self.running.set()
        self.paused_at = 0.0
        self.current = None          # (generation, data) being played
        self.cut_by_pause = False
        self.again = None            # the line a pause cut: played again from its start
        self.out = out
        self.silent = False          # the last line could not play: said once, until one plays again
        # the volume the addon asks now, 0..2: set as the frames come (a chat line's, a settings frame's - the wheel),
        # in their order, and read by the output for every block of the line playing; before the first frame,
        # config.json's own (the program's notices at its start)
        self.gain = gain

    def set_gain(self, gain: float) -> None:
        """The volume asked now: the line playing follows at once (the stream), the next ones are played so too."""
        self.gain = max(0.0, float(gain))

    def level(self) -> float:
        return self.gain

    def quiet_for(self) -> float:
        """Seconds since the last line ended; 0 while one plays or waits to."""
        if not self.items.empty() or not self.running.is_set() or self.again is not None:
            return 0.0
        return max(0.0, time.monotonic() - self.playing_until)

    def play(self, data: bytes, generation: int | None = None, *, low_latency=False,
             window: PlaybackWindow | None = None) -> None:
        """Queue a line; one made for an earlier generation (before a stop) is dropped. With
        max_queue lines made and waiting, it waits for room - synthesis ahead of the playing - rather
        than lose a line already made (which lines to drop is decided before they are made:
        queue_line)."""
        if generation is not None and generation != self.generation:
            return
        made_for = self.generation
        while self.items.qsize() >= self.max_queue and self.generation == made_for:
            if window is not None and window.expired():
                return
            time.sleep(0.05)
        if self.generation != made_for:          # a stop while it waited
            return
        self.items.put((made_for, data, low_latency, window))

    def pause(self) -> bool:
        """Hold the lines (CatQuest speaks): the one being played is cut, to be played again from its
        start on resume(). False if already paused."""
        if not self.running.is_set():
            return False
        self.paused_at = time.monotonic()
        self.running.clear()
        if self.current is not None:
            self.cut_by_pause = True
            self.stopped.set()
        return True

    def resume(self) -> bool:
        """Go on: the cut line first, then the queue. False if not paused."""
        if self.running.is_set():
            return False
        self.running.set()
        return True

    def _wait_running(self) -> None:
        while not self.running.wait(0.5):
            if time.monotonic() - self.paused_at > PAUSE_MAX:
                self.log(f"пауза для CatQuest дольше {PAUSE_MAX:.0f} с — продолжаю")
                self.running.set()

    def stop(self) -> None:
        """Cut the line being played and forget the queue."""
        self.generation += 1
        self.again = None
        drain(self.items)
        self.stopped.set()

    def _output(self):
        """The stream, opened at the first line (and again after it could not be: no device then, one now)."""
        if self.out is None:
            try:
                self.out = StreamOut()
            except Exception as e:  # noqa: BLE001 - no PortAudio, no device: said, the next line tries again
                self._silent(f"не открылось звуковое устройство: {e}")
        return self.out

    def _silent(self, why: str) -> None:
        if not self.silent:
            self.silent = True
            self.log(f"звук не играет: {why} — пробую снова со следующей строкой")

    def run(self) -> None:
        while True:
            item, self.again = self.again, None
            if item is None:
                item = self.items.get()
            try:
                self._play(item)
            except StreamBroken as e:
                self.current = None
                self.playing_until = time.monotonic()
                self._silent(str(e))
            except Exception as e:  # noqa: BLE001 - the only player: one line that fails must not silence the rest
                self.current = None
                self.playing_until = time.monotonic()
                self.log(f"не удалось проиграть строку: {e}")

    def _play(self, item) -> None:
        self._wait_running()
        generation, data, low_latency, window = item
        if generation != self.generation or (window is not None and window.expired()):
            return
        self.stopped.clear()
        self.cut_by_pause = False
        self.current = item
        if not self.running.is_set():         # paused just now: before it started
            self.current, self.again = None, item
            return
        out = self._output()
        if out is None:                       # no device: the line is not heard (said once)
            self.current = None
            return
        length = seconds(data)
        self.playing_until = time.monotonic() + length
        if window is not None:
            window.started = True
        if low_latency:
            out.play(data, self.level, self.stopped, length, low_latency=True)
        else:
            out.play(data, self.level, self.stopped, length)
        self.silent = False
        if self.stopped.is_set():
            self.playing_until = time.monotonic()
            if self.cut_by_pause and generation == self.generation:
                self.again = item
        self.current = None
