"""The launcher's knowledge, without a window (so the tests can run it anywhere): which games are
installed and with which addon, what the companion is doing in words, the settings in config.json,
the version on GitHub, starting with Windows."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ..dictionary import GAMES, wow_roots

from ..install import version_key  # noqa: F401 - versions in order, a test build before its release
from ..paths import CODE_DIR, DATA_DIR, starter
from ..update import AGENT, REPO, newer
APP_DIR = CODE_DIR                                       # the companion's folder: bv/, the addon beside it
CONFIG_PATH = DATA_DIR / "config.json"
GAME_NAMES = {"forever": "WoW Forever", "retail": "WoW Retail"}
ADDON = "BarrensVoice"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Barrens Voice"

GREEN, YELLOW, RED, GREY = "#4ccf6a", "#e0b33a", "#e05a4a", "#8a7a66"


# --- the addon and the version ----------------------------------------------------------------

def addon_source(app_dir: Path = APP_DIR) -> Path | None:
    """The addon that comes with this companion: addon/BarrensVoice next to it (an installed copy),
    or in the repository the companion folder sits in."""
    for path in (app_dir / "addon" / ADDON, app_dir.parent / "addon" / ADDON):
        if (path / f"{ADDON}.toc").is_file():
            return path
    return None


def toc_version(folder: Path | None) -> str | None:
    """The addon's `## Version:` (None when there is no addon there)."""
    if folder is None:
        return None
    try:
        text = (Path(folder) / f"{ADDON}.toc").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^## Version:\s*(\S+)", text, re.M)
    return match.group(1) if match else "?"


def app_version(code_dir: Path = APP_DIR) -> str:
    """The launcher's and companion's version: pyproject.toml beside the code (the release's root, or
    the repository's); the addon has its own, in its .toc (addon_version)."""
    import tomllib
    try:
        return tomllib.loads((code_dir.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, ValueError, KeyError):
        return "?"


def addon_version() -> str:
    """The version of the addon that comes with this launcher (what «установить/обновить» puts in the game)."""
    return toc_version(addon_source()) or "?"


SKIP = ("*.md", "._*")      # notes for the repository, macOS copy litter: not for the game
# written by the companion into the addon's folder (bv/voicelist.py, bv/status.py, bv/online_dictionary.py, bv/news.py)
# and shipped only as empty stand-ins: an install keeps the companion's own, or the voice list and «Что нового» go
# empty and the addon says the companion is not running until the companion writes them again
GENERATED = ("VoiceList.lua", "CompanionStatus.lua", "DictionaryOnline.lua", "NewsData.lua")


def install_addon(source: Path, game_root: Path, *, preserve_extra: bool = False) -> Path:
    """Put the addon into the game's Interface/AddOns as `source` has it: copied over the old one, and
    what the old one had and this one does not removed (the player's settings live in WTF, not here).
    Returns the addon's folder in the game."""
    target = Path(game_root) / "Interface" / "AddOns" / ADDON
    target.parent.mkdir(parents=True, exist_ok=True)
    kept = [name for name in GENERATED if (target / name).is_file()]
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*SKIP, *kept))
    if preserve_extra:
        return target
    wanted = {p.relative_to(source) for p in source.rglob("*")
              if not any(p.match(pattern) for pattern in SKIP)}
    for path in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):   # files before their folders
        if path.relative_to(target) not in wanted:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
    return target


# --- the games ----------------------------------------------------------------------------------

@dataclass
class Game:
    key: str              # "forever", "retail"
    name: str             # "WoW Forever"
    root: Path            # …\World of Warcraft\_forever_
    addon: str | None     # the installed addon's version, None if not installed

    @property
    def addon_dir(self) -> Path:
        return self.root / "Interface" / "AddOns" / ADDON


def registry_roots() -> list[Path]:
    """World of Warcraft folders Windows knows from the installer (Battle.net writes the retail one)."""
    if sys.platform != "win32":
        return []
    import winreg
    roots = []
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            if "world of warcraft" not in name.lower():
                continue
            try:
                location, _ = winreg.QueryValueEx(winreg.OpenKey(key, name), "InstallLocation")
                roots.append(Path(location))
            except OSError:
                pass
    return roots


def battlenet_roots() -> list[Path]:
    """Where Battle.net installs games by default (its own settings: «DefaultInstallPath»)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    try:
        settings = json.loads((Path(appdata) / "Battle.net" / "Battle.net.config").read_text(encoding="utf-8"))
        base = settings.get("Client", {}).get("Install", {}).get("DefaultInstallPath")
    except (OSError, ValueError, AttributeError):
        return []
    return [Path(base) / "World of Warcraft"] if base else []


def find_games(config: dict, extra: list[Path] | None = None) -> list[Game]:
    """The installed games the companion works with, each with its folder and addon version. A
    game is there when its folder has Wow.exe. Looked for, best first: the folder remembered when
    the game last ran (`game_dirs`) and `wow_dir`, the folders Windows knows from the installer
    (retail), Battle.net's default install folder, the usual places on every local disk."""
    games = []
    bases = registry_roots() + battlenet_roots() + list(extra or [])
    for key, flavor in GAMES.items():
        candidates = wow_roots({**config, "game": key})
        candidates[1:1] = [b / flavor for b in bases] + [b for b in bases if b.name.lower() == flavor]
        root = next((r for r in candidates if (r / "Wow.exe").is_file()), None)
        if root is not None:
            games.append(Game(key, GAME_NAMES[key], root, toc_version(root / "Interface" / "AddOns" / ADDON)))
    return games


def game_folder(text: str, game: str) -> str:
    """A folder typed or picked for a game, as the game's own: «…\\World of Warcraft» -> «…\\World of Warcraft\\_forever_»
    when that is where the game is; "" for an empty field."""
    if not text:
        return ""
    folder = Path(text)
    flavor = GAMES[game]
    if folder.name.lower() != flavor and (folder / flavor / "Wow.exe").is_file():
        folder = folder / flavor
    return os.path.normpath(str(folder))


def remember(config: dict, game: str, folder: Path) -> dict | None:
    """The changes to save when a game ran from `folder`: its folder under `game_dirs` (None if known)."""
    dirs = dict(config.get("game_dirs") or {})
    if dirs.get(game) == str(folder):
        return None
    dirs[game] = str(folder)
    return {"game_dirs": dirs}


# --- config.json --------------------------------------------------------------------------------

class BrokenConfig(ValueError):
    """config.json is there but is not a JSON object of settings (a hand edit gone wrong: a single backslash
    in a path, a missing comma), or it cannot be read at all right now (in use, an antivirus scan, its
    permissions). Its text is for the player (Russian: the line and column); `detail` the parser's own words, for
    the log window."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class ConfigNotOpened(BrokenConfig):
    """config.json there but not opened: another program holds it, or its permissions keep it from this user."""


class ConfigNotWritten(BrokenConfig):
    """config.json read but not written: marked read-only, or another program holds it right now."""


def read_config(path: Path = CONFIG_PATH, strict: bool = False) -> dict:
    """The companion's settings as it reads them: its defaults with config.json over them. A file that does
    not parse or cannot be read gives the defaults, or BrokenConfig when `strict` - only a file that is not
    there gives the defaults then (the defaults written over an unreadable file lost the player's settings)."""
    from ..__main__ import DEFAULT_CONFIG
    config = dict(DEFAULT_CONFIG)
    try:
        try:
            text = Path(path).read_text(encoding="utf-8-sig")             # -sig: Notepad's BOM is no error
        except OSError as e:
            if isinstance(e, FileNotFoundError):
                raise
            import time
            time.sleep(0.1)                     # held a moment by another program (an antivirus, an editor's save)
            text = Path(path).read_text(encoding="utf-8-sig")
        saved = json.loads(text)
    except FileNotFoundError:
        return config
    except OSError as e:
        if strict:
            raise ConfigNotOpened(f"config.json не открывается: {e.strerror or e}") from e
        return config
    except ValueError as e:
        if strict:
            if isinstance(e, json.JSONDecodeError):
                raise BrokenConfig(f"config.json не читается: ошибка в строке {e.lineno}, столбец {e.colno}",
                                   f"{e.msg} (char {e.pos})") from e
            raise BrokenConfig("config.json не читается", str(e)) from e
        return config
    if not isinstance(saved, dict):
        if strict:
            raise BrokenConfig("config.json не читается: в нём не настройки в фигурных скобках")
        return config
    config.update(saved)
    return config


def config_problem(path: Path = CONFIG_PATH) -> str | None:
    """Why config.json cannot be read (None when it can, or when there is none yet)."""
    error = config_error(path)
    return str(error) if error else None


def config_error(path: Path = CONFIG_PATH) -> BrokenConfig | None:
    """What is wrong with config.json (BrokenConfig, its words and detail), None when it reads or is not there."""
    try:
        read_config(path, strict=True)
        return None
    except BrokenConfig as e:
        return e


def update_config(path: Path = CONFIG_PATH, **changes) -> dict:
    """Change some settings in config.json, keeping the rest as the file has them. A file that does not parse
    is never written over (BrokenConfig): the player's names and voices are in it, to be fixed by hand. A write
    that fails (ConfigNotWritten) leaves the file as it was and no config.tmp beside it."""
    config = read_config(path, strict=True)
    config.update(changes)
    tmp = Path(path).with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigNotWritten(f"config.json не записывается: {e.strerror or e}") from e
    return config


# --- what the companion is doing, in words ----------------------------------------------------

EXIT_WORDS = {1: "Озвучка остановилась с ошибкой — подробности в журнале", 4: "Озвучка уже запущена отдельно",
              5: "Озвучка не прочитала config.json — подробности в журнале"}


def describe(state: dict, running: bool, exit_code: int | None, game: str | None, addon_missing: bool) -> list:
    """The four lines of «Состояние»: (colour, text) for the companion, the game, the race voices
    and the bridge. `state` is the companion's tmp/state.json, `game` the name of the running game."""
    phase = state.get("phase")
    if addon_missing:
        first = (RED, "Аддон не установлен — «Установить» в блоке «Игра»")
    elif running:
        first = (GREEN, "Озвучка работает") if phase == "running" else (YELLOW, "Озвучка запускается…")
    elif exit_code is None:
        first = (GREY, "Озвучка не запущена")
    elif phase == "error" and state.get("error"):
        first = (RED, "Озвучка: " + state["error"][:90])
    elif phase == "game_closed":
        first = (GREY, "Озвучка остановлена: игра закрыта")
    else:
        first = (RED if exit_code else GREY, EXIT_WORDS.get(exit_code, "Озвучка остановлена"))

    second = (GREEN, f"{game} запущена") if game else (GREY, "Игра не запущена")

    voices = state.get("voices") if running else None
    engine = state.get("provider", "")
    if state.get("f5_steps"):
        engine += f" · {state['f5_steps']} шагов"
    error_text = ("Проверь устройство вывода Windows и повтори проверку" if
                  str(state.get("voices_error", "")).startswith("Не удалось открыть аудиовыход Windows") else
                  "Голоса рас недоступны — читаю голосами Windows" if state.get("windows_fallback", True) else
                  "Голоса рас недоступны · озвучка на паузе")
    third = {
        "bank": (YELLOW, "Голоса рас: готовлю образцы из WoW" + state.get("bank_progress", "…")),
        "loading": (YELLOW, "Голоса рас загружаются…"),
        "warming": (YELLOW, "Проверяю CUDA и звуковое устройство…"),
        "tensorrt": (YELLOW, "Голоса рас: подготовка библиотек TensorRT…"),
        "ready": (GREEN, "Голоса рас готовы" + (f" — {engine}" if engine else (f" — {state['device']}" if state.get("device") else ""))),
        "error": (RED, error_text),
        "off": (GREY, "Голоса рас выключены: в аддоне выбраны голоса Windows"),
    }.get(voices, (GREY, "Голоса рас: ещё не загружены"))

    if not running:
        fourth = (GREY, "Мост: нет связи")
    elif state.get("mismatch") == "newer":
        fourth = (RED, "Мост: аддон новее лаунчера — обнови Barrens Voice Launcher")
    elif state.get("mismatch") == "older":
        fourth = (RED, "Мост: аддон устарел — «обновить аддон» и /reload")
    elif game and state.get("window") is False:       # before «связь есть»: no window, no link, whatever was said
        fourth = (YELLOW, "Мост: окно игры свёрнуто или не найдено")
    elif state.get("linked"):
        fourth = (GREEN, "Мост: связь с аддоном есть")
    elif state.get("damaged"):
        fourth = (RED, "Мост: полоску что-то перекрывает — см. журнал")
    elif game:
        fourth = (YELLOW, "Мост: ждём связь с аддоном")
    else:
        fourth = (GREY, "Мост: ждём игру")
    return [first, second, third, fourth]


# --- updates ------------------------------------------------------------------------------------

def latest_release(timeout: float = 10.0) -> dict | None:
    """The newest release on GitHub: {"version", "url"}; None when there is none or no network."""
    import urllib.request
    request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json",
                                              "User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - no network, no release yet (404), GitHub down: just no news
        return None
    tag = data.get("tag_name") or ""
    return {"version": tag.lstrip("v"), "url": data.get("html_url") or f"https://github.com/{REPO}/releases"} if tag else None


# --- the command line ---------------------------------------------------------------------------

USAGE = """BarrensVoice.pyw [ключи] (или BarrensVoice.cmd [ключи])

--forever, --retail   с какой игрой работать (как у компаньона)
--console             компаньон в окне консоли, без лаунчера
--tray                сразу в трей, компаньон — когда запустится игра (так при входе в Windows)
--f5                  загрузить голоса рас, даже если в аддоне выбраны голоса Windows
--launch, --no-launch запустить игру / не запускать её при старте компаньона
--selftest, --selftest-f5   проверка голосов в файлы (с --console)
--open settings|help|log  сразу открыть «Настройки», «Помощь» или «Журнал»
--scale 1.25          свой масштаб окна (обычно — как у экрана)
--pos 100,200         где открыть окно: его левый верхний угол на экране
--updated             так лаунчер перезапускает себя после обновления: игру он тогда не запускает
--help                эта справка"""

COMPANION_FLAGS = ("--f5", "--launch", "--no-launch", "--selftest", "--selftest-f5")


def parse_args(argv: list[str]) -> dict:
    """The launcher's command line: its own switches, the game, and the companion's switches it
    passes on to the companion."""
    out = {"tray": False, "console": False, "help": False, "updated": False, "game": None, "open": None, "pos": None,
           "scale": None, "companion": [], "unknown": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        value = argv[i + 1] if i + 1 < len(argv) else None
        if a in ("--help", "-h", "/?"):
            out["help"] = True
        elif a in ("--tray", "--console", "--updated"):
            out[a[2:]] = True
        elif a in ("--forever", "--retail"):
            out["game"] = a[2:]
        elif a in COMPANION_FLAGS:
            out["companion"].append(a)
        elif a in ("--open", "--pos", "--scale") and value is not None:
            try:
                if a == "--scale":
                    out["scale"] = float(value)
                elif a == "--pos":
                    x, y = value.split(",")                  # "100,200": the window's corner (ValueError if not)
                    out["pos"] = (int(x), int(y))
                else:
                    out["open"] = value
            except ValueError:
                out["unknown"].append(f"{a} {value}")
            i += 1
        else:
            out["unknown"].append(a)
        i += 1
    return out


# --- starting with Windows ----------------------------------------------------------------------

def launcher_command() -> str:
    """How Windows starts the launcher at sign-in: quietly into the tray (the installed starter, so a
    later version starts, not this one)."""
    return f"{starter()} --tray"


def autostart() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, launcher_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass


# --- the bug report ------------------------------------------------------------------------------

PATH_KEYS = ("wow_dir", "hf_home", "battlenet_exe", "f5_onnx_dir")   # a folder of the player's own
NAME_KEYS = ("exclude", "gender_overrides", "players")                # people's names
HIDDEN_KEYS = ("launcher_dismissed", "launcher_tray_told")            # the launcher's own notes
DRIVE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
ACCOUNT = re.compile(r"(?i)(\\WTF\\Account\\)[^\\\s\"']+")


def shown_setting(key: str, value) -> str:
    """A changed setting as the report shows it: names counted, folders only said to be the player's own."""
    if key in NAME_KEYS:
        return f"{len(value)} шт." if isinstance(value, (list, dict)) else "задано"
    if key == "game_dirs":
        return ", ".join(sorted(value)) if isinstance(value, dict) else "задано"
    if key in PATH_KEYS or isinstance(value, str) and DRIVE_PATH.match(value):
        return "свой путь" if value else "пусто"
    return json.dumps(value, ensure_ascii=False)


def changed_settings(config: dict, defaults: dict) -> list[str]:
    """`key = value` for every setting that differs from the companion's defaults, without personal data."""
    return [f"{key} = {shown_setting(key, value)}" for key, value in sorted(config.items())
            if key not in HIDDEN_KEYS and not key.startswith("_") and (key not in defaults or value != defaults[key])]


def hide_private(text: str, home: str | None = None) -> str:
    """The user's home folder as ~ and the WoW account name in WTF\\Account\\ as ***."""
    home = home if home is not None else os.path.expanduser("~")
    if home and home not in ("~", os.sep):
        text = re.sub(re.escape(home), "~", text, flags=re.IGNORECASE)
    return ACCOUNT.sub(r"\1***", text)


def gpu_names() -> list[str]:
    """Every display adapter Windows lists (an integrated one and the discrete one, say)."""
    names = []
    try:
        import win32api
        for i in range(16):
            try:
                name = str(win32api.EnumDisplayDevices(None, i).DeviceString).strip()
            except Exception:  # noqa: BLE001 - past the last adapter
                break
            if name and name not in names:
                names.append(name)
    except ImportError:
        pass
    return names


def onnx_runtime() -> str:
    """The ONNX Runtime build installed: its package (CUDA or DirectML) and version."""
    from importlib import metadata
    for package in ("onnxruntime-gpu", "onnxruntime-directml", "onnxruntime"):
        try:
            return f"{package} {metadata.version(package)}"
        except metadata.PackageNotFoundError:
            continue
    return "не установлен"


VOICES = {"loading": "загружаются", "ready": "готовы", "error": "ошибка", "off": "выключены",
          "bank": "собираются из клиента"}


def report_header(version: str, state: dict, config: dict, games: list, running_game: str | None,
                  running: bool = True, gpus: list[str] | None = None, runtime: str | None = None) -> str:
    """What a bug report needs besides the log: versions, the video card, the games, the settings
    changed - without paths, the account or anyone's names. `state` is the companion's last; `running`
    whether it runs now (the file stays as it last wrote it)."""
    import platform

    from ..__main__ import DEFAULT_CONFIG
    gpus = gpu_names() if gpus is None else gpus
    if not gpus and state.get("device"):             # Windows lists no adapters outside a desktop session
        gpus = [state["device"]]
    phase = state.get("phase") or "не запускался"
    voices = VOICES.get(state.get("voices", ""), state.get("voices") or "не загружались")
    if state.get("voices") == "ready" and state.get("device"):
        voices += f" на {state['device']}" + (f" ({state['provider']})" if state.get("provider") else "")
    if state.get("voices_error"):
        voices += f": {state['voices_error']}"
    played = ", ".join(f"{g.name} (аддон {g.addon or 'не установлен'})" for g in games) or "не найдены"
    lines = [
        f"Barrens Voice {version} · Windows {platform.version()} · Python {platform.python_version()}",
        f"ONNX Runtime: {runtime or onnx_runtime()}",
        f"Видеокарты: {', '.join(gpus) or 'Windows не назвала'}",
        f"Голоса рас: {voices}",
        f"Игры: {played}. Запущена: {GAME_NAMES.get(running_game, 'нет') if running_game else 'нет'}",
        f"Озвучка: {phase if running else f'не запущена (в прошлый раз: {phase})'}",
    ]
    # one setting a line: a value may have commas of its own (game_dirs, a list)
    changed = changed_settings(config, DEFAULT_CONFIG)
    lines += ["Настройки не по умолчанию:"] + [f"  {c}" for c in changed] if changed else ["Настройки не по умолчанию: нет"]
    return hide_private("\n".join(lines))


MAIN_LEVELS = ("INFO", "WARN", "ERROR")          # the main window's log: how things go, not every detail
# lines of one long job: the main window keeps only the newest of each group, a line that moves on in place
PROGRESS = (("собираю голоса рас", "голоса рас собраны"),)


def main_lines(items, keep: int = 14) -> tuple:
    """What the main window's log shows: INFO, WARN and ERROR, of a PROGRESS group only the newest."""
    shown, groups = [], set()
    for item in reversed(items):
        if item[1] not in MAIN_LEVELS:
            continue
        group = next((g for g in PROGRESS if item[2].startswith(g)), None)
        if group is not None:
            if group in groups:
                continue
            groups.add(group)
        shown.append(item)
        if len(shown) == keep:
            break
    return tuple(reversed(shown))


def log_view(total: int, fits: int, back: int) -> tuple[int, int]:
    """The main window's log scrolled: `back` rows up from the newest, kept within the rows there are.
    (the first row shown, `back` as kept) - the newest `fits` rows with back 0."""
    back = max(0, min(back, total - fits))
    return max(0, total - fits - back), back


def log_rows_after(last, log, counts) -> int:
    """The rows of the lines that came after `last` (the newest line at the last drawing), `counts` the rows of each
    line of `log`. With 200 lines kept the oldest goes as a new one comes - the count of rows stays, and only this
    tells how far a view scrolled up must move to stay put. 0 when `last` is gone (a progress line moved on)."""
    if last is None:
        return 0
    for i in range(len(log) - 1, -1, -1):
        if log[i] == last:
            return sum(counts[i + 1:])
    return 0


def log_thumb(total: int, fits: int, first: int, y0: float, y1: float, least: float = 24) -> tuple[float, float] | None:
    """The scroll bar's thumb between `y0` and `y1` for rows first..first+fits of `total`; None when all fit."""
    if total <= fits:
        return None
    size = max(least, (y1 - y0) * fits / total)
    top = y0 + (y1 - y0 - size) * first / (total - fits)
    return top, top + size


def log_back_at(total: int, fits: int, top: float, y0: float, y1: float, least: float = 24) -> int:
    """The `back` that puts the thumb's top at `top` (dragging it): log_thumb the other way round."""
    if total <= fits:
        return 0
    size = max(least, (y1 - y0) * fits / total)
    room = y1 - y0 - size
    first = round((top - y0) / room * (total - fits)) if room > 0 else 0
    return log_view(total, fits, total - fits - first)[1]


def addons_to_update(games, version: str, running: str | None) -> list:
    """The games whose installed addon is older than `version` and not running now."""
    return [g for g in games if g.addon is not None and g.key != running and newer(version, g.addon)]
