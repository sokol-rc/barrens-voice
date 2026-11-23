"""F5-TTS (Russian fine-tune) speaking chat lines in race voices from the game client, on the GPU.

One thread synthesises, another plays, so the next line is ready while one is being said. F5 runs
through ONNX Runtime (bv/f5onnx.py): on CUDA with an NVIDIA card, on DirectML with any other DirectX 12
card (it holds the GPU for a whole step, so the game's frames wait: DEVELOPMENT.md). Up to 0.3.0 it ran
through torch on CUDA (gigabytes to install); that code was removed in the commit "F5: torch
removed, ONNX Runtime only" - `git revert` it to bring torch back (DEVELOPMENT.md).
"""
from __future__ import annotations

import os
import re
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .assign import Assigner
from .audio import KNEE, TAIL, Player, drain, limit, prime_output, seconds, wav  # noqa: F401 - KNEE and limit: tests and tools take them here
from . import gamevoices
from .intonation import shape
from .text import Accentor, numbers_to_words

SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
GAP = 0.15   # seconds between sentence groups made separately
RECENT_PIECES = 16
MAX_PIECE_BYTES = 512 * 1024


def sentence_groups(text: str) -> list[str]:
    """`text` cut into runs of statements and single questions, so each question can get its own
    intonation: "Привет. Кто идёт? Есть хил? Собираемся у входа." is four runs."""
    groups: list[str] = []
    last = None
    for sentence in SENTENCE_END.split(text.strip()):
        if not sentence:
            continue
        question = sentence.rstrip().endswith("?")
        if groups and not question and last is False:
            groups[-1] += " " + sentence
        else:
            groups.append(sentence)
        last = question
    return groups or [text]

def first_packets(text: str) -> list[str]:
    """Speak a complete sentence first. A long statement can start at a clause boundary too.
    Keep questions whole so that shape() can find their stressed word and final rise."""
    sentences = SENTENCE_END.split(text, maxsplit=1)
    if len(sentences) > 1 or text.endswith("?") or len(text.replace("+", "")) < 75:
        return sentences
    for gap in re.finditer(r"(?<=[,;:])\s+", text):
        first, rest = text[:gap.start()], text[gap.end():]
        if len(first.split()) >= 3 and len(rest.split()) >= 4:
            return [first, rest]
    return [text]


MODEL_REPO = "Misha24-10/F5-TTS_RUSSIAN"   # the vocabulary; the model itself is our ONNX export of it
VOCAB_FILE = "F5TTS_v1_Base/vocab.txt"
# the video card, best first: CUDA (NVIDIA; onnxruntime-gpu) leaves the game alone - DirectML (any DirectX 12
# card; onnxruntime-directml) holds the card for a whole denoising step, and the game's frames wait (DEVELOPMENT.md)
PREFERRED = ("CUDAExecutionProvider", "DmlExecutionProvider")
# our ONNX export of that model (10 steps; Hugging Face, CC BY-NC 4.0 as the model), downloaded into
# the model cache at the first start unless the graphs are in models/f5-onnx or `f5_onnx_dir`
ONNX_REPO = "rgeraskin/barrens-voice-f5-onnx"


PROFILE_STEPS = {"balanced": 8, "fastest": 6}


def profile_steps(config: dict, engine: str) -> int | None:
    profile = config.get("f5_profile")
    if engine == "CUDAExecutionProvider" and isinstance(profile, str):
        return PROFILE_STEPS.get(profile)
    return None


TARGET_RMS = 0.08     # about -22 dBFS: every voice about as loud as the others
PEAK = 0.95


def tidy(samples, rate: int, gain_db: float = 0.0):
    """Speech with the silence before it cut (keeping 50 ms) and its loudness evened out: to
    TARGET_RMS, `gain_db` louder or quieter (a yell, a whisper), never over PEAK. One gain for the
    whole line, so the intonation inside it is kept."""
    import numpy as np

    samples = np.asarray(samples, dtype="float32")
    if not len(samples):
        return samples
    peak = float(np.max(np.abs(samples))) or 1.0
    loud = np.flatnonzero(np.abs(samples) > peak * 0.03)  # about -30 dB under the peak
    if len(loud):
        samples = samples[max(0, loud[0] - rate // 20):]  # keep 50 ms before the first sound
    rms = float(np.sqrt(np.mean(samples ** 2))) or 1e-6
    gain = TARGET_RMS * 10 ** (gain_db / 20) / rms
    peak = float(np.max(np.abs(samples))) or 1e-6
    gain = min(gain, PEAK / peak)
    return samples * gain


def wav_bytes(samples, rate: int, volume: float, gain_db: float = 0.0, *, tail=TAIL) -> bytes:
    import numpy as np

    samples = limit(tidy(samples, rate, gain_db) * volume)
    return wav((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes(), rate, tail=tail)


def provider(config: dict, available: list[str] | None = None) -> str:
    """ONNX Runtime's execution provider for F5 and Whisper: `f5_onnx_provider`, else the first of
    PREFERRED this ONNX Runtime has (CUDA in onnxruntime-gpu, DirectML in onnxruntime-directml)."""
    if config.get("f5_onnx_provider"):
        return config["f5_onnx_provider"]
    if available is None:
        try:
            import onnxruntime
            available = onnxruntime.get_available_providers()
        except ImportError:
            available = []
    return next((p for p in PREFERRED if p in available), "CPUExecutionProvider")


def on_card(session, engine: str) -> None:
    """A session asked for the video card runs there: ONNX Runtime falls back to the processor quietly when
    the card cannot (DirectML without DirectX 12), and the race voices would then take tens of seconds a
    line - and the voice bank, built first, many minutes for nothing. Raises then: for any provider asked for
    other than the processor itself (f5_onnx_provider "TensorrtExecutionProvider" too)."""
    used = session.get_providers()[0] if session.get_providers() else "?"
    if engine != "CPUExecutionProvider" and used != engine:
        raise RuntimeError(f"{engine} не запустился на этой видеокарте (ONNX Runtime перешёл на {used}): "
                           "нужна NVIDIA (CUDA) или видеокарта с DirectX 12")


def card_session(engine: str):
    """(SessionOptions, providers) for a graph on the video card, F5's and Whisper's alike: DirectML's
    own requirements (no memory pattern, one node at a time) and its fastest card; the processor last,
    as ONNX Runtime's fallback (`on_card` refuses a session that fell back); the processor alone for the
    processor."""
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if engine == "DmlExecutionProvider":
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return options, [(engine, {"device_id": 0, "performance_preference": "high_performance"}),
                         "CPUExecutionProvider"]
    if engine == "CPUExecutionProvider":
        return options, [engine]
    return options, [engine, "CPUExecutionProvider"]


def preload(engine: str) -> None:
    """CUDA and cuDNN from their pip packages (onnxruntime-gpu[cuda,cudnn]) before a CUDA session."""
    if engine == "CUDAExecutionProvider":
        import onnxruntime
        if hasattr(onnxruntime, "preload_dlls"):
            onnxruntime.preload_dlls()


def model_dir(config: dict, home: Path) -> Path | None:
    """The folder of F5's ONNX graphs on this computer: `f5_onnx_dir`, else models/f5-onnx next to
    the program when they are there; None means: from Hugging Face (`fetch_models`)."""
    if config.get("f5_onnx_dir"):
        return Path(config["f5_onnx_dir"])
    local = home / "models" / "f5-onnx"
    return local if (local / "F5_Metadata.onnx").exists() else None


def fetch_models(config: dict, log) -> Path:
    """F5's ONNX graphs from Hugging Face into the model cache (~760 MB once; then from the cache,
    offline like the other models). The cache is asked first, without the network: the word about the download
    only when there is one (a snapshot missing or incomplete: LocalEntryNotFoundError, IncompleteSnapshotError)."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    repo = config.get("f5_onnx_repo") or ONNX_REPO
    try:
        return Path(snapshot_download(repo, allow_patterns=["*.onnx"], local_files_only=True))
    except LocalEntryNotFoundError:
        pass
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        log(f"модели F5 (ONNX): {repo} — в первый раз скачивается ~760 МБ ...")
    return Path(snapshot_download(repo, allow_patterns=["*.onnx"]))


def models_missing(error: BaseException | None) -> bool:
    """Was it a model missing from the Hugging Face cache while offline (`offline_models`): a file not
    there, or the hub asked in offline mode - here or as the cause of another library's error. Only then
    a restart online helps; a video card's failure (TensorRT not starting, no DirectX 12), a client without
    the Russian voice-over and the like are not fixed by the network."""
    from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, (LocalEntryNotFoundError, OfflineModeIsEnabled)):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def gpu_name() -> str:
    """The video card DirectML runs on (its device 0: the adapter the desktop is on), for people to
    read; "" if Windows does not say."""
    try:
        import win32api
        return str(win32api.EnumDisplayDevices(None, 0).DeviceString).strip()
    except Exception:  # noqa: BLE001 - only a label
        return ""


def make_bank(config: dict, home: Path, log) -> list:
    """The voice bank (gamevoices): as it is, or made again from the game client with Whisper
    loaded for it and freed from the GPU afterwards."""
    out = gamevoices.bank_dir(config, home)
    checker = None
    if gamevoices.stale(config, out):
        from . import omni_runtime, state
        state.report(voices="bank")
        from .whisper_onnx import OnnxChecker
        if omni_runtime.native_only() and omni_runtime.ready():
            engine = "CUDAExecutionProvider"
            omni_runtime.preload_whisper_cuda()
        else:
            engine = provider(config)
            preload(engine)
        checker = OnnxChecker(log, engine)
        if not checker.load():
            checker = None
    try:
        return gamevoices.build(config, out, log, checker)
    finally:
        if checker is not None:
            import gc
            checker.encoder = checker.decoder = None
            gc.collect()


def prepare_voices(config: dict, home: Path, log) -> None:
    """Before the game starts: (re)make the voice bank if the game was updated since, so its files
    are read while nothing (Battle.net patching, the game) is busy with them. Quiet when the bank
    is up to date or F5 cannot run here (the F5 engine reports that when it is asked for). Never raises:
    whatever goes wrong here is tried again when the F5 engine loads, and the game starts anyway."""
    try:
        if not gamevoices.stale(config, gamevoices.bank_dir(config, home)):
            return
        from . import omni_runtime
        if omni_runtime.native_only():
            if not omni_runtime.ready():
                return
        else:
            import onnxruntime
            engine = provider(config)
            if engine not in onnxruntime.get_available_providers() or engine == "CPUExecutionProvider":
                return
        log("игра запустится, когда соберутся голоса рас")
        make_bank(config, home, log)
    except ImportError:
        return
    except Exception as e:  # noqa: BLE001 - tried again when the F5 engine loads
        log(f"голоса рас из клиента: {e}")


CUT_TAIL = 0.06   # seconds of quiet after the last sound below which the ending may not have fit


def ending_heard(expected: str, heard: str) -> bool:
    """Whether Whisper heard the line's last word in a take: the judge of a cut ending.

    Whole: the last word as written; or Whisper's run-together of the last two («в ульдаман» ->
    «вульдаман»); or the word with letters added at its end («копи» -> «копии»: a cut takes sounds
    away, it does not add them); a last word in digits (Whisper writes numbers so, the line has
    them in words). Whisper also writes a word as it hears it - «рейд» as «рейт», an unknown
    «хильте» as «хильти», three words as one: then the ending counts when the letters before it are
    not fewer (a cut word is shorter: «всем по-» for «всем пока») and the last word's length of
    letters at the end is alike."""
    from difflib import SequenceMatcher

    from .refcheck import words

    want, got = words(expected), words(heard)
    if not want:
        return True
    if not got:
        return False
    last = got[-1]
    if any(ch.isdigit() for ch in last):
        return True
    if last.startswith(want[-1]) or last == "".join(want[-2:]):
        return True
    expected, said, k = "".join(want), "".join(got), len(want[-1])
    return len(said) >= len(expected) - 1 and SequenceMatcher(None, expected[-k:], said[-k:]).ratio() >= 0.6


def line_seconds(pace: float, target_pace: float, text_bytes: int, rate: int, character: str,
                 config: dict) -> float:
    """Seconds of speech for a line (F5 takes its tempo from the length it is given).

    `pace` is the voice's own (its reference's seconds per byte of text). The game's recordings
    differ up to fourfold: a hurried error message of a character, a solemn NPC greeting. A voice
    slower than `target_pace` - the faster quarter of the bank (`voice_pace_quantile`), near the
    characters' own voices: the bank's median is a typical NPC, itself unhurried - is drawn toward
    it by `voice_pace_blend` (geometric; 0 keeps the voice's own, 1 brings it all the way), by at
    most `voice_pace_limit` times; faster voices stay as they are. Each character then gets a
    tempo of their own, within ±`character_tempo` (stable: from the name), so two characters with
    the same voice still sound like two people. The addon's speed slider (`rate`) goes on top, and
    `f5_margin`/`f5_tail` keep the last syllables from being squeezed."""
    import zlib

    target = pace
    if target_pace and pace > target_pace:
        blend = config.get("voice_pace_blend", 0.85)
        target = max(pace / config.get("voice_pace_limit", 1.8), pace ** (1 - blend) * target_pace ** blend)
    spread = config.get("character_tempo", 0.08)
    if character and spread:
        unit = (zlib.crc32(character.lower().encode("utf-8")) % 1001) / 1000      # 0..1, stable per name
        target *= 1 + spread * (2 * unit - 1)
    line = target * text_bytes / (rate / 100)
    return line * config["f5_margin"] + config["f5_tail"]


def silence_after(samples, rate: int) -> float:
    """Seconds after the last sound (frames louder than a tenth of the loudest)."""
    import numpy as np

    x = np.asarray(samples, dtype="float32")
    n = int(rate * 0.01)
    k = len(x) // n
    if k == 0:
        return 0.0
    rms = np.sqrt(np.mean(x[:k * n].reshape(k, n) ** 2, axis=1))
    loud = np.flatnonzero(rms > rms.max() * 0.1)
    return (k - 1 - loud[-1]) * 0.01 if len(loud) else 0.0


class F5Speaker:
    engine_name = "F5-TTS"

    def __init__(self, config: dict, log, home: Path, accentor: Accentor | None = None,
                 player: Player | None = None):
        self.config, self.log, self.home = config, log, home
        self.player = player
        self.lines: queue.Queue = queue.Queue()
        self.working = False         # a line taken from the queue and not yet handed to the player
        self.ready = threading.Event()
        self.error: str | None = None
        self.models_missing = False   # the error was a model not in the cache while offline (models_missing)
        self.accentor = accentor or Accentor(log, config.get("ruaccent", True))
        self.assigner: Assigner | None = None
        self.onnx = None             # F5Onnx, once loaded
        self.checker = None          # Whisper (whisper_onnx.OnnxChecker), the judge of cut endings
        self.device = self.card = self.provider = ""
        self.ref_text: dict[str, str] = {}
        self.target_pace = 0.0
        self.recent_pieces = OrderedDict()

    # --- setup (on the synth thread) --------------------------------------------------------

    def _load(self) -> None:
        self._load_onnx()
        import soundfile as sf
        slots = self.assigner.slots
        first = next(s for s in slots if s.race)     # _load_onnx refused a bank without race voices
        self.ref_text = {s.id: self.accentor.accent(s.text) for s in slots}
        self.ref_seconds = {s.id: sf.info(s.path).duration for s in slots}
        paces = sorted(self.pace(s) for s in slots if s.shift == 0 and s.speech)
        quantile = self.config.get("voice_pace_quantile", 0.25)
        self.target_pace = paces[min(len(paces) - 1, int(len(paces) * quantile))] if paces else 0.0
        self._synth(first, "Проверка.", 100)   # warm-up: the first call is slow
        if self.provider:
            self.device = f"{self.card} (ONNX Runtime, {self.onnx.steps} шагов)"
        self.log(f"F5-TTS готов на {self.device}")

    def _load_onnx(self) -> None:
        """F5 through ONNX Runtime; the models from `model_dir`."""
        import onnxruntime

        engine = provider(self.config)
        preload(engine)
        if engine not in onnxruntime.get_available_providers() or engine == "CPUExecutionProvider":
            raise RuntimeError(f"ONNX Runtime без видеокарты ({engine}): нужна NVIDIA (CUDA) или DirectX 12")
        models = model_dir(self.config, self.home) or fetch_models(self.config, self.log)
        if not (models / "F5_Metadata.onnx").exists():
            raise RuntimeError(f"нет моделей F5 в ONNX: {models} (f5_onnx_dir в config.json)")
        slots = make_bank(self.config, self.home, self.log)
        if not any(s.race for s in slots):   # before the models: a retry (RETRY_AFTER) loads nothing for nothing
            # a client without the Russian voice-over (or before it is downloaded)
            raise RuntimeError("в клиенте игры нет русской озвучки: голоса рас не из чего собрать")
        self.assigner = Assigner(slots, self.home / "assignments.json", self.config["players"])
        self.log("загружаю RUAccent ...")
        self.accentor.load()
        self.log("загружаю F5-TTS (ONNX) ...")
        from huggingface_hub import hf_hub_download

        from .f5onnx import F5Onnx
        steps = profile_steps(self.config, engine)
        trt_dir = self.config.get("f5_tensorrt_dir", "")
        if steps and self.config.get("f5_tensorrt") and not trt_dir:
            from . import state, trt_runtime
            state.report(voices="tensorrt")
            try:
                trt_dir = str(trt_runtime.runtime(log=self.log))
            except Exception as e:
                self.log(f"TensorRT не подготовлен, запускаю CUDA: {e}")
            finally:
                state.report(voices="loading")
        self.onnx = F5Onnx(models, Path(hf_hub_download(MODEL_REPO, VOCAB_FILE)), engine, self.log,
                           steps=steps, cache=self.home / "tmp" / "f5-schedules", trt_dir=trt_dir)
        self.onnx.load()
        if self.config.get("f5_end_check") == "whisper" and not self.fastest():
            from .whisper_onnx import OnnxChecker
            checker = OnnxChecker(self.log, engine)
            self.checker = checker if checker.load() else None
        self.ref_audio: dict[str, object] = {}
        self.card = (gpu_name() if engine in ("DmlExecutionProvider", "CUDAExecutionProvider") else "") or engine
        self.provider = engine

    def pace(self, slot) -> float:
        """The voice's seconds of speech per byte of its reference's text as F5 gets it - stressed, as
        the lines are counted (`duration`): the bank's target pace and each voice's own in one unit."""
        return (slot.speech or self.ref_seconds[slot.id]) / max(1, len(self.ref_text[slot.id].encode("utf-8")))

    def duration(self, slot, text: str, rate: int, character: str = "") -> float:
        """Total seconds (reference + line) to give F5 - which fits the speech into exactly that
        length, so this sets the voice's tempo: see `line_seconds`."""
        ref = self.ref_seconds[slot.id]
        return ref + line_seconds(self.pace(slot), self.target_pace, len(text.encode("utf-8")), rate, character,
                                  self.config)

    def fastest(self) -> bool:
        return self.config.get("f5_profile") == "fastest" and self.onnx.accelerated

    def _synth(self, slot, text: str, rate: int, character: str = ""):
        """The line in `slot`'s voice. F5 fits the speech into the length it is given, and sometimes
        the last syllable does not fit. A take whose speech runs to its very end (`silence_after`
        under CUT_TAIL: one line in eight, most of them whole) is made once more with a quarter
        more length: always (`f5_end_check` "silence", the default), or only when Whisper does not
        hear the last word ("whisper": a second saved on such lines for 1.5 GB of video memory)."""
        ref_file, ref_text, ref = slot.path, self.ref_text[slot.id], self.ref_seconds[slot.id]
        total = self.duration(slot, text, rate, character)
        # Duration already includes the character's tempo and the addon's rate. Keep the raw
        # voice so question shaping, channel gain and the current playback volume still apply.
        cache_key = None
        if character and self.fastest():
            cache_key = (slot.id, text, total, self.onnx.using_trt)
            if cache_key in self.recent_pieces:
                self.recent_pieces.move_to_end(cache_key)
                return self.recent_pieces[cache_key]
        if ref_file not in self.ref_audio:
            import soundfile as sf
            from .whisper_onnx import resample
            data, sr_in = sf.read(ref_file, dtype="float32", always_2d=True)
            self.ref_audio[ref_file] = resample(data.mean(axis=1), sr_in, self.onnx.in_rate)
        if (cache_key is not None and self.config.get("f5_prime_output", True)
                and self.player is not None and self.player.quiet_for() >= 5.0):
            prime_output(self.onnx.out_rate)
        for attempt in range(2):
            wav, sr = self.onnx.synth(self.ref_audio[ref_file], ref_text, text, total)
            if (attempt or self.fastest() or self.config.get("f5_end_check") == "off"
                    or silence_after(wav, sr) >= CUT_TAIL or self._whole(wav, sr, text)):
                break
            total = ref + (total - ref) * 1.25
        if (cache_key is not None and self.fastest() and self.onnx.using_trt == cache_key[-1]
                and wav.nbytes <= MAX_PIECE_BYTES):
            self.recent_pieces[cache_key] = (wav, sr)
            if len(self.recent_pieces) > RECENT_PIECES:
                self.recent_pieces.popitem(last=False)
        return wav, sr

    def _whole(self, wav, sr: int, text: str) -> bool:
        """Whether Whisper hears the take's last word (no Whisper: assumed cut, made again)."""
        if self.checker is None:
            return False
        try:
            heard = self.checker.transcribe(wav, sr)
        except Exception as e:  # noqa: BLE001 - a judge that fails must not lose the line
            self.log(f"    Whisper: {e}")
            return False
        whole = ending_heard(text, heard)
        if self.config.get("log_details"):
            self.log(f"    конец {'цел' if whole else 'обрезан, ещё раз длиннее'} (Whisper: «{heard.strip()}»)")
        return whole

    # --- threads ----------------------------------------------------------------------------

    def start(self) -> None:
        if self.player is None:
            self.player = Player(self.config["max_queue"])
            self.player.start()
        threading.Thread(target=self._synth_loop, daemon=True, name="f5-synth").start()

    def _prepare_text(self, text: str) -> str:
        return self.accentor.accent(numbers_to_words(text))

    def _synth_loop(self) -> None:
        try:
            self._load()
        except Exception as e:  # noqa: BLE001 - reported, the caller falls back to SAPI
            self.error = str(e) or type(e).__name__     # never "": that would read as success
            self.models_missing = models_missing(e)
            self.ready.set()
            return
        self.ready.set()
        while True:
            self.working = False
            line = self.lines.get()
            self.working = True
            if line is None:                 # shutdown: free the model and the GPU memory
                self.onnx = self.checker = None
                import gc
                gc.collect()
                return
            generation = self.player.generation   # a stop while this is being made drops it
            slot = self.assigner.slot_for(line.sender, line.race, line.sex)
            if slot is None:
                continue
            # numbers first: RUAccent then stresses the number words too ("шестн+адцать"; F5 left
            # to itself put it on the first syllable in some voices)
            text = self._prepare_text(line.text)
            if text[-1:].isalnum() or text[-1:] == "+":
                text += "."
            if self.config.get("log_details") and text != line.text:
                groups = sentence_groups(text)
                self.log(f"    к синтезу: {text}" + (f"  ({len(groups)} части)" if len(groups) > 1 else ""))
            started = time.perf_counter()
            try:
                parts = first_packets(text) if self.fastest() else [text]
                duration = 0.0
                for index, part in enumerate(parts):
                    if generation != self.player.generation:
                        break
                    data = self._render(slot, part, line, generation, index < len(parts) - 1)
                    if data is not None:
                        if self.config.get("debug"):
                            duration += seconds(data)
                        if self.fastest():
                            self.player.play(data, generation, low_latency=True)
                        else:
                            self.player.play(data, generation)
                if self.config.get("debug"):
                    self.log(f"  синтез {time.perf_counter() - started:.2f} с, {duration:.1f} с звука")
            except Exception as e:  # noqa: BLE001 - one bad line must not stop the reader
                self.log(f"{self.engine_name}: {e}")

    def _render(self, slot, text, line, generation, trailing_gap):
        import numpy as np
        pieces = []
        for group in sentence_groups(text):
            piece, sr = self._synth(slot, group, line.rate, line.sender)
            if generation != self.player.generation:
                return None
            if pieces:
                pieces.append(np.zeros(int(sr * GAP), dtype=piece.dtype))
            pieces.append(shape(piece, sr, group, self.config.get("question_intonation")))
        samples = np.concatenate(pieces)
        gain = self.config["kind_gain_db"].get(line.kind, 0)
        if trailing_gap:
            return wav_bytes(samples, sr, 1.0, gain, tail=GAP)
        return wav_bytes(samples, sr, 1.0, gain)

    # --- the reader's side ------------------------------------------------------------------

    def describe(self, line) -> str:
        if self.assigner is None:
            return "-"
        slot = self.assigner.slot_for(line.sender, line.race, line.sex)
        return slot.id if slot else "-"

    def clear(self) -> None:
        drain(self.lines)

    def shutdown(self) -> None:
        """Stop after the line being made; the model is freed on the synth thread."""
        if self.assigner is not None:
            self.assigner.flush()
        drain(self.lines)
        self.lines.put(None)

    def say(self, line) -> None:
        from .audio import queue_line
        queue_line(self.lines, line, self.config["max_queue"], self.log)


def selftest(config: dict, log, home: Path) -> int:
    """Build the voice bank and synthesise a line for a few characters into home/f5test, timed."""
    import soundfile as sf
    from .protocol import Line

    speaker = F5Speaker(config, log, home)
    speaker._load()
    out = home / "f5test"
    out.mkdir(exist_ok=True)
    cast = [("Громмаш", "Orc", "2"), ("Гаррош", "Orc", "2"), ("Дрекша", "Orc", "3"), ("Джайна", "Human", "3"),
            ("Брэнн", "Dwarf", "2"), ("Тиранда", "NightElf", "3"), ("Вол'джин", "Troll", "2"), ("Сильвана", "Scourge", "3")]
    for name, race, sex in cast:
        line = Line("test", name, race, sex, "", "", "Нужен дд в Мёртвые копи, продам сумки за 5000.")
        slot = speaker.assigner.slot_for(name, race, sex)
        text = speaker.accentor.accent(numbers_to_words(line.text))
        started = time.perf_counter()
        wav, sr = speaker._synth(slot, text, 100, name)
        took = time.perf_counter() - started
        sf.write(str(out / f"{race}_{name}.wav"), wav, sr)
        log(f"{name:9s} {race:8s} -> {slot.id:22s} {took:.2f} с на {len(wav) / sr:.1f} с звука")
    import ctypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    c = Counters()
    c.cb = ctypes.sizeof(c)
    kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    log(f"память: {c.WorkingSetSize / 2**30:.2f} ГБ (пик {c.PeakWorkingSetSize / 2**30:.2f})")
    log(f"файлы: {out}")
    return 0
