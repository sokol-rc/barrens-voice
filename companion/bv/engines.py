"""Which engine speaks: Windows voices (always loaded, light) or F5-TTS race voices (loaded only
while the addon asks for them, unloaded when it asks for Windows voices). Notices go to the
program's window; spoken only with "spoken_notices" (the program cannot write into the game)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import state
from .protocol import Line

NOTICE_SENDER = "Barrens Voice"
# seconds after a failed load before a line asking for the race voices loads them again (a switch to
# Windows voices and back in the addon tries at once)
RETRY_AFTER = 600.0


class Engines:
    def __init__(self, config: dict, log, home: Path, sapi, accentor, player=None, clock=time.monotonic):
        self.config, self.log, self.home, self.clock = config, log, home, clock
        self.player = player
        self.sapi, self.accentor = sapi, accentor
        self.f5 = None
        self.ready = False
        self.omni_failed = False
        self.on_ready = None    # called once F5 is loaded (all models are in the cache by then)
        self.on_models_missing = None   # called with the error when F5 failed for a model missing from the cache
        self.failed_at = None   # clock() of the last failed load: not tried again for RETRY_AFTER
        self.lock = threading.Lock()

    def notice(self, text: str) -> None:
        self.log(text)
        if self.config.get("spoken_notices"):
            self.sapi.say(Line("notice", NOTICE_SENDER, "", "3", "", "", text))

    def want(self, voice: str) -> None:
        """The addon's choice of voices: load F5 for "f5", unload it for "sapi"."""
        if voice == "f5":
            self._load()
        elif voice == "sapi":
            with self.lock:
                self.failed_at = None       # switched away: switching back tries again at once
                self.omni_failed = False
            self._unload()

    def follow(self, line: Line) -> None:
        """The addon's choice of voices as a frame carries it. A test line of one Windows voice
        (`sapi=`, the play buttons of the voices window) says voice=sapi for itself only: the race
        voices stay loaded."""
        if line.voice and not line.sapi_voice:
            self.want(line.voice)

    def _load(self) -> None:
        with self.lock:
            if self.f5 is not None:
                return
            # every frame and chat line asks for F5: a failed load is not started again for each
            if self.failed_at is not None and self.clock() - self.failed_at < RETRY_AFTER:
                return
            from .omni_runtime import selected
            if not self.omni_failed and selected(self.config):
                from .omni import OmniSpeaker as speaker_type
            else:
                from .f5engine import F5Speaker as speaker_type
            self._start_locked(speaker_type)

    def _start_locked(self, speaker_type) -> None:
        self.log("загружаю голоса рас ...")
        state.report(voices="loading")
        speaker = speaker_type(self.config, self.log, self.home, self.accentor, self.player)
        self.f5, self.ready = speaker, False
        speaker.start()
        threading.Thread(target=self._wait, args=(speaker,), daemon=True, name="f5-wait").start()

    def _wait(self, speaker) -> None:
        speaker.ready.wait()
        with self.lock:
            if self.f5 is not speaker:
                return                      # unloaded meanwhile
            from .omni_runtime import native_only
            if speaker.error is not None and getattr(speaker, "engine_name", "") == "OmniVoice" and not native_only():
                from .f5engine import F5Speaker

                self.omni_failed = True
                self.log(f"OmniVoice недоступен: {speaker.error}. Проверяю F5.")
                self._start_locked(F5Speaker)
                return
            if speaker.error is not None:
                self.f5, self.failed_at = None, self.clock()
                error, missing = speaker.error, getattr(speaker, "models_missing", False)
            else:
                self.ready, error, missing = True, None, False
        if error is not None:
            state.report(voices="error", voices_error=error)
            self.notice("Голоса рас недоступны, читаю голосами Windows.")
            self.log(f"  причина: {error}")
            if missing and self.on_models_missing:      # a video card's failure is no reason to go online
                self.on_models_missing(error)
        else:
            self.report_ready(speaker)
            self.notice("Голоса рас готовы.")
            if self.on_ready:
                self.on_ready()

    @staticmethod
    def report_ready(speaker) -> None:
        graph = getattr(speaker, "onnx", None)
        provider = getattr(speaker, "provider", "")
        if getattr(graph, "using_trt", False):
            provider = "TensorRT"
        elif provider:
            provider = provider.removesuffix("ExecutionProvider").replace("Dml", "DirectML")
        state.report(voices="ready", device=getattr(speaker, "card", ""), provider=provider,
                     f5_steps=getattr(graph, "steps", None))

    def _unload(self) -> None:
        with self.lock:
            speaker, was_ready = self.f5, self.ready
            self.f5, self.ready = None, False
        if speaker is None:
            return
        state.report(voices="off")
        speaker.shutdown()
        if was_ready:
            self.notice("Голоса рас выгружены.")
        else:
            self.log("загрузка голосов рас отменена")

    def stop(self) -> None:
        """Silence now: the line being played is cut, the queues and lines being made dropped."""
        self.sapi.clear()
        with self.lock:
            speaker = self.f5
        if speaker is not None:
            speaker.clear()
        if self.player is not None:
            self.player.stop()

    def reset_assignments(self) -> None:
        """The addon's "hand the race voices out afresh"."""
        with self.lock:
            speaker = self.f5
        if speaker is not None and speaker.assigner is not None:
            speaker.assigner.reset()
        else:
            (self.home / "assignments.json").unlink(missing_ok=True)
        self.log("голоса рас будут розданы заново")

    def quiet_for(self) -> float:
        """Seconds since the last line was heard to its end; 0 while a line is being made, waits
        or plays (in either engine)."""
        for speaker in (self.sapi, self.f5):
            if speaker is not None and (speaker.lines.qsize() or getattr(speaker, "working", False)):
                return 0.0
        return self.player.quiet_for() if self.player is not None else 1e9

    def for_line(self, line: Line):
        """The engine for a chat line: F5 when the line asks for it and it is ready (starting it if
        need be; meanwhile Windows voices), else SAPI."""
        voice = line.voice or self.config["engine"]
        if voice == "f5":
            self._load()
            with self.lock:
                if self.ready and self.f5 is not None:
                    return self.f5
        return self.sapi
