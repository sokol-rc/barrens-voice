"""Barrens Voice companion: reads the addon's strip off the WoW window and speaks each chat line.

Run: python -m bv  (config.json next to the bv folder is created on first run)
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import protocol
from .paths import CACHE_DIR, DATA_DIR      # the data's folder, apart from the code's (bv/paths.py)

CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_CONFIG = {
    "_help": "rate -10..10, volume 0..100. exclude: voices left out (part of the name). "
             "gender_overrides: voices whose gender SAPI does not know. players: {\"Name\": \"part of a voice name\"}. "
             "stress: how each engine gets the dictionary's stress marks (plus, acute or strip). "
             "engine: f5 (race voices from the game client, on an NVIDIA or DirectX 12 card) or sapi (Windows voices). "
             "voice_shifts: semitones of the extra copies of each race voice. "
             "players: also takes race voice ids (orc-male, orc-male/standard, orc-male/standard^+1.5).",
    "engine": "f5",
    "rate": 0,
    "volume": 100,
    "max_queue": 8,
    "exclude": ["Irina Desktop", "Dmitry Online"],
    "gender_overrides": {"Aidar": "Male", "Eugene": "Male", "Baya": "Female", "Kseniya": "Female", "Xenia": "Female"},
    "players": {},
    "stress": {"rhvoice": "acute", "silero": "plus", "edge": "strip", "other": "strip"},
    "ruaccent": True,
    "restore_yo": True,
    "declension": True,
    "f5_margin": 1.25,
    "f5_tail": 0.5,
    # a take whose speech reaches its very end (about one in eight): "silence" - made again, a second
    # longer; "whisper" - made again only if Whisper does not hear the last word (1.5 GB of video memory)
    "f5_end_check": "silence",
    "f5_profile": "quality",
    "f5_prime_output": True,
    "f5_tensorrt_dir": "",
    # slow voices drawn toward the bank's faster quarter (quantile) by this share, at most this many
    # times faster; each character's own tempo within this share either way (f5engine.line_seconds)
    "voice_pace_quantile": 0.25,
    "voice_pace_blend": 0.85,
    "voice_pace_limit": 1.8,
    "character_tempo": 0.08,
    # F5 only; 0 turns a part off. See bv/intonation.py.
    "question_intonation": {"yes_no_rise_st": 7, "yes_no_end_db": 8, "wh_peak_st": 6},
    "kind_gain_db": {"yell": 3, "whisper": -4},
    "voice_shifts": [-1.5, 1.5],
    "wow_dir": "",
    "game_dirs": {},              # {"forever": "...\\_forever_"}: the folders the launcher found
    "hf_home": "",
    "launch_wow": False,          # opening Barrens Voice does not start the game unless asked («Играть»)
    "game": "forever",            # or "retail"; --retail / --forever on the command line override it
    "launch_product": "WoWF",
    "launch_product_retail": "WoW",
    "battlenet_exe": "",
    "exit_with_wow": True,
    "spoken_notices": False,
    # a character's next line within this many seconds after their previous one was heard (or while
    # it still is) goes on without their name: one speech
    "same_speaker_gap": 5.0,
    "log_details": True,
    "log_keep_days": 90,
    # updates of an installed Barrens Voice (bv/update.py): prepared and applied by themselves; "beta" -
    # pre-releases too
    "auto_update": True,
    "update_channel": "stable",
    # the game first (bv/share.py): ONNX threads few and sleeping, the companion's priority below normal
    "share_with_game": True,
    "onnx_threads": 2,
    "debug": False,
    "poll_interval": 0.05,
    "capture_height": 16,
    "window_capture": True,
}
MARKUP = re.compile(r"\|c[0-9a-fA-F]{8}|\|r|\|T.*?\|t|\|A.*?\|a")
LABELS = {"party": "группа", "raid": "рейд", "warning": "объявление", "instance": "подземелье",
          "say": "сказать", "yell": "крик", "whisper": "шёпот", "guild": "гильдия", "officer": "офицеры",
          "channels": "канал", "emote": "эмоция", "test": "тест",
          "notice": "озвучка"}


def speech_text(text: str, dictionary, inflector, declension: bool, restore_yo=lambda t: t) -> str:
    """A chat line's text (without the name) as it goes to an engine, which then says its numbers
    as words and stresses it: markup gone, disguised words unmasked ("xyйня", "п0шел", "бл*ть"),
    the dictionary's words, then its regexes (whose own patterns want plain text), each replacement
    declined after its preposition when `declension` is on; then `text.plain` and «ё» (`restore_yo`)."""
    from .morph import strip_marks
    from .text import plain
    from .unmask import unmask
    text = unmask(MARKUP.sub("", text))
    text = dictionary.apply_words(text)
    text = inflector.apply(text) if declension else strip_marks(text)
    text = dictionary.apply(text, mark=True)     # marked either way: a mark keeps a replacement from the next regexes
    text = inflector.apply(text) if declension else strip_marks(text)
    return restore_yo(plain(text))


LOG_DIR = DATA_DIR / "logs"
_log_file = None
_log_day = None


_log_lock = threading.Lock()


def _write_log_file(line: str, now: datetime) -> None:
    """One line into logs/bv-YYYY-MM-DD.log (appended: one file a day, kept for later)."""
    global _log_file, _log_day
    with _log_lock:
        try:
            day = f"{now:%Y-%m-%d}"
            if day != _log_day:
                if _log_file is not None:
                    _log_file.close()
                LOG_DIR.mkdir(exist_ok=True)
                _log_file = open(LOG_DIR / f"bv-{day}.log", "a", encoding="utf-8", buffering=1)
                _log_day = day
            _log_file.write(line + "\n")
        except OSError:
            pass


def log(message: str, level: str | None = None) -> None:
    """To the console (the launcher reads it) and to logs/bv-YYYY-MM-DD.log, with its level (bv/logs.py)."""
    from .logs import level_of
    now = datetime.now()
    line = f"{now:%H:%M:%S} {level or level_of(message):<5} {message}"
    print(line, flush=True)
    _write_log_file(line, now)


class StderrToLog:
    """What the libraries write to stderr (warnings of the libraries, errors outside the
    program's own logging) goes to the console as before and to the log file too, a line at a time
    with the time. A progress bar redraws its line with "\\r": only the finished line is kept."""

    def __init__(self, console):
        self.console, self.pending = console, ""

    def write(self, text: str) -> int:
        launched = bool(os.environ.get(LAUNCHED))      # under the launcher: lines in the log's form only
        if not launched:
            try:
                self.console.write(text)
            except (OSError, ValueError):
                pass
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            line = line.split("\r")[-1].rstrip()
            if line:
                if launched:
                    log(f"[stderr] {line}", "DEBUG")
                else:
                    now = datetime.now()
                    _write_log_file(f"{now:%H:%M:%S} DEBUG [stderr] {line}", now)
        if "\r" in self.pending:                       # a bar redrawing itself: only its last state counts
            self.pending = self.pending.rsplit("\r", 1)[-1]
        return len(text)

    def flush(self) -> None:
        try:
            self.console.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):                        # isatty, encoding, fileno... as the console has them
        return getattr(self.console, name)


def prune_logs(keep_days: int) -> None:
    """Delete daily logs older than `keep_days` (0: keep them all)."""
    if keep_days <= 0 or not LOG_DIR.is_dir():
        return
    cutoff = datetime.now().timestamp() - keep_days * 86400
    for path in LOG_DIR.glob("bv-*.log"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


class ConfigError(Exception):
    """config.json cannot be read: the companion stops and says why, in words for the player."""


# the least a number setting may be: a queue of 0 lines, a poll every 0 s would break the reading; a pace
# limit of 0 divides by zero (and under 1 would slow voices down), a quantile under 0 picks no voice of the
# bank, a thread count under 1 is ONNX Runtime's "all of them" or an error
LEAST = {"max_queue": 1, "poll_interval": 0.01, "capture_height": 1, "voice_pace_limit": 1.0,
         "voice_pace_quantile": 0.0, "onnx_threads": 1}


def settle_numbers(config: dict, log) -> None:
    """Settings as the companion can use them. A number setting typed as text ("8") is read as the number,
    one that is no finite number (text, null, NaN, Infinity) gets its default, none goes under its LEAST;
    a setting of a list or of settings ({...}, [...]) that is something else (null) gets its default.
    Each such change is logged."""
    for key, default in DEFAULT_CONFIG.items():
        value = config.get(key)
        if isinstance(default, (dict, list)):
            if not isinstance(value, type(default)):
                log(f"не удалось прочитать «{key}» в config.json ({value!r}): "
                    f"беру {json.dumps(default, ensure_ascii=False)}")
                config[key] = default
            continue
        if not isinstance(default, (int, float)) or isinstance(default, bool):
            continue
        try:
            if isinstance(value, bool):
                raise TypeError(key)
            number = float(value)                       # "8" as 8; OverflowError for an int too big for a float
            if not math.isfinite(number):
                raise ValueError(key)
            value = type(default)(number)
        except (TypeError, ValueError, OverflowError):
            log(f"не удалось прочитать «{key}» в config.json ({value!r}): беру {default}")
            value = default
        if key in LEAST and value < LEAST[key]:
            log(f"не удалось взять «{key}» = {value} из config.json: беру {LEAST[key]}")
            value = LEAST[key]
        config[key] = value


def _write_config(path: Path, config: dict, log=log) -> bool:
    """Through a temporary file, as the launcher writes it: neither ever reads half a file. A file that can be
    read but not replaced (read-only, held by another program for a moment) is no reason not to start: the
    file stays as it was, no .tmp beside it, a line in the log, and the settings go on as read (what would
    have been written in - new settings, this version's _help - is only the defaults). Whether it was written."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log(f"не удалось записать config.json ({e.strerror or e}) — файл остался как был, озвучка работает дальше")
        return False


def read_config(path: Path) -> dict:
    """The settings in config.json. Raises ConfigError when the file is not JSON of settings, OSError when it
    cannot be read."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8-sig"))   # -sig: Notepad's BOM is no error
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json не читается: ошибка в строке {e.lineno}, столбец {e.colno} ({e.msg})") from e
    except ValueError as e:                             # not UTF-8
        raise ConfigError(f"config.json не читается: {e}") from e
    if not isinstance(saved, dict):
        raise ConfigError("config.json не читается: в нём должны быть настройки в фигурных скобках { }")
    return saved


def load_config(path: Path | None = None, log=log) -> dict:
    """DEFAULT_CONFIG with config.json over it; settings new to the file written into it (a write that fails
    is logged, not raised). Raises ConfigError when the file is not JSON of settings (the launcher shows why)."""
    path = path or CONFIG_PATH
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        _write_config(path, config, log)
        return config
    saved = read_config(path)
    config.update(saved)
    config["_help"] = DEFAULT_CONFIG["_help"]           # this version's text, not the one the file was made with
    settle_numbers(config, log)
    if any(key not in saved for key in DEFAULT_CONFIG) or saved["_help"] != config["_help"]:
        _write_config(path, config, log)                # new settings or a new _help: written in
    return config


class GameWatch:
    """The voice-over ends with the game it saw: the race voices leave video memory, whatever the launcher
    does after (closes, or waits for the next game in the tray). Looked at every 2 s; gone for 10 s (a client
    restarting itself, an update) counts as closed. A game never seen (started before Battle.net opened it,
    «Играть» still launching) keeps it waiting."""

    def __init__(self, running=None):
        if running is None:
            from .launch import wow_running as running
        self.running, self.seen, self.gone_since, self.checked = running, False, None, None

    def closed(self, now: float) -> bool:
        if self.checked is not None and now - self.checked <= 2:
            return False
        self.checked = now
        if self.running():
            self.seen, self.gone_since = True, None
        elif self.seen:
            self.gone_since = self.gone_since if self.gone_since is not None else now
            return now - self.gone_since > 10
        return False


MODELS_READY = DATA_DIR / ".models_ready"
# the launcher (bv.launcher): what the companion does, and its word to stop
STATE_FILE = DATA_DIR / "tmp" / "state.json"
STOP_FILE = DATA_DIR / "tmp" / "stop"
RESTART = 3               # exit code: start me again (online) - the launcher does it
ALREADY = 4               # exit code: another companion runs
CONFIG_ERROR = 5          # exit code: config.json cannot be read - the player's file, not the version (no rollback)
LAUNCHED = "BV_LAUNCHER"  # set in the environment of a companion the launcher started


def single_instance() -> bool:
    """True for the only companion on this Windows session (a named mutex, freed when the process
    ends however it ends); False when another one already runs - two would read the same strip
    and speak every line twice."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "Local\\BarrensVoiceCompanion")
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


_mutex = None


def install_crash_logging() -> None:
    """Uncaught errors - in the main thread or any other - go to the log too, not only the console."""
    import threading
    import traceback

    def report(kind, exc_type, exc, tb):
        log(f"ОШИБКА ({kind}): {exc_type.__name__}: {exc}")
        for chunk in traceback.format_exception(exc_type, exc, tb):
            for line in chunk.rstrip().splitlines():
                log("  " + line)

    sys.excepthook = lambda t, e, tb: report("озвучка", t, e, tb)
    threading.excepthook = lambda a: report(f"поток {a.thread.name if a.thread else '?'}", a.exc_type,
                                            a.exc_value, a.exc_traceback)


OFFLINE_VARS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")   # set by offline_models


def offline_models() -> bool:
    """After the models were loaded once (F5, Vocos, RUAccent in the Hugging Face cache), never ask
    the hub again: no update checks at start, no "unauthenticated requests" warnings, and nothing
    breaks if the site is down or a model is taken away. Must run before huggingface_hub loads."""
    if MODELS_READY.exists():
        for name in OFFLINE_VARS:
            os.environ[name] = "1"
        return True
    return False


_restart = threading.Event()   # set by restart_online off the main thread: the main loop restarts


def load_accentor(accentor, offline: bool, log, restart=None) -> None:
    """RUAccent loaded; offline, any failure starts the companion again online (restart_online) - a model missing
    from the cache, its own cut download cleared (text.Accentor). Its error is a DEBUG line then: the restart's
    WARN says what happens, and a red English «RUAccent: Cannot reach … offline mode» in the launcher's window
    was alarm for a start that mends itself. Online the error goes up, an ERROR where main() ends."""
    try:
        accentor.load()
    except Exception as e:  # noqa: BLE001 - offline with a model missing: go online once
        if offline:
            log(f"RUAccent: {e}", "DEBUG")
            (restart or restart_online)(log)
        raise


def restart_online(log) -> None:
    """A model missing from the cache in offline mode: forget the mark and start again online (under
    the launcher: exit with RESTART, it starts the companion again - so it keeps its process). In the
    console the companion runs itself again and waits for it, so the console waits too (os.execv on
    Windows started a separate process and did not quote a path with a space); off the main thread
    (engines.on_models_missing) the main loop does that, so that it stops reading the strip first."""
    MODELS_READY.unlink(missing_ok=True)
    log("в кэше моделей не всё — перезапускаюсь с сетью")
    if os.environ.get(LAUNCHED):
        os._exit(RESTART)          # from any thread (engines.on_models_missing runs on its own)
    if threading.current_thread() is not threading.main_thread():
        _restart.set()
        return
    run_again()


def run_again() -> None:
    """The console's restart: this companion's mutex freed, the same command run as a child, its exit
    code this process's. The child goes online: offline_models' variables are not handed down (the mark
    is gone, so it does not set them again)."""
    global _mutex
    if _mutex:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(_mutex)
        _mutex = None
    import subprocess
    env = {k: v for k, v in os.environ.items() if k not in OFFLINE_VARS}
    os._exit(subprocess.call([sys.executable, "-m", "bv"] + sys.argv[1:], env=env))


def describe_place(place) -> str:
    x, y, w, h, sw, sh = place
    where = "верх" if y == 0 and w == sw else "низ" if y + h == sh and w == sw else "своё место"
    return f"{where}, x {x}, y {y}, ширина {w} (экран {sw}×{sh})"


# frames that are not chat: settings, the dictionary, the addon's commands and clicks (`service`)
SERVICE_KINDS = ("control", "dict", "stop", "pause", "resume", "reset", "open", "copy", "diag")


def service(line, first: bool, engines, player, dictionary, capturer) -> None:
    """A frame of one of SERVICE_KINDS. State (control, dict, pause, resume) counts even in the frame
    that was up before the companion started; a command or a click (stop, reset, open, copy, diag)
    only in a new one - `first` is the frame already there."""
    kind = line.kind
    if kind == "control":
        # settings: applied for every frame (sapi, follow); here where the strip is (or is about to move)
        from . import locate
        place = locate.parse_place(line.options.get("strip", ""))
        if place and place != capturer.place:
            capturer.set_place(place)
            log("полоска: " + describe_place(place))
        if place:                       # kept for a restart before the game saves it (/reload, logout)
            capturer.remember(place)
        if line.volume is not None:     # the volume now (the mouse wheel): the line playing follows at once
            player.set_gain(line.gain(engines.config))
    elif kind == "dict":
        dictionary.receive(line.options, line.text)
    elif kind == "pause":
        if player.pause():
            log("CatQuest говорит: чат на паузе")
    elif kind == "resume":
        if player.resume():
            log("CatQuest замолчал: чат продолжает (прерванная строка — сначала)")
    elif first:
        return
    elif kind == "stop":
        engines.stop()
        log("стоп: замолкаю")
    elif kind == "reset":
        engines.reset_assignments()
    elif kind == "open":                                # a click in the addon's settings
        from .links import open_link
        open_link(line.text, log)
    elif kind == "copy":
        from .links import copy_link
        copy_link(line.text, log)
    elif kind == "diag":
        log(f"[диагностика аддона] {line.text}")


class Lines:
    """What a new frame's line does: the addon's settings for the engines, a frame of SERVICE_KINDS
    (`service`), and a chat line spoken - its text through `speech_text`, its speaker's name left out
    when the same character goes on (`same_speaker_gap`), logged, queued on its engine. `first` is the
    frame that was up before the companion started: its chat line is not news."""

    def __init__(self, config: dict, log, engines, sapi, player, dictionary, capturer, inflector,
                 restore_yo, seen_lines):
        self.config, self.log = config, log
        self.engines, self.sapi, self.player = engines, sapi, player
        self.dictionary, self.capturer, self.inflector, self.restore_yo = dictionary, capturer, inflector, restore_yo
        self.seen_lines = seen_lines
        self.last_speaker = None              # (kind, sender, channel) of the last chat line passed on
        self.unknown_kinds: set[str] = set()

    def handle(self, line, first: bool) -> None:
        """A parsed frame (protocol.parse; None when it is no line)."""
        if line is None:
            return
        if line.off is not None:
            self.sapi.set_disabled({n for n in line.off.split("|") if n})
        self.engines.follow(line)
        if line.volume is not None and line.kind in LABELS:   # a chat line's volume, in the order the frames come
            self.player.set_gain(line.gain(self.config))
        if line.kind in SERVICE_KINDS:
            service(line, first, self.engines, self.player, self.dictionary, self.capturer)
        elif line.kind not in LABELS:
            # a service line of a newer addon: never read aloud what is not chat
            if line.kind not in self.unknown_kinds:
                self.unknown_kinds.add(line.kind)
                self.log(f"строки типа «{line.kind}» не озвучиваю (аддон новее лаунчера?)")
        elif not line.text or first:
            return
        elif self.seen_lines.seen(line.options.get("id", "")):
            # a line the addon sent again: this one was read already
            self.log(f"строка {line.options.get('id')} уже прочитана — пропускаю повтор")
        else:
            self.speak(line)

    def speak(self, line) -> None:
        speaker_key = (line.kind, line.sender, line.channel)
        continuing = (speaker_key == self.last_speaker
                      and self.engines.quiet_for() <= self.config["same_speaker_gap"])
        self.last_speaker = speaker_key
        name, text, droppable = protocol.split_name(line.text)
        if continuing and droppable:
            name, text = "", text.lstrip()
        # the name is left as it is
        text = protocol.with_name(name, speech_text(text, self.dictionary, self.inflector, line.declension,
                                                    self.restore_yo))
        line = dataclasses.replace(line, text=text)
        speaker = self.engines.for_line(line)
        label = LABELS.get(line.kind, line.kind) + (f" {line.channel}" if line.channel else "")
        self.log(f"[{label}] {protocol.shown_sender(line.sender)} ({line.race or '?'}"
                 f"{', м' if line.sex == '2' else ', ж' if line.sex == '3' else ''})"
                 f" → {speaker.describe(line)}: {text}")
        if self.config["log_details"] and line.raw and line.raw != text:
            self.log(f"    исходно:   {line.raw}")
        speaker.say(line)


def fetch_models(config: dict) -> int:
    """Everything the race voices need from the network, downloaded now - BarrensVoice.cmd runs this
    right after installing, so the first start neither waits for ~3 GB nor needs the network: F5 in
    ONNX and its vocabulary, Whisper (it builds the voice bank from the game's files), RUAccent.
    Models already in the cache are not downloaded again."""
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")     # no "unauthenticated requests" notice; bars stay
    from huggingface_hub import hf_hub_download

    from . import f5engine, whisper_onnx
    from .text import Accentor
    try:
        log("модели: голоса рас (~0,8 ГБ) ...")
        if f5engine.model_dir(config, DATA_DIR) is None:
            f5engine.fetch_models(config, log)
        hf_hub_download(f5engine.MODEL_REPO, f5engine.VOCAB_FILE)
        log("модели: распознавание речи для банка голосов (~1,5 ГБ) ...")
        for name in whisper_onnx.FILES:
            hf_hub_download(whisper_onnx.REPO, name)
        log("модели: ударения (~0,7 ГБ) ...")
        Accentor(log, True).load()
    except Exception as e:  # noqa: BLE001 - the first start tries again
        log("модели не скачались — докачаются при первом запуске")
        log(f"    {e}")
        return 1
    log("модели скачаны")
    return 0


def utf8_output() -> None:
    """The console's output in UTF-8, a character it cannot take replaced, never an error: redirected to a file
    (`python -m bv > out.txt`), Windows gives the console's code page (cp1251), which has no «×» of «экран 1920×1080»,
    and print() raised. The launcher sets PYTHONUTF8 itself."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):     # not a text stream of its own (a test's, a wrapper)
            pass


def main() -> int:
    if sys.platform != "win32":
        print("Windows only")
        return 1
    utf8_output()
    offline = offline_models() if "--fetch-models" not in sys.argv else False
    # two notices of RUAccent's own, harmless and not ours to fix: a regex string it writes without r"" (shown
    # once, when Python first compiles it) and an argument huggingface_hub no longer uses
    import warnings
    warnings.filterwarnings("ignore", message=r"invalid escape sequence", category=SyntaxWarning, module=r".*ruaccent.*")
    warnings.filterwarnings("ignore", message=r".*local_dir_use_symlinks.*")
    install_crash_logging()
    if not isinstance(sys.stderr, StderrToLog):
        sys.stderr = StderrToLog(sys.stderr)
    from .speech import Speaker
    from .capture import Capturer
    from .window import make_dpi_aware

    make_dpi_aware()

    try:
        config = load_config()
    except (ConfigError, OSError) as e:                 # before anything else: nothing works without it
        log(f"ОШИБКА: {e}")
        log("    исправь файл или удали его — лаунчер создаст новый с настройками по умолчанию")
        from . import state
        state.init(STATE_FILE, phase="error", error=str(e))
        return CONFIG_ERROR
    for game in ("retail", "forever"):                # --retail / --forever (the launcher's): the game to read
        if f"--{game}" in sys.argv:
            config["game"] = game
    # one Hugging Face cache for every model (F5, Vocos, RUAccent, Whisper): hf_home in config.json,
    # else the hf folder next to the program - before anything of Hugging Face is imported
    os.environ["HF_HOME"] = config.get("hf_home") or str(CACHE_DIR / "hf")
    # Windows without developer mode has no symlinks for a normal user: the cache keeps plain copies, which
    # costs nothing here (each model once, one revision) - the library's long warning about it is noise
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # transformers (Whisper's features and tokenizer, RUAccent) says at import that it found no
    # PyTorch - on purpose since 0.4.0: only its errors are worth the log
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    # the game first: ONNX Runtime's threads few and sleeping, the companion below normal (bv/share.py)
    from . import share
    if config.get("share_with_game", True):
        share.limit_onnx(int(config.get("onnx_threads", 2)))
    if "--selftest" in sys.argv:
        from .speech import selftest
        selftest(config, log)
        return 0
    if "--selftest-f5" in sys.argv:
        from .f5engine import selftest as f5_selftest
        return f5_selftest(config, log, DATA_DIR)
    if "--fetch-models" in sys.argv:
        return fetch_models(config)
    log("=" * 60)
    if not single_instance():
        log("озвучка уже запущена — вторая не нужна, выхожу")
        return ALREADY
    STOP_FILE.unlink(missing_ok=True)
    if config.get("share_with_game", True):
        share.lower_priority(log)
    from . import state
    from .dictionary import game_of
    state.init(STATE_FILE, phase="starting", game=game_of(config))
    # the launcher says «компаньон запускается (игра)» itself: here only the details, for the full log
    log(f"озвучка стартует, игра: {'WoW (ритейл)' if game_of(config) == 'retail' else 'WoW Forever'}, "
        f"настройки: {CONFIG_PATH}", "DEBUG")
    prune_logs(config["log_keep_days"])
    from . import launch
    from .status import Beat, SeenLines
    from .voicelist import find_addon, missing_addon
    problem = missing_addon(config)
    if problem:                   # nothing would ever reach the companion: say so and stop
        log("ОШИБКА: " + problem)
        state.report(phase="error", error=problem)
        return 1
    # "the companion is running", for the addon at login (status.py): from now on, before the game
    # is launched and while the models load
    # asked before the first beat: is the player already in the world, the addon past its check? (reloadhint)
    from . import reloadhint
    needs_reload = reloadhint.needed(config, find_addon(config))
    beat = Beat(find_addon(config), log)
    beat.start()
    if needs_reload:
        log("если окно чата не прячется — /reload в игре")
        state.report(reload_hint=True)
    import atexit
    atexit.register(beat.stop)        # any end - the game closed, a crash, the launcher's stop - but a kill
    # «Что нового» of this version for the addon (bv/news.py): before the game is launched, so its login has them
    from . import news
    news.write(find_addon(config), log)
    seen_lines = SeenLines(DATA_DIR / "tmp" / "seen_lines.txt")
    # --launch / --no-launch: the launcher's Play and its start for a game already running
    want_launch = "--launch" in sys.argv or (config["launch_wow"] and "--no-launch" not in sys.argv)
    if want_launch and launch.launch_allowed(log):
        # the race voices come from the client's files: (re)made before the game, after an update
        from .f5engine import prepare_voices
        prepare_voices(config, DATA_DIR, log)
        launch.launch(config, log)   # the game starts while the voices load (not if the player started it meanwhile)
    from .dictionary import Dictionary
    dictionary = Dictionary(config, log, find_addon(config))
    dictionary.refresh()
    dictionary_checked = time.monotonic()
    # the dictionary online (bv/online_dictionary.py): on in an install, off run from the source (where the
    # addon's own Dictionary.lua is being edited); config.json "dictionary_online" says otherwise
    from . import online_dictionary
    from .paths import INSTALL_DIR
    online = None
    if config.get("dictionary_online", INSTALL_DIR is not None):
        online = online_dictionary.Online(DATA_DIR, log)
        if online.cached():
            dictionary.use_online(online.current)
            if online.current["revision"] > dictionary.revision:
                online_dictionary.write_addon(dictionary.addon, online.current, log)
        online.start()
    else:
        online_dictionary.clear_addon(dictionary.addon, log)
    from .text import Accentor
    from . import yo
    restore_yo = yo.restore if config.get("restore_yo", True) else (lambda t: t)
    if config.get("restore_yo", True):
        log(f"словарь «ё»: {sum(1 for k in yo.table() if k[:1].islower())} форм слов")
    accentor = Accentor(log, config.get("ruaccent", True))
    log("загружаю RUAccent ..." + (" (модели из кэша, без сети)" if offline else ""))
    load_accentor(accentor, offline, log)
    from .morph import Inflector
    inflector = Inflector(log, config.get("declension", True))
    inflector.load()
    from .audio import Player
    player = Player(config["max_queue"], log=log, gain=config["volume"] / 100)
    player.start()
    sapi = Speaker(config, log, accentor, player)
    sapi.start()
    sapi.ready.wait(120)
    log(f"голосов Windows: {len(sapi.all_voices)} (включено {len(sapi.voices)})")
    from . import voicelist
    voicelist.write(config, sapi.all_voices, sapi.default_off, log)
    from .engines import Engines
    engines = Engines(config, log, DATA_DIR, sapi, accentor, player)
    engines.on_ready = lambda: MODELS_READY.touch()
    engines.on_models_missing = (lambda error: restart_online(log)) if offline else None
    # The race voices load while the game starts, not when its first chat line comes - unless the
    # addon was last set to Windows voices (then F5 is loaded only if the addon asks for it).
    from .dictionary import saved_voices
    if "--f5" in sys.argv or (saved_voices(config) or config["engine"]) == "f5":
        engines.want("f5")

    game = GameWatch()
    capturer = Capturer(config, log)
    from . import locate
    known = locate.known_place(config)
    if known:                     # where the strip last was; the addon's settings frame says it again
        place, source = known
        capturer.set_place(place)
        log(("полоска (по сохранённым настройкам аддона): " if source == "saved" else
             "полоска (где её последний раз видел лаунчер — новее сохранённых настроек аддона): ")
            + describe_place(place))
    from .bridge import StripReader
    strip = StripReader(capturer, config, log, state.report)
    lines = Lines(config, log, engines, sapi, player, dictionary, capturer, inflector, restore_yo, seen_lines)
    state.report(phase="running")
    while True:
        if _restart.is_set():                  # restart_online from another thread
            run_again()
        now = time.monotonic()
        if now - dictionary_checked > 2:
            dictionary.refresh()               # a changed Dictionary.lua works within seconds
            dictionary_checked = now
            fresh = online.take() if online else None
            if fresh and fresh["revision"] > dictionary.revision:      # applied here, between chat lines
                dictionary.use_online(fresh)
                online_dictionary.write_addon(dictionary.addon, fresh, log)
                log("свежий словарь с GitHub — в окне словаря после /reload")
            if STOP_FILE.exists():             # the launcher's "stop" (its window closed, «Выход»)
                STOP_FILE.unlink(missing_ok=True)
                log("остановлен лаунчером")
                state.report(phase="stopped")
                return 0
        if game.closed(now):
            log("игра закрыта — выключаю озвучку")
            state.report(phase="game_closed")
            return 0
        step = strip.poll(now)
        if step.frame is not None:
            lines.handle(protocol.parse(step.frame.payload), step.first)
        time.sleep(step.pause)


if __name__ == "__main__":
    sys.exit(main())
