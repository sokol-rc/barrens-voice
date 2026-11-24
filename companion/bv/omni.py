"""Use the managed Windows CUDA OmniVoice runtime or an explicitly configured WSL environment."""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path, PureWindowsPath

from . import omni_runtime
from .assign import Assigner
from .audio import PlaybackWindow, check_output, seconds
from .f5engine import GAP, SENTENCE_END, F5Speaker, make_bank, wav_bytes
from .omni_worker import profile_steps
from .text import numbers_to_words

LIVE_MAX_AGE = 8.0
FIRST_AUDIO_BUDGET = 3.0
LONG_AUDIO_BUDGET = 5.0


class WorkerLost(RuntimeError):
    """The current worker cannot accept another request."""


def balanced_parts(text):
    """Keep short lines whole. Split long lines only across complete sentences."""
    if len(text) >= 75:
        for boundary in SENTENCE_END.finditer(text):
            first = text[:boundary.start()]
            if len(first.split()) >= 3 and not re.search(r"\b(?:[а-яёa-z]|ул|им|рис|стр|см|гл|руб|гг|т\.\s*[деп])\.$",
                                                          first, re.IGNORECASE):
                return first, text[boundary.end():]
    return (text,)


def linux_path(value: str | Path) -> str:
    path = PureWindowsPath(value)
    if len(path.drive) != 2 or not path.is_absolute():
        raise ValueError(f"OmniVoice needs a local Windows drive path: {path}")
    return "/mnt/" + path.drive[0].lower() + "/" + "/".join(path.parts[1:])


def plain_text(text: str) -> str:
    import re
    return re.sub(r"\+(?=[аеёиоуыэюяАЕЁИОУЫЭЮЯ])", "", numbers_to_words(text))


class Worker:
    def __init__(self, config, logdir):
        python = config.get("omnivoice_wsl_python")
        model = config.get("omnivoice_wsl_model")
        script = Path(__file__).with_name("omni_worker.py")
        if (python or model) and not (omni_runtime.native_only() and omni_runtime.ready()):
            if not (python and model):
                raise ValueError("Для OmniVoice в WSL укажи пути к Python и модели")
            distribution = config.get("omnivoice_wsl_distro", "Ubuntu")
            command = ["wsl.exe", "-d", distribution, "--exec", python, "-u", linux_path(script), model]
            self.voice_path = linux_path
            self.provider = "OmniVoice / CUDA (WSL)"
            log_name = "omnivoice-wsl.log"
        else:
            if not omni_runtime.ready():
                raise RuntimeError("OmniVoice для Windows CUDA не установлен")
            command = [str(omni_runtime.interpreter()), "-u", str(script), str(omni_runtime.MODEL_DIR)]
            self.voice_path = str
            self.provider = "OmniVoice / CUDA"
            log_name = "omnivoice.log"
        logdir.mkdir(parents=True, exist_ok=True)
        self.messages = queue.Queue()
        with (logdir / log_name).open("ab") as log:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        threading.Thread(target=self._read, daemon=True, name="omni-wsl-read").start()

    def _read(self):
        try:
            with self.process.stdout as pipe:
                for raw in pipe:
                    message = json.loads(raw)
                    size = message.get("bytes", 0)
                    if not isinstance(size, int) or not 0 <= size <= 16_000_000 or size % 4:
                        raise ValueError("Некорректный размер звука OmniVoice")
                    data = pipe.read(size)
                    if len(data) != size:
                        raise OSError("OmniVoice остановился во время передачи аудио")
                    self.messages.put((message, data))
        except (OSError, ValueError) as error:
            self.messages.put(({"lost": True, "error": str(error)}, b""))
        finally:
            self.messages.put(({"lost": True, "error": "Процесс OmniVoice остановился. Подробности в журнале OmniVoice"}, b""))

    def receive(self, timeout: float, *, close_on_timeout=True):
        try:
            message, data = self.messages.get(timeout=timeout)
        except queue.Empty as error:
            if close_on_timeout:
                self.close()
                raise WorkerLost(f"OmniVoice не ответил за {timeout:.0f} с") from error
            raise TimeoutError(f"OmniVoice не ответил за {timeout:.0f} с") from error
        if message.get("lost"):
            raise WorkerLost(message["error"])
        if message.get("error"):
            raise RuntimeError(message["error"])
        return message, data

    def synth(self, slot, text, rate, profile="quality", *, budget=None, on_slow=None, timeout=240):
        import numpy as np

        req = dict(text=text, path=self.voice_path(slot.path), reference=slot.text,
                   speed=max(.5, min(2., rate / 100)), profile=profile)
        try:
            self.process.stdin.write(json.dumps(req, ensure_ascii=True).encode("ascii") + b"\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise WorkerLost("Не удалось передать реплику OmniVoice") from error
        if budget is None:
            message, data = self.receive(timeout)
        else:
            try:
                message, data = self.receive(max(0.0, budget), close_on_timeout=False)
            except TimeoutError:
                on_slow()
                message, data = self.receive(timeout)
        samples = np.frombuffer(data, dtype="<f4")
        if not len(samples) or not np.isfinite(samples).all() or np.max(np.abs(samples)) < .001:
            raise ValueError("OmniVoice вернул пустую или повреждённую запись")
        return samples, message["rate"], message["seconds"]

    def close(self):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()


class OmniSpeaker(F5Speaker):
    engine_name = "OmniVoice"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker = None
        self.stopped = threading.Event()
        self.slow = threading.Event()
        self.on_fallback = None
        self.on_failed = None
        self.profile = self.config.get("omnivoice_profile", "quality")

    def current_profile(self):
        # The launcher replaces config.json atomically. Keep the last choice if an external edit is incomplete.
        try:
            saved = json.loads((self.home / "config.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return self.profile
        if isinstance(saved, dict):
            self.profile = saved.get("omnivoice_profile", self.profile)
        return self.profile

    def _load(self):
        slots = make_bank(self.config, self.home, self.log)
        first = next((slot for slot in slots if slot.race), None)
        if first is None:
            raise RuntimeError("в клиенте игры нет русской озвучки для банка голосов")
        self.assigner = Assigner(slots, self.home / "assignments.json", self.config["players"])
        self.log("загружаю OmniVoice на CUDA …")
        worker = self.worker = Worker(self.config, self.home / "logs")
        try:
            if self.stopped.is_set():
                raise RuntimeError("Загрузка OmniVoice отменена")
            header, _ = worker.receive(180)
            self.card = header["gpu"]
            self.provider = worker.provider
            self.device = self.provider
            from . import state

            state.report(voices="warming")
            self.log("проверяю синтез на CUDA и звуковое устройство …")
            samples, rate, _ = worker.synth(first, "Проверяю озвучку чата.", 100, self.current_profile(),
                                             timeout=120)
            try:
                check_output(wav_bytes(samples, rate, 1.0))
            except Exception as error:
                raise RuntimeError(f"Не удалось открыть аудиовыход Windows: {error}") from error
            if self.stopped.is_set():
                raise RuntimeError("Загрузка OmniVoice отменена")
        except BaseException:
            worker.close()
            raise
        self.log(f"{self.provider} готов на {self.card}")

    def fastest(self):
        return False

    def _prepare_text(self, text):
        return plain_text(text)

    @staticmethod
    def live(line):
        return getattr(line, "received_at", None) is not None and line.kind != "test" and line.options.get("replay") != "1"

    def _fallback(self, line):
        if self.on_fallback is not None and not self.stopped.is_set():
            self.on_fallback(replace(line, expires_at=line.received_at + LIVE_MAX_AGE))

    def say(self, line):
        if self.slow.is_set() and self.on_fallback is not None and self.live(line) and self.current_profile() == "balanced":
            self._fallback(line)
        else:
            super().say(line)

    def _read_line(self, line, generation):
        profile = self.current_profile()
        live = profile == "balanced" and self.live(line)
        window = PlaybackWindow(line.received_at + LIVE_MAX_AGE) if live else None
        if window is not None and window.expired():
            self.log("OmniVoice: устаревшую реплику пропускаю")
            return
        slot = self.assigner.slot_for(line.sender, line.race, line.sex)
        if slot is None:
            return
        text = self._prepare_text(line.text)
        if text[-1:].isalnum():
            text += "."
        parts = balanced_parts(text) if profile == "balanced" else (text,)
        skip_audio = False
        continuation_budget = 3.0

        def skip_late_tail():
            nonlocal skip_audio
            skip_audio = True
            self.slow.set()
            self.log("OmniVoice: продолжение опоздало, пропускаю его")

        def fallback():
            nonlocal skip_audio
            skip_audio = True
            self.slow.set()
            if self.on_fallback is not None and generation == self.player.generation and not window.expired():
                self.log("OmniVoice: синтез задержался, читаю реплику голосом Windows")
                self._fallback(line)

        try:
            for index, part in enumerate(parts):
                if generation != self.player.generation or self.stopped.is_set() or (window and window.expired()):
                    return
                timing = {}
                if live and index == 0 and self.on_fallback is not None:
                    deadline = line.received_at + (LONG_AUDIO_BUDGET if len(text) >= 75 else FIRST_AUDIO_BUDGET)
                    if time.monotonic() >= deadline:
                        fallback()
                        return
                    timing = {"budget": deadline - time.monotonic(), "on_slow": fallback, "timeout": 45}
                elif live and index:
                    timing = {"budget": continuation_budget, "on_slow": skip_late_tail, "timeout": 45}
                data = self._render(slot, part, line, generation, index < len(parts) - 1, profile=profile, **timing)
                if skip_audio or data is None:
                    return
                self.player.play(data, generation, low_latency=True, **({"window": window} if window else {}))
                if live and index + 1 < len(parts):
                    continuation_budget = max(3.0, min(10.0, seconds(data) + 2.0))
        except WorkerLost as error:
            if live and not skip_audio and index == 0:
                fallback()
            if self.on_failed is not None:
                self.on_failed(str(error))
            raise
        finally:
            self.slow.clear()

    def _render(self, slot, text, line, generation, trailing_gap, *, profile=None, **timing):
        profile = profile or self.current_profile()
        samples, rate, elapsed = self.worker.synth(slot, text, line.rate, profile, **timing)
        if generation != self.player.generation or self.stopped.is_set():
            return None
        gain = self.config["kind_gain_db"].get(line.kind, 0)
        data = wav_bytes(samples, rate, 1.0, gain, tail=GAP) if trailing_gap else wav_bytes(samples, rate, 1.0, gain)
        if line.kind == "test" or self.config.get("debug"):
            message = "тестовый звук готов" if line.kind == "test" else "синтез"
            self.log(f"OmniVoice: {message} ({profile_steps(profile)} шагов, {elapsed:.2f} с)")
        return data

    def shutdown(self):
        self.stopped.set()
        super().shutdown()
        if self.worker is not None:
            self.worker.close()
