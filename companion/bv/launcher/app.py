"""Barrens Voice launcher: a window instead of the companion's console.

It finds the installed games, keeps the addon in them up to date, starts the companion (a child
process without a console: bv.launcher.process) and shows what it does - the state from its
tmp/state.json, its log lines from its output - and starts the game through Battle.net. The window's
cross quits, the companion with it. It waits by the clock (tray) only when started with --tray (at
Windows sign-in: the companion starts when the game appears) and while a game it started runs; «Выход»
in the tray's menu quits then.

Run: pythonw BarrensVoice.pyw  (or python -m bv.launcher)
"""
from __future__ import annotations

import collections
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

from . import model
from ..install import display_version, release_tag
from ..paths import DATA_DIR, INSTALL_DIR, starter
from .model import APP_DIR, GAME_NAMES
from .paint import DIM, GOLD, HERE, PARCHMENT, Painter, ScaledCanvas, register_font, window_icon
from ..logs import level_of, parse
from .process import Companion
from .tray import Tray

W = 840
LX0, LX1, RX0, RX1 = 22, 462, 478, W - 22       # left column (state, log), right column (start, game)
TOP, GAP, ROW1 = 100, 14, 140                   # State and Start: one height
MAIN_COLORS = {"WARN": "#e0b33a", "ERROR": "#ff6a5a"}
# hints on the main window's checks and links (what the window does not show by itself)
TIPS = {
    "launch_wow": "Открываешь лаунчер ярлыком — он сразу запускает игру через Battle.net и уходит в трей.\n\n"
                  "Если лаунчер уже ждёт в трее, ярлык тоже запускает игру.",
    "exit_with_wow": "Игра закрылась — лаунчер закрывается целиком.\n\n"
                     "Выключено — он остаётся ждать следующую игру (в окне или в трее, как был).\n\n"
                     "Озвучка после игры выключается в любом случае и освобождает видеопамять.",
    "exit_with_wow_off": "Лаунчер запускается при входе в Windows, поэтому после закрытия игры он остаётся в трее "
                         "и ждёт следующего запуска WoW.\n\nЧтобы он закрывался вместе с игрой, сними «Запускать при входе в Windows» и поставь эту галочку.",
    "autostart": "Лаунчер запускается при входе в Windows и ждёт в трее (значок у часов).\n\n"
                 "Озвучка включается сама, когда ты запустишь игру, и выключается, когда закроешь. До запуска и после "
                 "закрытия игры озвучка в память не загружена и ресурсов не занимает.",
    "install": "Скопировать аддон Barrens Voice в папку игры (Interface\\AddOns\\BarrensVoice).\n\n"
               "Если игра запущена — потом /reload.",
    "update_now": "Версия готова и сама установится, когда ты закроешь игру.\n\n"
                  "Установить сейчас — лаунчер перезапустится с ней, озвучка прервётся примерно на полминуты (пока "
                  "загружаются голоса).\n\nЕсли менялся аддон — в игре потом нужен /reload.",
    "update": "Заменить аддон в игре версией, которая пришла с лаунчером. Твои настройки аддона сохранятся.\n\n"
              "Если игра запущена — потом /reload.",
}
SHOW_FILE = DATA_DIR / "tmp" / "launcher.show"   # a second launcher asks the first to show itself ("play" in it: and to
                                                 # launch the game, as a start by hand would)
LAUNCHER_LOG = DATA_DIR / "logs" / "launcher.log"
TICK_MS = 400
UPDATE_EVERY = 3600          # an hour; and at every start
CONFIG_EVERY = 2.0           # seconds between looks at whether config.json reads (check_config)
LOG_GUTTER = 12              # the log's text stops this short of the right edge: room for its scroll bar
PRUNE_AFTER_MS = 5000        # old versions removed this long after the start (update.prune_unwatched)
# «Игра запускается…» this long after Battle.net was asked, unless the game came or the launch said it gave up:
# a cold Battle.net takes up to two minutes (launch.follow_cold_start), the game's process a few seconds more
LAUNCH_WAIT = 130.0


def rotate_log(path: Path, limit: int = 1_000_000) -> None:
    """The log moved to .log.1 once it is over `limit`. Best effort: a windowless launcher holds launcher.log open
    as its stderr, and Windows will not move an open file - main() rotates it before that."""
    try:
        if path.exists() and path.stat().st_size > limit:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass


def launcher_log(message: str) -> None:
    rotate_log(LAUNCHER_LOG)                 # failing, it no longer keeps the line from being written
    try:
        LAUNCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LAUNCHER_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


class App:
    def __init__(self, root: tk.Tk, args: dict, scale: float = 1.0):
        self.root, self.args, self.scale = root, args, scale
        self.resident = resident = args["tray"]
        self.title_font = register_font()
        root.title("Barrens Voice Launcher")
        root.resizable(False, False)
        window_icon(root)
        self.canvas = ScaledCanvas(root, scale, width=W, height=560, highlightthickness=0, bg="#0b0705")
        self.canvas.pack()
        self.p = Painter(self.canvas, self.title_font)
        for event, handler in (("<Motion>", self.log_pointer), ("<Leave>", self.log_pointer),
                               ("<MouseWheel>", self.log_wheel), ("<Button-4>", self.log_wheel),
                               ("<Button-5>", self.log_wheel), ("<B1-Motion>", self.log_dragged),
                               ("<ButtonRelease-1>", self.log_released)):
            self.canvas.bind(event, handler, add="+")
        self.canvas.tag_bind("logbar", "<Button-1>", self.log_pressed)

        self.config = model.read_config()
        self.config_told: set[str] = set()   # what was said of config.json: "broken" (does not parse, cannot be
                                             # read), "unwritten" (read, not written) - each once until a good write
        self.pending: dict = {}          # changes config.json did not take: written with the next change that it takes
        self.version = model.app_version()
        self.addon_version = model.addon_version()
        self.addons_tried: set[tuple[str, str]] = set()
        self.updater = None              # an install's updates (bv/update.py Updater); None run from the source
        if INSTALL_DIR is not None:
            from ..update import Updater
            self.updater = Updater(INSTALL_DIR, self.version, lambda: self.config, self.say,
                                   detail=lambda m: self.incoming.put(f"{datetime.now():%H:%M:%S} DEBUG [обновление] {m}"))
        self.companion_started = 0.0
        self.settle_due = False          # this run of the companion may still settle a fresh version (settle_running)
        self.games = model.find_games(self.config)
        keys = [g.key for g in self.games]
        self.selected = self.config.get("game") if self.config.get("game") in keys else (keys[0] if keys else None)
        if args["game"] in keys:                   # --retail / --forever, as the companion takes them
            self.selected = args["game"]
        self.autostart = model.autostart()

        # (time, level, message): every line of the companion and the launcher (bv/logs.py); the main window
        # shows model.MAIN_LEVELS of them, the log window all
        self.lines: collections.deque[tuple[str, str, str]] = collections.deque(maxlen=20000)
        # the main window's own: a busy chat (a line, «исходно», «к синтезу» each) pushed the last INFO lines
        # out of any window of recent lines, and the main window went blank
        self.main: collections.deque[tuple[str, str, str]] = collections.deque(maxlen=200)
        self.log_back = 0                # the main window's log scrolled this many rows up from the newest
        self.log_last = None             # its newest line at the last drawing: rows after it are new (a scrolled view stays)
        self.log_wrapped: dict = {}      # its lines as wrapped into rows: wrapped again only when new (scrolling redraws)
        self.log_box = None              # its rows' place and its bar's track (redraw), for the mouse
        self.log_hover = False           # the pointer over the log: its scroll bar shown
        self.log_drag = None             # the thumb held: (pointer y, thumb top) where it was taken
        self.incoming: queue.Queue[str] = queue.Queue()
        self.log_window = None
        self.events: queue.Queue[str] = queue.Queue()
        self.companion = Companion(APP_DIR, self.incoming.put)
        self.companion_game: str | None = None
        self.exit_seen = True            # the companion's end handled (or it never ran)
        self.busy = ""                   # "stopping", "restarting": a stop is on its way
        self.last_code: int | None = None   # how the companion last ended
        self.running_game: str | None = None
        # the game on its way: ("asked", t) - a companion started to launch it, its launch not logged yet (the race
        # voices may be built first); ("sent", t) - Battle.net asked (launch.LAUNCHING); None - nothing on its way
        self.launching: tuple[str, float] | None = None
        self.extra = args["companion"]   # the companion's own switches given to the launcher (--f5 ...)
        self.update: dict | None = None
        self.drawn = None
        self.settings_win = self.help_win = None
        self.tray = Tray(self.events)
        self.has_tray = self.tray.start()
        self.checked = {"game": 0.0, "games": 0.0, "update": -UPDATE_EVERY}   # the first check at once
        self.closed = False              # quit() ran: the window is destroyed
        self.reload_told = False         # the /reload hint of this companion's run shown

        root.protocol("WM_DELETE_WINDOW", self.close)
        self.say(f"Barrens Voice {display_version(self.version)}" + (", ждёт игру в трее" if resident else ""))
        self.config_error = None         # config.json that does not read (check_config): no voice-over until it does
        self.check_config(time.monotonic())
        if INSTALL_DIR is not None:                # an update by an older version did not bring the starter yet,
            from ..install import place_starter, repair_current    # nor set the version Windows «Apps» shows
            from ..paths import CODE_DIR
            from ..update import note_version
            if repair_current(INSTALL_DIR, CODE_DIR.parent):
                self.say(f"переход на другую версию в прошлый раз прервался — снова указал текущей "
                         f"{display_version(self.version)}")
            place_starter(INSTALL_DIR, CODE_DIR.parent)
            note_version(INSTALL_DIR, self.version)
            root.after(PRUNE_AFTER_MS, self.prune_versions)
        if self.addon_missing():                   # said at once, not only when the companion would start
            self.say(f"в {GAME_NAMES[self.selected]} нет аддона — «установить аддон» справа", "WARN")
        self.redraw()
        if resident and self.has_tray:
            root.withdraw()
        else:
            from .. import launch
            if sys.platform == "win32":
                self.running_game = launch.running_game()[0]
            self.start_on_open()
            if self.config.get("launch_wow") and self.companion.running and not self.running_game:
                self.hide(told="Лаунчер запускает игру и ждёт её в трее — щёлкни значок, чтобы открыть окно.")
        if args["open"]:                           # --open settings|help: straight to that window
            root.after(300, {"settings": self.open_settings, "help": self.open_help, "log": self.open_log}.get(args["open"], lambda: None))
        root.after(TICK_MS, self.tick)

    def prune_versions(self) -> None:
        """Old versions removed on a thread of their own (update.prune_unwatched): not only at a settle, whose
        prune the launcher closing with the game cut short."""
        if not self.closed and INSTALL_DIR is not None:
            from ..update import prune_unwatched
            prune_unwatched(INSTALL_DIR)

    # --- the companion --------------------------------------------------------------------------

    def start_on_open(self) -> None:
        """The companion (and the race voices in video memory) only with the game: started at the window's
        start when the game runs or the launcher starts it («Запускать игру вместе с лаунчером», opened by hand);
        else when it appears (tick) or on «Играть». Not the game after an update's restart (--updated): the player
        did not ask to play, and may just have closed it."""
        updated = self.args.get("updated")
        if self.running_game:
            self.start_companion(["--no-launch"] if updated else None)
        elif self.config.get("launch_wow") and not updated and not self.resident:
            self.start_companion()             # by hand only: the start with Windows never launches the game
        else:
            self.say("ждёт игру: «Играть» или запуск из Battle.net")

    def check_config(self, now: float, error="read") -> None:
        """Whether config.json reads; checked at start, every CONFIG_EVERY s and before the voice-over starts. A new
        problem is said once, an ERROR in the main window (the player's words: the line and column, what to do),
        its parser's words a DEBUG line under it; the file fixed is said too. While it does not read the voice-over
        does not start (start_companion) and the state line says so (snapshot)."""
        self.checked["config"] = now
        error = model.config_error() if error == "read" else error
        old = getattr(self, "config_error", None)
        self.config_error = error
        if error is not None and (old is None or str(old) != str(error)):
            self.config_told.add("broken")              # set_config: no second word of it
            self.say_config_error(error)
        elif error is None and old is not None:
            self.config_told.discard("broken")
            self.say("config.json снова читается")

    def say_config_error(self, error) -> None:
        """The ERROR line of a config.json that does not read, what to do in it; the parser's words a DEBUG line."""
        if isinstance(error, model.ConfigNotOpened):
            what = "закрой программу, которая его держит, и проверь права на файл"
        else:
            what = "исправь его («Открыть config.json» в «Настройках»)"
        self.say(f"{error} — {what}, до тех пор лаунчер в него не пишет и озвучку не запускает", "ERROR")
        if error.detail:
            self.say(f"    {error.detail}", "DEBUG")

    def set_config(self, **changes) -> dict:
        """Settings changed in config.json; when the file does not parse it is not written over (the player's
        settings are in it), and when it cannot be written (read-only, held by another program) it stays as it
        was - the change holds in this window (and in the companion it starts: start_companion) and is written with
        the next change the file takes (`pending`), not lost to it. Each of the two is said once (again only after a
        write that worked): a file that broke after one that could not be written is said too."""
        try:
            return self.write_config(**changes)
        except model.BrokenConfig as e:
            self.pending = changes = {**self.pending, **changes}
            self.config = {**self.config, **changes}
            kind = "unwritten" if isinstance(e, model.ConfigNotWritten) else "broken"
            if kind not in self.config_told:
                self.config_told.add(kind)
                if kind == "unwritten":
                    self.say(f"{e} — изменения действуют, а в файл запишутся, когда его снова можно будет изменить", "WARN")
                else:
                    self.say(f"{e} — изменения действуют, а в файл запишутся, когда он будет исправлен", "WARN")
            return self.config

    def write_config(self, **changes) -> dict:
        """config.json with `changes` and the ones it did not take before (`pending`); BrokenConfig as
        model.update_config raises it, nothing changed then."""
        self.config = model.update_config(**{**self.pending, **changes})
        self.pending = {}
        self.config_told.clear()
        return self.config

    def say(self, message: str, level: str = "INFO") -> None:
        """A line of the launcher's own in the logs (and its log file)."""
        self.incoming.put(f"{datetime.now():%H:%M:%S} {level:<5} [лаунчер] {message}")
        launcher_log(message)

    def say_as_companion(self, message: str) -> None:
        """A line of the companion's code run in the launcher (launch.launch at «Играть»), with the level the
        companion's own log gives it: «    Battle.net: --exec=launch WoWF» is a detail for the log window,
        not a line of the main one."""
        self.say(message, level_of(message))

    def game(self, key: str | None):
        return next((g for g in self.games if g.key == key), None)

    def addon_missing(self) -> bool:
        g = self.game(self.selected)
        return g is not None and g.addon is None

    def start_companion(self, args: list[str] | None = None) -> bool:
        if self.companion.running or self.busy:
            return False
        if self.selected is None:
            self.say("игра не найдена: укажи её папку в «Настройках»", "WARN")
            return False
        if self.addon_missing():
            self.say(f"в {GAME_NAMES[self.selected]} нет аддона — «установить аддон» справа", "WARN")
            return False
        try:
            # config.json as it is now - edited by hand while the window was open (it may sit in the tray for days) -
            # with what it did not take over it: the launch choice goes by this, not by the window's copy from its start
            self.config = {**model.read_config(strict=True), **self.pending}
        except model.BrokenConfig as e:
            # unreadable: no voice-over (it would end at once on the same file, with the same error a second time)
            self.check_config(time.monotonic(), e)
            return False
        if getattr(self, "config_error", None) is not None:
            self.check_config(time.monotonic(), None)      # fixed since: said
        if self.config.get("game") != self.selected:
            self.set_config(game=self.selected)
        self.companion_game = self.selected
        self.companion_started = time.monotonic()
        self.game_seen = False          # a game while this voice-over runs: it ends some 10 s after that game
        self.settle_due = True
        self.exit_seen = False
        self.reload_told = False
        flags = list(args or [])
        launch_flags = {"--launch", "--no-launch"}
        extra = [a for a in self.extra if not (launch_flags & set(flags) and a in launch_flags)]
        # the game and «Запускать игру вместе с лаунчером» as the window has them: config.json, which the
        # companion reads, may not have taken them (set_config)
        if not launch_flags & set(extra + flags):
            flags.append("--launch" if self.config.get("launch_wow") else "--no-launch")
        self.companion.start([f"--{self.selected}", *extra, *flags])
        if "--launch" in extra + flags:
            self.launching = ("asked", time.monotonic())
        self.say(f"озвучка запускается ({GAME_NAMES[self.selected]})")
        return True

    def stop_companion(self, then=None, why="stopping") -> None:
        """Stop the companion on a thread (it takes up to a few seconds), then `then` on the window's."""
        if not self.companion.running:
            if then:
                then()
            return
        self.busy = why

        def work():
            self.companion.stop()
            self.events.put(("stopped", then))
        threading.Thread(target=work, daemon=True, name="companion-stop").start()

    def restart_companion(self, args: list[str] | None = None) -> None:
        self.say("перезапускаю озвучку")
        self.stop_companion(lambda: self.start_companion(args), why="restarting")

    def settle_running(self, now: float) -> None:
        """A freshly applied version is settled while its companion runs, once the run is past update.EARLY: a
        companion only ever stopped by the launcher (the window or voice-over closed first, «Выход») never ended by itself, so its version stayed watched for good - nothing pruned, and two
        failed starts months later still went back. Once per run; the run's own end then finds nothing watched
        and counts as nothing."""
        if self.settle_due and INSTALL_DIR is not None and self.companion.running:
            from .. import update
            if now - self.companion_started > update.EARLY:
                self.settle_due = False
                update.record_start(INSTALL_DIR, now - self.companion_started, False)

    def companion_ended(self, code: int | None) -> None:
        self.exit_seen = True
        self.last_code = code
        if INSTALL_DIR is not None and self.companion_started:
            from .. import update
            back = update.record_start(INSTALL_DIR, time.monotonic() - self.companion_started, self.start_failed(code),
                                       say=lambda message: self.say(message, "ERROR"))
            if back:
                self.say(f"версия {self.version} не запускается — вернулся на {back}", "ERROR")
                self.tray.notify(f"Новая версия лаунчера не запустилась, вернулся на {back}. "
                                 "Подробности — в журнале.")
                self.relaunch()
                return
        g = self.game(self.companion_game)
        if g is not None and g.addon is not None:      # killed or crashed hard: it could not say so itself
            from ..status import write
            write(g.addon_dir, stopped=True)
        if code not in (0, None, Companion.RESTART) and self.running_game:
            self.tray.notify("Озвучка чата остановилась (подробности — в журнале). «Включить озвучку» в окне "
                             "запустит её снова, а скрытый чат в игре вернёт /reload.")
        state = self.companion.state()
        if Companion.wants_restart(code):
            self.say("озвучке нужна сеть для моделей — запускаю снова")
            self.start_companion()
            return
        if state.get("phase") == "game_closed":       # the voice-over ends with the game; the launcher by its check
            if self.closes_with_game():
                self.say("игра закрыта — закрываюсь")
                if self.updater is not None and self.updater.due(False, self.companion.running, bool(self.busy)):
                    self.updater.apply()             # no restart: the next start opens the new version
                self.quit()
            return
        if code not in (0, None):
            self.say(f"озвучка остановилась (код {code}) — подробности в «Журнале»", "ERROR")

    def closes_with_game(self) -> bool:
        """«Закрываться вместе с игрой»: the launcher ends after the game - unless it starts with Windows, then it
        always stays to wait for the next game in the tray (the check is grey)."""
        return bool(self.config.get("exit_with_wow")) and not self.autostart

    def follow_launch(self, new: list[tuple[str, str, str]], now: float) -> None:
        """«Игра запускается…» from the launch's own lines, whichever started it («Играть», the shortcut, the
        window's start with «Запускать игру вместе с лаунчером»): on at Battle.net asked, off when the launch gave
        up, when the companion that was to launch it ended first, or LAUNCH_WAIT after Battle.net was asked
        with no game (the player closed its login window, say). The game seen ends it too (_tick)."""
        from .. import launch
        for _, _, message in new:
            message = message.removeprefix("[лаунчер] ")      # the launcher's own launch (play) says them too
            if message.startswith(launch.LAUNCHING):
                self.launching = ("sent", now)
            elif message.startswith(launch.NOT_LAUNCHED):
                self.launching = None
        if self.launching is None:
            return
        kind, since = self.launching
        if (kind == "asked" and not self.companion.running and not self.busy) or \
                (kind == "sent" and now - since > LAUNCH_WAIT):
            self.launching = None

    def start_failed(self, code: int | None) -> bool | None:
        """How the companion's end counts for a freshly applied version (update.record_start): True - an error,
        the version's failed start; False - a good end (0); None - no verdict: 3 (started again online), 4
        (another companion runs), 5 (config.json cannot be read) or an error this very run explained itself
        (state.json's phase "error": no addon, say). Those say nothing of the version either way: going back
        would not help, and taking them for a good run settled a version that had not started yet.
        state.json is this run's: Companion.start deletes it before every start (its pid is not compared - in a
        venv on Windows python.exe may start the interpreter as a child, so Popen's pid is not the one written).
        An explained error is no verdict, not a good run: a version that "explains" every start is never settled
        or pruned, and its first crash still counts."""
        if code == 0:
            return False
        if code in (None, Companion.RESTART, Companion.ALREADY, Companion.CONFIG_ERROR):
            return None
        if self.companion.state().get("phase") == "error":
            return None
        return True

    # --- actions --------------------------------------------------------------------------------

    def play(self) -> None:
        if self.busy or (getattr(self, "launching", None) and not self.running_game):   # «Игра запускается…»
            return
        if self.running_game:                      # the game runs, the companion not (it stopped): voices back on
            if not self.companion.running and not self.start_companion(["--no-launch"]) \
                    and getattr(self, "config_error", None) is not None:
                self.say_config_error(self.config_error)   # a click asked for it: said again
            return
        g = self.game(self.selected)
        if g is None:
            self.say("игра не найдена: укажи её папку в «Настройках»", "WARN")
            return
        if g.addon is None and not self.install_addon(g, ask=True):
            return
        if self.companion.running and self.companion_game == self.selected:
            from .. import launch
            if launch.launch(dict(self.config, game=self.selected), self.say_as_companion):
                self.launching = ("sent", time.monotonic())
        elif self.companion.running:
            self.restart_companion(["--launch"])
        elif not self.start_companion(["--launch"]) and getattr(self, "config_error", None) is not None:
            self.say_config_error(self.config_error)   # a click asked for it: said again, not only the red state line

    def choose_game(self, key: str) -> None:
        if key == self.selected:
            return
        self.selected = key
        self.set_config(game=key)
        if self.addon_missing():
            self.say(f"в {GAME_NAMES[key]} нет аддона — «установить аддон» справа", "WARN")
        if self.companion.running and self.companion_game != key:
            self.restart_companion(["--no-launch"])
        self.redraw()

    def update_addons(self) -> None:
        """An installed Barrens Voice keeps the games' addon at its own version: an older one is replaced
        while that game is closed (a newer one, from CurseForge say, stays). Not from the source folder."""
        if self.updater is None or not self.config.get("auto_update", True):   # off: «обновить аддон» by hand
            return
        for g in model.addons_to_update(self.games, self.addon_version, self.running_game):
            if (g.key, g.addon) not in self.addons_tried:      # a failed one is said once, not every 15 s
                self.addons_tried.add((g.key, g.addon))
                self.install_addon(g)

    def install_addon(self, g, ask=False) -> bool:
        source = model.addon_source()
        if source is None:
            self.say("аддона нет рядом с лаунчером — переустанови его", "WARN")
            return False
        if ask and not messagebox.askyesno("Barrens Voice Launcher", f"В {g.name} нет аддона Barrens Voice. Установить его?",
                                           parent=self.root):
            return False
        try:
            model.install_addon(source, g.root)
        except OSError as e:
            self.say(f"аддон не установлен: {e}", "WARN")
            return False
        done = (f"аддон обновлён до {self.addon_version} в {g.name}" if g.addon not in (None, self.addon_version)
                else f"аддон {self.addon_version} установлен в {g.name}")
        self.say(done + (" — в игре нужен /reload" if self.running_game == g.key else ""))
        self.games = model.find_games(self.config)
        self.redraw()
        return True

    def toggle(self, key: str) -> None:
        if key == "autostart":
            try:
                model.set_autostart(not self.autostart)
                self.autostart = model.autostart()
            except OSError as e:
                self.say(f"не удалось изменить автозапуск: {e}", "WARN")
            if self.autostart and self.config.get("exit_with_wow"):    # it stays in the tray: the check comes off
                self.set_config(exit_with_wow=False)
        else:
            self.set_config(**{key: not self.config.get(key)})
            if self.companion.running and key != "exit_with_wow":     # that one is the launcher's own
                self.say("изменение подействует при следующем запуске озвучки")
        self.redraw()

    def open_log(self) -> None:
        """The full log in a window of its own, live (logwindow.py)."""
        if self.log_window is not None:
            self.log_window.lift()
            return
        from .logwindow import LogWindow
        self.log_window = LogWindow(self.root, self.lines, self.scale, DATA_DIR / "logs",
                                    on_close=lambda: setattr(self, "log_window", None), header=self.report_header)

    def report_header(self) -> str:
        return model.report_header(self.version, self.companion.state(), model.read_config(), self.games,
                                   self.running_game, running=self.companion.running)

    def dismiss_update(self) -> None:
        if self.update:
            self.set_config(launcher_dismissed=self.update["version"])
        self.redraw()

    def close(self) -> None:
        """The window's cross: Barrens Voice ends, the companion with it (waiting in the tray is for the
        start with Windows and for the game started automatically)."""
        if self.resident and self.has_tray:
            self.hide()
            return
        if (self.running_game and self.companion.running and not messagebox.askyesno(
                "Barrens Voice Launcher", "Игра запущена — озвучка чата остановится. Закрыть Barrens Voice Launcher?", parent=self.root)):
            return
        self.quit()

    def hide(self, told: str = "") -> None:
        if not self.has_tray:
            self.quit()
            return
        self.root.withdraw()
        if told or not self.config.get("launcher_tray_told"):
            self.tray.notify(told or "Barrens Voice Launcher работает в трее: щёлкни значок, чтобы открыть окно. "
                                     "Выйти — правой кнопкой по значку.")
            if not self.config.get("launcher_tray_told"):
                self.set_config(launcher_tray_told=True)

    def second_start(self, path: Path) -> None:
        """The launcher started again while this one runs: the window shown; started by hand (a shortcut, not the
        start with Windows: "play" in the file) with «Запускать игру вместе с лаунчером» on, the game launched too,
        as a first start by hand would - unless a game or the voice-over is already on its way."""
        try:
            asked = path.read_text(encoding="utf-8").strip()
        except OSError:
            asked = ""
        path.unlink(missing_ok=True)
        self.show()
        if (asked == "play" and self.config.get("launch_wow") and not self.running_game
                and not self.companion.running and not self.busy):
            self.play()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        self.closed = True               # quit may come from inside a tick: nothing is drawn after it
        self.root.withdraw()
        self.tray.stop()
        if self.companion.running:
            self.companion.stop()
        self.root.destroy()

    # --- the loop -------------------------------------------------------------------------------

    def tick(self) -> None:
        if self.closed:
            return
        try:
            self._tick()
        except Exception as e:  # noqa: BLE001 - the window must live on; the error goes to the file
            if self.closed:              # the window went away during this tick: nothing to report
                return
            import traceback
            launcher_log("ошибка: " + "".join(traceback.format_exception(e)))
        if not self.closed:
            self.root.after(TICK_MS, self.tick)

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self.checked.get("config", 0.0) > CONFIG_EVERY:
            self.check_config(now)                 # fixed or broken by hand while the window is open
        new = []
        while not self.incoming.empty():
            new.append(parse(self.incoming.get_nowait()))
        self.lines.extend(new)
        self.main.extend(item for item in new if item[1] in model.MAIN_LEVELS)
        if new and self.log_window is not None:
            self.log_window.append(new)
        self.follow_launch(new, now)
        while not self.events.empty():
            event = self.events.get_nowait()
            if isinstance(event, tuple) and event[0] == "stopped":
                self.busy = ""
                self.exit_seen = True
                if event[1]:
                    event[1]()
            elif event == "show":
                self.show()
            elif event == "play":
                self.play()
            elif event == "quit":
                self.quit()
                return
        if SHOW_FILE.exists():
            self.second_start(SHOW_FILE)
        if now - self.checked["game"] > 2:
            self.checked["game"] = now
            from .. import launch
            self.running_game, folder = launch.running_game() if sys.platform == "win32" else (None, None)
            changes = model.remember(self.config, self.running_game, folder) if folder else None
            if changes:                                # where the game is, for next time and the companion
                self.set_config(**changes)
                self.games = model.find_games(self.config)
            if self.running_game and self.running_game != self.selected and not self.companion.running:
                self.selected = self.running_game      # the game the player started: that one
            if self.running_game:
                self.launching = None                  # it came
        if now - self.checked["games"] > 15:
            self.checked["games"] = now
            self.games = model.find_games(self.config)
            self.update_addons()
        self.settle_running(now)
        if not self.exit_seen and not self.busy and not self.companion.running:
            self.companion_ended(self.companion.poll())   # may close the launcher with the game, or go back a version
            if self.closed:
                return
        self.apply_update_when_idle()
        if self.closed:
            return
        if now - self.checked["update"] > UPDATE_EVERY:
            self.checked["update"] = now
            threading.Thread(target=self._check_update, daemon=True, name="update-check").start()
        if self.companion.running and not self.reload_told and self.companion.state().get("reload_hint"):
            self.reload_told = True          # started after the player entered the world: say it outside the game too
            self.tray.notify("Озвучка включена. Если окно чата не прячется — сделай в игре /reload.")
        if self.autostart_due():                 # the game started from Battle.net
            self.selected = self.running_game
            self.start_companion(["--no-launch"])
        self.redraw()

    def autostart_due(self) -> bool:
        """The game started from Battle.net while the companion is not running (waiting in the tray, or after the
        last game was closed): start it for that game - unless it ended with an error, or config.json does not
        read. That one is not read again here, every tick: check_config looks every CONFIG_EVERY s (a file that
        cannot be opened waits 0.1 s for its second try, in the window's thread) and clears it when it reads."""
        return bool(self.running_game and not self.companion.running and not self.busy and self.exit_seen
                    and self.last_code in (0, None) and getattr(self, "config_error", None) is None
                    and self.game(self.running_game) and not self.addon_missing())

    def _check_update(self) -> None:
        """On its own thread: an install checks (and prepares) through its Updater; from the source - only tell."""
        if self.updater is None:
            found = model.latest_release()
            if found and model.newer(found["version"], self.version):
                self.update = found
                launcher_log(f"вышла версия {found['version']}")
            return
        if self.updater.check():
            self.update = {"version": self.updater.found["version"], "url": self.updater.url(),
                           "addon": self.updater.found.get("addon")}

    def start_update(self) -> None:
        """«Обновить»: prepare the new version now (installs); from the source - its release page."""
        if self.updater is None or not self.updater.found:
            webbrowser.open(self.update["url"] if self.update else f"https://github.com/{model.REPO}/releases")
            return
        threading.Thread(target=self.updater.prepare, daemon=True, name="update").start()

    def install_update_now(self) -> None:
        """«Установить сейчас»: the prepared version at once, the game or not - the companion stops, the launcher
        restarts into the new version and starts it again for a running game."""
        if self.updater is None or not self.updater.ready or self.busy:
            return
        if self.running_game and not messagebox.askyesno(
                "Barrens Voice Launcher", "Установить новую версию сейчас? Озвучка прервётся примерно на полминуты.", parent=self.root):
            return

        def switch():
            if self.updater.apply():                # failed: the version stays ready, the companion comes back (tick)
                self.relaunch(updated=True)
        self.stop_companion(switch, why="stopping")

    def apply_update_when_idle(self) -> None:
        """A prepared version becomes current while no game and no companion run; the launcher restarts into it.
        Only after the companion's end is handled (companion_ended): closing with the game comes first."""
        if (self.updater is not None and self.exit_seen
                and self.updater.due(bool(self.running_game), self.companion.running, bool(self.busy))):
            if self.updater.apply():
                self.relaunch(updated=True)
            return
        ready = getattr(self.updater, "ready", None)
        # what it waits for, once per version and cause, and only when it does wait (no game, no voice-over: switched
        # at once). The voice-over ends by itself some 10 s after a game it saw (bv/__main__.py GameWatch), just
        # before the switch: no word then - but one that has seen no game yet waits for one, and the switch with it
        if self.running_game:
            self.game_seen = True
        voice_waits = self.companion.running and not getattr(self, "game_seen", False)
        cause = "game" if self.running_game else "voice" if voice_waits else None
        if ready and cause and (ready, cause) != getattr(self, "wait_told", None):
            self.wait_told = (ready, cause)
            name = display_version(ready)
            self.say(f"версия {name} установится, когда закроешь игру" if cause == "game"
                     else f"версия {name} установится, когда остановится озвучка")

    def relaunch(self, updated: bool = False) -> None:
        """Start the launcher anew through the install's starter (a moment after this one has gone); end this one.
        `updated`: into the version just applied, which then does not start the game (--updated)."""
        import subprocess

        from ..update import relaunch_command
        subprocess.Popen(relaunch_command(starter(), self.resident, updated),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.quit()

    # --- drawing --------------------------------------------------------------------------------

    def snapshot(self):
        state = self.companion.state()
        lines = model.describe(state, self.companion.running, self.companion.poll(),
                               GAME_NAMES.get(self.running_game) if self.running_game else None,
                               self.addon_missing() and not self.companion.running)
        if self.busy:
            lines[0] = (model.YELLOW, "Озвучка перезапускается…" if self.busy == "restarting" else "Озвучка останавливается…")
        elif getattr(self, "config_error", None) is not None and not self.companion.running:
            # the tray's hint says it too
            lines[0] = (model.RED, "config.json не открывается — его держит другая программа"
                        if isinstance(self.config_error, model.ConfigNotOpened)
                        else "config.json не читается — открой «Настройки»")
        show_update = bool(self.update) and self.config.get("launcher_dismissed") != self.update["version"]
        games = tuple((g.key, g.addon, str(g.root)) for g in self.games)
        progress = model.main_lines(list(self.main), keep=self.main.maxlen)
        return (tuple(lines), progress, games, self.selected, self.running_game, show_update,
                self.updater.status if self.updater else "",
                self.companion.running,
                self.config.get("launch_wow"), self.config.get("exit_with_wow"), self.autostart,
                self.launching is not None, self.log_back)

    def draw_log_bar(self) -> None:
        """The log's scroll bar: a thin track, its thumb the rows shown - drawn only when the rows do not all fit,
        and seen only while the pointer is over the log (or the thumb is held)."""
        c = self.canvas
        c.delete("logbar")
        if self.log_box is None:
            return
        _, _, _, _, (x0, y0, x1, y1), total, fits, first = self.log_box
        thumb = model.log_thumb(total, fits, first, y0, y1)
        if thumb is None:
            return
        state = "normal" if self.log_hover or self.log_drag else "hidden"
        self.p.rounded(x0, y0, x1, y1, r=3, fill="#2a1d10", outline="", tags=("logbar",), state=state)
        self.p.rounded(x0, thumb[0], x1, thumb[1], r=3, fill="#8a6420" if not self.log_drag else "#c9a24a",
                       outline="", tags=("logbar", "logthumb"), state=state)

    def in_log(self, event) -> bool:
        if self.log_box is None:
            return False
        x, y = event.x / self.scale, event.y / self.scale
        x0, y0, x1, y1 = self.log_box[:4]
        return x0 <= x <= x1 and y0 <= y <= y1

    def log_pointer(self, event) -> None:
        """The pointer over the log shows its scroll bar, off it hides it; a press on the bar takes the thumb
        (or pages to where it was pressed)."""
        hover = str(event.type) != "Leave" and self.in_log(event)
        if hover != self.log_hover:
            self.log_hover = hover
            self.canvas.itemconfigure("logbar", state="normal" if hover or self.log_drag else "hidden")

    def log_scroll_to(self, back: int) -> None:
        if self.log_box is not None:
            total, fits = self.log_box[5], self.log_box[6]
            back = model.log_view(total, fits, back)[1]
        if back != self.log_back:
            self.log_back = back
            self.redraw()

    def log_wheel(self, event) -> None:
        if not self.in_log(event):
            return
        if getattr(event, "num", None) in (4, 5):             # X11
            notches = 1 if event.num == 4 else -1
        else:                                                 # Windows: 120 a notch; macOS: a few, either way
            notches = event.delta / 120 if abs(event.delta) >= 120 else (1 if event.delta > 0 else -1)
        self.log_scroll_to(self.log_back + round(notches * 3))

    def log_pressed(self, event) -> None:
        """A press on the bar: on the thumb it is taken, beside it the log moves a page that way."""
        if self.log_box is None:
            return
        _, _, _, _, (x0, y0, x1, y1), total, fits, first = self.log_box
        thumb = model.log_thumb(total, fits, first, y0, y1)
        if thumb is None:
            return
        y = event.y / self.scale
        if thumb[0] <= y <= thumb[1]:
            self.log_drag = (y, thumb[0])
            self.draw_log_bar()
        else:
            self.log_scroll_to(self.log_back + (fits - 1 if y < thumb[0] else 1 - fits))

    def log_dragged(self, event) -> None:
        if self.log_drag is None or self.log_box is None:
            return
        _, _, _, _, (_, y0, _, y1), total, fits, _ = self.log_box
        taken, top = self.log_drag
        self.log_scroll_to(model.log_back_at(total, fits, top + event.y / self.scale - taken, y0, y1))

    def log_released(self, event) -> None:
        if self.log_drag is not None:
            self.log_drag = None
            self.log_hover = self.in_log(event)
            self.draw_log_bar()

    def redraw(self) -> None:
        if self.closed:
            return
        snap = self.snapshot()
        if snap == self.drawn:
            return
        self.drawn = snap
        lines, log, _, _, _, show_update, _, _, launch_wow, exit_with_wow, autostart, _, _ = snap
        c, p = self.canvas, self.p
        c.delete("all")
        n = max(1, len(self.games))
        row2 = 36 + n * 56 + 40                    # Game: its cards and the note; the log as tall
        y2 = TOP + ROW1 + GAP
        y3 = y2 + row2 + GAP
        bottom = y3 + 52 + (GAP + 48 if show_update else 0)
        h = bottom + 30
        c.config(height=h)
        p.background(W, h)
        p.title(W, "Barrens Voice Launcher", "озвучка чата голосами рас")

        # row 1: State | Start
        p.panel(LX0, TOP, LX1, TOP + ROW1, "Состояние")
        for i, (color, text) in enumerate(lines):
            yy = TOP + 48 + i * 24
            c.create_oval(LX0 + 16, yy - 5, LX0 + 26, yy + 5, fill=color, outline="")
            c.create_text(LX0 + 36, yy, text=p.fit(text, p.body, LX1 - LX0 - 50), anchor="w", fill=PARCHMENT, font=p.body)
        p.panel(RX0, TOP, RX1, TOP + ROW1, "Запуск")
        for i, (key, text, on) in enumerate((("launch_wow", "Запускать игру вместе с лаунчером", launch_wow),
                                             ("exit_with_wow", "Закрываться вместе с игрой", exit_with_wow),
                                             ("autostart", "Запускать при входе в Windows", autostart))):
            grey = key == "exit_with_wow" and bool(autostart)      # the start with Windows always stays in the tray
            p.check(RX0 + 14, TOP + 48 + i * 24, text, bool(on) and not grey, f"check{i}", lambda k=key: self.toggle(k),
                    tip=TIPS["exit_with_wow_off" if grey else key], enabled=not grey)

        # row 2: the log | Game
        p.panel(LX0, y2, LX1, y2 + row2, "Журнал")
        fits = (row2 - 44 - 6) // 18 + 1
        # every line wrapped, messages are written to fit one line (~58 characters after the time), and one that
        # does not is wrapped under its time rather than cut. The text stops LOG_GUTTER short of the scroll bar's
        # track: the bar, shown over the log while the pointer is on it, never covers a letter
        wrapped, counts, rows = {}, [], []
        for item in log:
            time, level, message = item             # the level itself is not shown here
            got = wrapped.get(item) or self.log_wrapped.get(item)
            if got is None:
                got = [(level, part) for part in p.wrap(f"{time} {message}", p.mono, LX1 - LX0 - 28 - LOG_GUTTER,
                                                        " " * (len(time) + 1))]
            wrapped[item] = got
            counts.append(len(got))
            rows += got
        self.log_wrapped = wrapped
        if self.log_back:      # scrolled up: the rows that came below do not move the view (nor the oldest gone above)
            self.log_back += model.log_rows_after(self.log_last, log, counts)
        self.log_last = log[-1] if log else None
        first, self.log_back = model.log_view(len(rows), fits, self.log_back)
        self.drawn = snap[:-1] + (self.log_back,)
        for i, (level, text) in enumerate(rows[first:first + fits]):
            c.create_text(LX0 + 14, y2 + 44 + i * 18, text=text, anchor="w", fill=MAIN_COLORS.get(level, "#c9b896"),
                          font=p.mono)
        track = (LX1 - 14, y2 + 36, LX1 - 8, y2 + row2 - 12)     # inside the frame, clear of its round corners
        self.log_box = (LX0, y2 + 30, LX1, y2 + row2, track, len(rows), fits, first)
        self.draw_log_bar()
        p.panel(RX0, y2, RX1, y2 + row2, "Игра")
        if not self.games:
            c.create_text(RX0 + 14, y2 + 40, anchor="nw", width=RX1 - RX0 - 28, fill=PARCHMENT, font=p.body,
                          text="WoW Forever и WoW Retail не найдены. Укажи папку игры в «Настройках».")
        for i, g in enumerate(self.games):
            y0 = y2 + 36 + i * 56
            on = g.key == self.selected
            tag = f"game{i}"
            p.rounded(RX0 + 14, y0, RX1 - 14, y0 + 48, r=7, fill="#46300f" if on else "#281a0e",
                      outline=GOLD if on else "#5a4220", width=2 if on else 1, tags=(tag, tag + "box"))
            if not on:
                p.hover(tag, lambda t=tag: c.itemconfig(t + "box", outline="#c9a24a"),
                        lambda t=tag: c.itemconfig(t + "box", outline="#5a4220"))
            title = g.name + ("  •  запущена" if g.key == self.running_game else "")
            c.create_text(RX0 + 26, y0 + 15, text=title, anchor="w", fill=PARCHMENT, font=p.body, tags=tag)
            p.click(tag, lambda k=g.key: self.choose_game(k))
            # the addon, right of the title: short words, the versions in the hint
            if g.addon is None:
                p.link(RX1 - 26, y0 + 15, "установить аддон", f"inst{i}", lambda g=g: self.install_addon(g), anchor="e",
                       tip=TIPS["install"])
            elif model.newer(self.addon_version, g.addon):
                p.link(RX1 - 26, y0 + 15, "обновить аддон", f"inst{i}", lambda g=g: self.install_addon(g), anchor="e",
                       tip=f"Аддон {g.addon} → {self.addon_version}.\n\n" + TIPS["update"])
            else:
                c.create_text(RX1 - 26, y0 + 15, text=f"аддон {g.addon}", anchor="e", fill=DIM, font=p.small, tags=tag)
            c.create_text(RX0 + 26, y0 + 34, text=p.fit(str(g.root), p.small, RX1 - RX0 - 52), anchor="w", fill=DIM,
                          font=p.small, tags=tag)
        c.create_text(RX0 + 14, y2 + 36 + n * 56, anchor="nw", width=RX1 - RX0 - 28, fill=DIM, font=p.small,
                      text="Запускаешь игру прямо из Battle.net — лаунчер подхватит её сам.")

        # row 3: the buttons under the log | Play under Game
        bw = (LX1 - LX0 - 2 * 10) / 3
        for i, (text, action) in enumerate((("Журнал", self.open_log), ("Настройки", self.open_settings),
                                            ("Помощь", self.open_help))):
            x0 = LX0 + i * (bw + 10)
            p.button(x0, y3 + 8, x0 + bw, y3 + 44, text, f"small{i}", action)
        playing, voiced = bool(self.running_game), self.companion.running
        starting = not playing and self.launching is not None
        label = ("Игра запускается…" if starting else "Играть" if not playing else "Игра запущена" if voiced
                 else "Включить озвучку")
        p.big_button(RX0, y3, RX1, y3 + 52, label, "play", self.play,
                     enabled=not self.busy and not starting and not (playing and voiced))

        # the update, across the whole window
        if show_update:
            y = y3 + 52 + GAP
            u = self.update
            p.rounded(22, y, W - 22, y + 48, r=6, fill="#3c2d0a", outline="#b8862b")
            title = f"Вышла версия {display_version(u['version'])}"
            if u.get("addon") and model.newer(u["addon"], self.addon_version):     # the addon changed too
                title += f" и аддон {u['addon']}"
            # what the update does or waits for: a second line under the title; the buttons on the right
            status = self.updater.status if self.updater else ""
            doing = {"downloading": "обновление скачивается…", "preparing": "обновление готовится…",
                     "ready": "обновление ждёт закрытия игры" if self.running_game else "обновление ждёт остановки озвучки"
                     }.get(status)
            c.create_text(36, y + (16 if doing else 24), text=title, anchor="w", fill=GOLD, font=(self.title_font, -18))
            if doing:
                c.create_text(36, y + 35, text=doing, anchor="w", fill=PARCHMENT, font=p.small)
            if status == "ready":                 # or now, by the player
                p.button(W - 316, y + 9, W - 166, y + 39, "Установить сейчас", "update_now", self.install_update_now,
                         tip=TIPS["update_now"])
            elif not doing:
                p.button(W - 246, y + 9, W - 166, y + 39, "Обновить", "update", self.start_update)
            p.button(W - 156, y + 9, W - 58, y + 39, "Что нового", "news", lambda: webbrowser.open(u["url"]))
            p.button(W - 50, y + 9, W - 30, y + 39, "×", "dismiss", self.dismiss_update)
        c.create_text(W - 24, h - 16, text=f"версия {display_version(self.version)}", anchor="e", fill=DIM, font=p.small)
        self.tray.tooltip("Barrens Voice Launcher — " + lines[0][1])

    # --- settings and help ----------------------------------------------------------------------

    def _child(self, title: str, width: int, height: int):
        win = tk.Toplevel(self.root)
        win.title(f"Barrens Voice Launcher — {title}")
        win.resizable(False, False)
        win.transient(self.root)
        if self.root.winfo_viewable():           # over the main window, not in the screen's corner
            win.geometry(f"+{self.root.winfo_rootx() + 60}+{self.root.winfo_rooty() + 40}")
        canvas = ScaledCanvas(win, self.scale, width=width, height=height, highlightthickness=0, bg="#0b0705")
        canvas.pack()
        p = Painter(canvas, self.title_font)
        p.background(width, height)
        p.title(width, title, size=30, y=36)
        return win, canvas, p

    def open_settings(self) -> None:
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        from ..omni_runtime import selected

        cfg = model.read_config()
        status = self.companion.state()
        provider = status.get("provider", "") if status.get("voices") == "ready" else ""
        omni = provider.startswith("OmniVoice") if provider else selected(cfg)
        width, height = 600, 696 if omni else 840
        win, c, p = self._child("Настройки", width, height)
        self.settings_win = win
        boxes = {}
        # a folder per game (game_dirs, where the launcher also remembers a game it saw running): two games
        # may well be on two disks; shown as found when not set
        found = {g.key: str(g.root) for g in self.games}
        dirs = cfg.get("game_dirs") or {}
        rows = (("dir_forever", "Папка WoW Forever (…\\_forever_), если её не нашли сами", "dir"),
                ("dir_retail", "Папка WoW Retail (…\\_retail_), если её не нашли сами", "dir"),
                ("battlenet_exe", "Battle.net.exe, если его не нашли сами", "exe"),
                ("hf_home", "Папка кэша моделей (пусто — по умолчанию)", "dir"),
                ("log_keep_days", "Сколько дней хранить журналы (0 — всегда)", ""))
        initial = {"dir_forever": dirs.get("forever") or found.get("forever", ""),
                   "dir_retail": dirs.get("retail") or found.get("retail", "")}
        y = 80
        for key, label, pick in rows:
            c.create_text(30, y, text=label, anchor="w", fill=PARCHMENT, font=p.body)
            boxes[key] = p.entry(30, y + 26, width - 60 - (100 if pick else 0), str(initial.get(key, cfg.get(key, ""))))
            if pick:
                def choose(k=key, kind=pick):
                    got = (filedialog.askdirectory(parent=win) if kind == "dir"
                           else filedialog.askopenfilename(parent=win, filetypes=[("Battle.net", "Battle.net.exe")]))
                    if got:
                        boxes[k].var.set(os.path.normpath(got))
                p.button(width - 120, y + 13, width - 30, y + 39, "Выбрать…", f"pick{key}", choose)
            y += 66
        profiles = {"quality": "Качество", "balanced": "Баланс"}
        if not omni:
            profiles["fastest"] = "Минимальная задержка"
        profile_key = "omnivoice_profile" if omni else "f5_profile"
        requested_profile = cfg.get(profile_key)
        profile = tk.StringVar(win, requested_profile if requested_profile in tuple(profiles) else "quality")
        c.create_text(30, y + 3, text="Профиль OmniVoice" if omni else "Профиль голосов рас",
                      anchor="w", fill=PARCHMENT, font=p.body)
        for x, (key, label) in zip((30, 170, 290), profiles.items()):
            choice = tk.Radiobutton(c, text=label, variable=profile, value=key, takefocus=True,
                                    bg="#211309", fg=PARCHMENT, selectcolor="#120a05",
                                    activebackground="#211309", activeforeground=GOLD, font=c.font(p.body),
                                    highlightthickness=0)
            c.create_window(x, y + 29, anchor="w", window=choice)
        y += 58
        flags = {"trt": bool(cfg.get("f5_tensorrt")), "details": bool(cfg.get("log_details")), "whisper": cfg.get("f5_end_check") == "whisper",
                 "auto": cfg.get("auto_update", True) is not False, "beta": cfg.get("update_channel") == "beta"}

        def draw_check(key, y, label):
            c.delete(key)
            p.check(30, y, label, flags[key], key,
                    lambda: (flags.__setitem__(key, not flags[key]), draw_check(key, y, label)))
        if not omni:
            draw_check("trt", y + 6, "TensorRT для NVIDIA в быстрых профилях (скачает 1,8 ГиБ)")
            y += 38
        draw_check("details", y + 6, "Подробный журнал: как строка написана и как прочитана")
        y += 38
        if not omni:
            draw_check("whisper", y + 6, "Проверять концовки фраз распознаванием речи (Whisper)")
            c.create_text(56, y + 26, anchor="nw", fill=DIM, font=p.small, width=width - 86,
                          text="Изредка голос расы не успевает договорить последний слог. Выключено: фраза, "
                               "договорённая впритык (примерно каждая восьмая), синтезируется ещё раз — на секунду "
                               "дольше. Включено: Whisper сначала слушает её конец и переделывает только при обрыве, "
                               "но занимает около 1,5 ГБ видеопамяти и пару секунд при загрузке голосов.")
            y += 106
        draw_check("auto", y + 6, "Обновлять автоматически")
        p.tips["auto"] = ("Новая версия скачивается и готовится сама и включается, когда игра и озвучка не запущены.\n\n"
                          "Выключено — внизу окна появится «Вышла версия…» с кнопкой «Обновить».")
        draw_check("beta", y + 34, "Получать тестовые версии")
        p.tips["beta"] = ("Тестовые версии выходят раньше и могут быть с ошибками — для тех, кто готов проверять "
                          "сборки и писать о найденном.\n\nВыключишь — останешься на тестовой, пока не выйдет "
                          "стабильная новее.")
        lead = "Остальное — в config.json:"
        c.create_text(30, y + 72, anchor="w", fill=DIM, font=p.small, text=lead)
        doc_tag = release_tag(self.version) if INSTALL_DIR is not None and self.version != "?" else "master"
        p.link(30 + p.measure(lead, p.small) + 12, y + 72, "описание настроек", "configdoc",
               lambda: webbrowser.open(f"https://github.com/{model.REPO}/blob/{doc_tag}/docs/config.md"))
        c.create_text(30, y + 92, anchor="w", fill=DIM, font=p.small,
                      text="Профиль OmniVoice применяется со следующей реплики." if omni
                      else "Если озвучка работает, после сохранения она перезапустится.")

        def edited():
            """The settings as the window has them now; None if the days are not a number."""
            try:
                days = int(boxes["log_keep_days"].var.get() or 0)
            except ValueError:
                return None
            changes = {k: boxes[k].var.get().strip() for k in ("battlenet_exe", "hf_home")}
            game_dirs = dict(cfg.get("game_dirs") or {})
            for box, game in (("dir_forever", "forever"), ("dir_retail", "retail")):
                folder = model.game_folder(boxes[box].var.get().strip(), game)
                if folder and game not in dirs and folder == os.path.normpath(found.get(game, "") or "."):
                    continue                     # the folder found, shown and left as it was: not a setting
                if folder:
                    game_dirs[game] = folder
                else:
                    game_dirs.pop(game, None)
            changes["game_dirs"] = game_dirs
            changes.update(log_keep_days=days, log_details=flags["details"],
                           auto_update=flags["auto"], update_channel="beta" if flags["beta"] else "stable")
            changes[profile_key] = profile.get()
            if not omni:
                changes["f5_tensorrt"] = flags["trt"]
                if flags["whisper"] != (cfg.get("f5_end_check") == "whisper"):
                    changes["f5_end_check"] = "whisper" if flags["whisper"] else "silence"
            return changes

        def differs(changes: dict) -> bool:
            """Whether `changes` differ from the file (cfg has the defaults under it): an empty field is the same
            as a key set to null."""
            return any((cfg.get(k) if cfg.get(k) is not None else "") != v for k, v in changes.items())

        def save():
            changes = edited()
            if changes is None:
                messagebox.showerror("Barrens Voice Launcher", "Дни хранения журналов — целое число.", parent=win)
                return
            missing = [GAME_NAMES[g] for g, d in changes["game_dirs"].items()
                       if d and not (Path(d) / "Wow.exe").is_file()]
            if missing:
                messagebox.showerror("Barrens Voice Launcher", f"Не нашёл Wow.exe в папке {', '.join(missing)}.", parent=win)
                return
            changed = differs(changes)
            before = {"auto_update": cfg.get("auto_update", True) is not False, "update_channel": cfg.get("update_channel")}
            try:
                self.write_config(**changes)
            except model.BrokenConfig as e:
                fix = ("Сними с него «Только чтение» или закрой программу, которая его держит"
                       if isinstance(e, model.ConfigNotWritten) else "Исправь его («Открыть config.json»)")
                messagebox.showerror("Barrens Voice Launcher", f"{e}\n\n{fix} и сохрани настройки снова.", parent=win)
                return
            self.games = model.find_games(self.config)
            win.destroy()
            if changed:
                self.say("настройки сохранены")
                self.update_settings_changed(before, changes)
                own = {k: v for k, v in changes.items() if k not in ("auto_update", "update_channel", "omnivoice_profile")}
                if self.companion.running and differs(own):     # the update settings are the launcher's own
                    self.restart_companion(["--no-launch"])
            self.redraw()

        def open_file():
            try:
                os.startfile(model.CONFIG_PATH)     # noqa: S606
            except (OSError, AttributeError) as e:
                self.say(f"не открылся config.json: {e}", "WARN")
        p.button(30, height - 60, 170, height - 26, "Сохранить", "save", save)
        p.button(182, height - 60, 362, height - 26, "Открыть config.json", "file", open_file)
        def close():
            """The cross and «Закрыть»: changes not saved are asked about, not lost silently."""
            changes = edited()
            if changes is None or differs(changes):
                answer = messagebox.askyesnocancel("Barrens Voice Launcher", "Сохранить изменения в настройках?", parent=win)
                if answer is None:
                    return
                if answer:
                    save()
                    return
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)
        p.button(width - 150, height - 60, width - 30, height - 26, "Закрыть", "close", close)

    def update_settings_changed(self, before: dict, after: dict) -> None:
        """«Обновлять автоматически» turned on, or the channel changed: check now, not at the next hourly check - a
        version found while updates were off gets prepared (Updater.check: prepare unless the version failed here),
        a test build is looked for. The switch is as always: with no game and no voice-over (apply_update_when_idle).
        The addon goes on as before (update_addons, every tick)."""
        if self.updater is None:
            return
        turned_on = after.get("auto_update") is True and before.get("auto_update") is False
        if turned_on or after.get("update_channel") != before.get("update_channel"):
            threading.Thread(target=self._check_update, daemon=True, name="update-check").start()

    def open_help(self) -> None:
        if self.help_win is not None and self.help_win.winfo_exists():
            self.help_win.lift()
            return
        from ..links import LINKS
        width, height = 760, 460
        win, c, p = self._child("Помощь", width, height)
        self.help_win = win
        win.icons = []
        media = model.addon_source()
        try:
            from PIL import Image, ImageTk
        except ImportError:
            Image = ImageTk = None

        def picture(name: str, size: int):
            """A picture from the addon's Media (the same as in the game), or the launcher's own (the community's
            Battle.net logo, battlenet.png: the same picture as the addon's Media/battlenet.tga), or None."""
            path = HERE / f"{name}.png" if name == "battlenet" else media / "Media" / f"{name}.tga" if media else None
            if Image is None or path is None:
                return None
            try:
                img = ImageTk.PhotoImage(Image.open(path).resize((round(size * self.scale),) * 2), master=win)
            except OSError:
                return None
            win.icons.append(img)
            return img

        # three blocks a gap apart: what this is for, the links, what to send. The words and the links are the
        # addon's «Помощь» tab's (Options.lua), the community first, and its paragraphs an empty line apart: the
        # second under the first's real height (it wraps) and one line
        import tkinter.font as tkfont
        first = c.create_text(30, 78, anchor="nw", width=width - 60, fill=PARCHMENT, font=p.body,
                              text="Ошибки, улучшения, предложения, или что-то прочитано неправильно — пиши в Discord, "
                                   "Telegram или в сообщество.")
        line = tkfont.Font(font=c.font(p.body)).metrics("linespace") / c.s

        def below(item) -> int:
            """Where the next paragraph starts: under the item's real bottom (it may wrap) and one empty line."""
            return round(c.bbox(item)[3] / c.s + line)
        top = below(first)
        github = "А лучше сразу заведи issue на GitHub"
        c.create_text(30, top, anchor="nw", fill=PARCHMENT, font=p.body, text=github)
        smile = picture("smile", 16)
        if smile:
            c.create_image(30 + p.measure(github, p.body) + 6, top + 9, image=smile)
        rows = top + 52                                     # the links a gap under it
        for i, (key, name) in enumerate((("community", "Сообщество"), ("discord", "Discord"), ("telegram", "Telegram"),
                                         ("github", "GitHub"))):
            y = rows + i * 44
            x = 30
            img = picture("battlenet" if key == "community" else key, 22)
            if img:
                c.create_image(x + 11, y, image=img)
            c.create_text(x + 32, y, text=name, anchor="w", fill=GOLD, font=p.body)
            box = p.entry(x + 120, y, 410, LINKS[key], readonly=True)
            box.bind("<FocusIn>", lambda e, b=box: b.select_range(0, "end"))

            def copy(url=LINKS[key]):
                win.clipboard_clear()
                win.clipboard_append(url)
                self.say("ссылка скопирована")
            p.button(x + 540, y - 14, x + 630, y + 14, "Копировать", f"copy{key}", copy, font=p.small)
            p.button(x + 638, y - 14, x + 700, y + 14, "Открыть", f"open{key}",
                     lambda url=LINKS[key]: webbrowser.open(url), font=p.small)
        send = c.create_text(30, rows + 166, anchor="nw", width=width - 60, fill=PARCHMENT, font=p.body,
                             text="Что прислать: что прозвучало и как надо, а лучше — строку из журнала (кнопка «Журнал»).")
        c.create_text(30, below(send), anchor="nw", width=width - 60, fill=PARCHMENT, font=p.body,
                      text="Неверное ударение можно сразу поправить себе на вкладке «Словарь» в настройках аддона.")
        p.button(width - 150, height - 50, width - 30, height - 18, "Закрыть", "close", win.destroy)


APP_ID = "BarrensVoice.Launcher"


def register_app_id() -> None:
    """The name and icon Windows shows for the launcher's notifications (else the id itself): a key
    under the user's AppUserModelId, as an installer would write it."""
    try:
        import winreg
        from .paint import ICON_FILE
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Barrens Voice Launcher")   # the notices' sender
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ICON_FILE))
    except OSError:
        pass


def single_instance(tray: bool = False) -> bool:
    """True for the first launcher; a second one asks the first to show itself (and, started by hand, to launch
    the game: second_start) and ends."""
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "Local\\BarrensVoiceLauncher")
    if ctypes.get_last_error() == 183:                    # ERROR_ALREADY_EXISTS
        try:
            SHOW_FILE.parent.mkdir(parents=True, exist_ok=True)
            SHOW_FILE.write_text("" if tray else "play", encoding="utf-8")
        except OSError:
            pass
        return False
    return True


_mutex = None


def run_in_console(args: dict) -> None:
    """--console: the companion in a console window of its own, as BarrensVoice.cmd ran it before the
    launcher (the window stays open after an error, to read it)."""
    import subprocess
    python = Path(sys.executable).with_name("python.exe")
    flags = ([f"--{args['game']}"] if args["game"] else []) + args["companion"]
    command = subprocess.list2cmdline([str(python), "-m", "bv", *flags])
    subprocess.Popen(f'cmd.exe /c "title Barrens Voice & {command} || pause"', cwd=APP_DIR,
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def main() -> int:
    if sys.stderr is None or "pythonw" in Path(sys.executable).name.lower():
        LAUNCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(LAUNCHER_LOG)                      # now: from here on the file is open as stderr
        sys.stderr = open(LAUNCHER_LOG, "a", encoding="utf-8", buffering=1)   # tracebacks of a windowless run
        sys.stdout = sys.stderr
    args = model.parse_args(sys.argv[1:])
    if args["help"] or args["unknown"]:
        root = tk.Tk()
        root.withdraw()
        text = (f"Неизвестно: {' '.join(args['unknown'])}\n\n" if args["unknown"] else "") + model.USAGE
        messagebox.showinfo("Barrens Voice Launcher", text, parent=root)
        return 0 if args["help"] else 2
    if args["console"]:
        run_in_console(args)
        return 0
    if not single_instance(args["tray"]):
        return 0
    if sys.platform == "win32":
        import ctypes
        try:                                           # sharp at any screen scale; ScaledCanvas does the sizes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        register_app_id()                              # its own taskbar button and icon, not Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    root = tk.Tk()
    scale = args["scale"] or max(1.0, root.winfo_fpixels("1i") / 96)
    if args["pos"]:
        root.geometry("+{}+{}".format(*args["pos"]))
    App(root, args, scale)
    root.mainloop()
    return 0
