"""Use the managed Windows CUDA OmniVoice runtime or an explicitly configured WSL environment."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path, PureWindowsPath

from . import omni_runtime
from .assign import Assigner
from .f5engine import F5Speaker, make_bank, wav_bytes
from .omni_worker import profile_steps
from .text import numbers_to_words


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
            self.messages.put(({"error": str(error)}, b""))
        finally:
            self.messages.put(({"error": "Процесс OmniVoice остановился. Подробности в журнале OmniVoice"}, b""))

    def receive(self, timeout: float):
        try:
            message, data = self.messages.get(timeout=timeout)
        except queue.Empty as error:
            self.close()
            raise TimeoutError(f"OmniVoice не ответил за {timeout:.0f} с") from error
        if message.get("error"):
            raise RuntimeError(message["error"])
        return message, data

    def synth(self, slot, text, rate, profile="quality"):
        import numpy as np

        req = dict(text=text, path=self.voice_path(slot.path), reference=slot.text,
                   speed=max(.5, min(2., rate / 100)), profile=profile)
        self.process.stdin.write(json.dumps(req, ensure_ascii=True).encode("ascii") + b"\n")
        self.process.stdin.flush()
        message, data = self.receive(240)
        samples = np.frombuffer(data, dtype="<f4")
        if not len(samples) or not np.isfinite(samples).all() or np.max(np.abs(samples)) < .001:
            raise ValueError("OmniVoice вернул пустую или повреждённую запись")
        return samples, message["rate"], message["seconds"]

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
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
        self.profile = self.config.get("omnivoice_profile", "quality")

    def current_profile(self):
        # The launcher replaces config.json atomically. Keep the last choice if an external edit is incomplete.
        try:
            saved = json.loads((self.home / "config.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return self.profile
        if isinstance(saved, dict):
            self.profile = saved.get("omnivoice_profile", "quality")
        return self.profile

    def _load(self):
        slots = make_bank(self.config, self.home, self.log)
        if not any(slot.race for slot in slots):
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
        except BaseException:
            worker.close()
            raise
        self.log(f"{self.provider} готов на {self.card}")

    def fastest(self):
        return False

    def _prepare_text(self, text):
        return plain_text(text)

    def _render(self, slot, text, line, generation, trailing_gap):
        profile = self.current_profile()
        samples, rate, elapsed = self.worker.synth(slot, text, line.rate, profile)
        if generation != self.player.generation or self.stopped.is_set():
            return None
        data = wav_bytes(samples, rate, 1.0, self.config["kind_gain_db"].get(line.kind, 0))
        if line.kind == "test" or self.config.get("debug"):
            message = "тестовый звук готов" if line.kind == "test" else "синтез"
            self.log(f"OmniVoice: {message} ({profile_steps(profile)} шагов, {elapsed:.2f} с)")
        return data

    def shutdown(self):
        self.stopped.set()
        super().shutdown()
        if self.worker is not None:
            self.worker.close()
