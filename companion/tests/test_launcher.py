"""The launcher's logic without its window: games and addon, versions, settings, state lines."""
import os
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
import tomllib

# the version in work (pyproject.toml's): a release build's tag must name it
WORKING = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"]["version"]


def make_game(root: Path, addon_version: str | None = None) -> Path:
    root.mkdir(parents=True)
    (root / "Wow.exe").write_bytes(b"")
    if addon_version:
        addon = root / "Interface" / "AddOns" / "BarrensVoice"
        addon.mkdir(parents=True)
        (addon / "BarrensVoice.toc").write_text(f"## Interface: 16001\n## Version: {addon_version}\n", encoding="utf-8")
    return root


def make_version(root: Path, version: str) -> Path:
    """An installed version's folder as point_current takes it: with what the starter runs (its pythonw.exe and the
    launcher's script)."""
    folder = root / "app" / version
    (folder / "companion").mkdir(parents=True)
    (folder / "companion" / "BarrensVoice.pyw").write_text("", encoding="utf-8")
    built(root, folder)
    return folder


def built(root: Path, version_dir: Path, *rest, **k) -> None:
    """A stand-in for install.build: the environment's pythonw.exe, the file of it the starter needs."""
    pythonw = version_dir / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True, exist_ok=True)
    pythonw.write_bytes(b"")


class Versions(unittest.TestCase):
    def test_compare(self):
        from bv.launcher.model import newer, version_key
        self.assertEqual(version_key("v0.4.1"), version_key("0.4.1"))
        self.assertTrue(newer("0.4.10", "0.4.9"))
        # a test build is older than its release and newer than the one before; betas in their order
        self.assertTrue(newer("0.4.0", "0.4.0b5") and newer("0.4.0b5", "0.3.0") and newer("0.4.0b10", "0.4.0b9"))
        self.assertEqual(version_key("v0.4.0-beta.5"), version_key("0.4.0b5"))
        self.assertFalse(newer("v0.4.0", "0.4.0"))
        self.assertFalse(newer("", "0.4.0"))

    def test_app_and_addon_versions(self):
        # the app's version is pyproject.toml's; the addon's, its own .toc's (raised only when it changes)
        import tomllib
        from bv.launcher.model import addon_source, addon_version, app_version
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(app_version(), tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])
        toc = (addon_source() / "BarrensVoice.toc").read_text(encoding="utf-8")
        self.assertIn(f"## Version: {addon_version()}", toc)


class Games(unittest.TestCase):
    def test_found_with_their_addon(self):
        from bv.launcher.model import find_games
        with tempfile.TemporaryDirectory() as d:
            forever = make_game(Path(d) / "A" / "World of Warcraft" / "_forever_", "0.3.0")
            make_game(Path(d) / "B" / "World of Warcraft" / "_retail_")
            games = find_games({"wow_dir": str(forever)}, extra=[Path(d) / "B" / "World of Warcraft"])
        self.assertEqual([(g.key, g.name, g.addon) for g in games],
                         [("forever", "WoW Forever", "0.3.0"), ("retail", "WoW Retail", None)])
        self.assertEqual(games[0].root, forever)

    def test_a_folder_without_the_game_is_not_one(self):
        from bv.launcher.model import find_games
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_retail_").mkdir()
            self.assertEqual(find_games({}, extra=[Path(d)]), [])

    def test_install_addon(self):
        from bv.launcher.model import ADDON, install_addon, toc_version
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "src" / ADDON
            source.mkdir(parents=True)
            (source / f"{ADDON}.toc").write_text("## Version: 0.4.0\n", encoding="utf-8")
            (source / "Media").mkdir()
            (source / "Media" / "README.md").write_text("x", encoding="utf-8")
            (source / "Media" / "icon.tga").write_bytes(b"tga")
            game = make_game(Path(d) / "_forever_", "0.3.0")
            old = game / "Interface" / "AddOns" / ADDON
            (old / "Removed.lua").write_text("-- gone from the new version", encoding="utf-8")
            (old / "Old").mkdir()
            (old / "Old" / "x.tga").write_bytes(b"tga")
            (old / "._Core.lua").write_text("mac", encoding="utf-8")
            (old / "VoiceList.lua").write_text("BarrensVoiceVoiceList = { 'Irina' }", encoding="utf-8")
            (source / "VoiceList.lua").write_text("BarrensVoiceVoiceList = BarrensVoiceVoiceList or {}", encoding="utf-8")
            (source / "CompanionStatus.lua").write_text("BarrensVoiceStatus = nil", encoding="utf-8")
            (source / "DictionaryOnline.lua").write_text("BarrensVoiceDictionaryOnline = nil", encoding="utf-8")
            (old / "DictionaryOnline.lua").write_text("BarrensVoiceDictionaryOnline = { revision = 5 }", encoding="utf-8")
            (source / "NewsData.lua").write_text("BarrensVoiceNewsData = nil", encoding="utf-8")
            (old / "NewsData.lua").write_text('BarrensVoiceNewsData = { version = "0.4.0b16" }', encoding="utf-8")
            target = install_addon(source, game)
            # the companion's list of voices kept; the status file, not there before, comes from the addon
            self.assertIn("Irina", (target / "VoiceList.lua").read_text(encoding="utf-8"))
            self.assertTrue((target / "CompanionStatus.lua").exists())
            self.assertEqual(toc_version(target), "0.4.0")
            self.assertTrue((target / "Media" / "icon.tga").exists())
            self.assertFalse((target / "Media" / "README.md").exists())
            left = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))
            self.assertEqual(left, ["BarrensVoice.toc", "CompanionStatus.lua", "DictionaryOnline.lua", "Media",
                                    "Media/icon.tga", "NewsData.lua", "VoiceList.lua"])   # the old version's own gone
            self.assertIn("revision = 5", (target / "DictionaryOnline.lua").read_text(encoding="utf-8"))  # the companion's kept
            self.assertIn("0.4.0b16", (target / "NewsData.lua").read_text(encoding="utf-8"))  # its «Что нового» too


class Paths(unittest.TestCase):
    """An install is told by its mark file (paths.layout): its data and cache folders, its starter. BV_DATA,
    BV_CACHE and BV_STARTER override them (tests, odd setups); not installed, the companion's own folder."""

    def run_paths(self, env):
        import os
        import subprocess
        import sys
        code = ("from bv import paths; print(paths.CODE_DIR); print(paths.DATA_DIR); print(paths.CACHE_DIR); "
                "print(paths.starter())")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("BV_")}
        out = subprocess.run([sys.executable, "-c", code], env={**clean, **env}, capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parents[1], check=True).stdout.splitlines()
        return [Path(x) for x in out[:3]] + [out[3]]

    def test_install_layout(self):
        from bv.paths import INSTALL_MARK, layout
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            code = root / "app" / "0.4.1" / "companion"
            code.mkdir(parents=True)
            self.assertIsNone(layout(code, {})["root"])                     # no mark: not an install
            (root / INSTALL_MARK).write_text("", encoding="utf-8")
            got = layout(code, {})
            self.assertEqual((got["root"], got["data"], got["cache"]), (root, root / "data", root / "cache"))
            self.assertIn(str(root / "app" / "current" / ".venv"), got["starter"])   # through the junction
            (root / "Barrens Voice.exe").write_bytes(b"")
            self.assertEqual(layout(code, {})["starter"], f'"{root / "Barrens Voice.exe"}"')   # the starter, if there
            self.assertEqual(layout(code, {"BV_DATA": "x"})["data"], Path("x"))

    def test_installed_and_not(self):
        code, data, cache, start = self.run_paths({})
        self.assertEqual((data, cache), (code, code))                   # as before the installer
        self.assertTrue(start.endswith('BarrensVoice.pyw"'))
        with tempfile.TemporaryDirectory() as d:
            code, data, cache, start = self.run_paths({"BV_DATA": f"{d}/data", "BV_CACHE": f"{d}/cache",
                                                       "BV_STARTER": '"py" "start.pyw"'})
            self.assertEqual((data, cache, start), (Path(d) / "data", Path(d) / "cache", '"py" "start.pyw"'))


class GameFolder(unittest.TestCase):
    def test_world_of_warcraft_folder_taken_for_the_game(self):
        from bv.launcher.model import game_folder
        with tempfile.TemporaryDirectory() as d:
            wow = Path(d) / "World of Warcraft"
            make_game(wow / "_forever_")
            self.assertEqual(game_folder(str(wow), "forever"), os.path.normpath(str(wow / "_forever_")))
            self.assertEqual(game_folder(str(wow / "_forever_"), "forever"), os.path.normpath(str(wow / "_forever_")))
            self.assertEqual(game_folder(str(wow), "retail"), os.path.normpath(str(wow)))   # no retail there: as typed
            self.assertEqual(game_folder("", "forever"), "")


def config_window(config: dict, say):
    """A stand-in for the launcher's window with what its settings code uses."""
    from types import SimpleNamespace
    from bv.launcher import app
    win = SimpleNamespace(config=config, config_told=set(), pending={}, say=say)
    win.write_config = lambda **kw: app.App.write_config(win, **kw)
    return win


class Config(unittest.TestCase):
    def test_update_keeps_the_rest(self):
        from bv.launcher.model import read_config, update_config
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"players": {"Вася": "orc-male"}, "rate": 3}), encoding="utf-8")
            update_config(path, game="retail")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((saved["players"], saved["rate"], saved["game"]), ({"Вася": "orc-male"}, 3, "retail"))
            self.assertTrue(read_config(path)["exit_with_wow"])        # defaults under the file

    def test_unparsable_file_never_written_over(self):
        # a hand edit gone wrong (a single backslash in a path): the launcher used to write the defaults over it
        from bv.launcher.model import BrokenConfig, config_problem, read_config, update_config
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            broken = '{"wow_dir": "D:\\Games\\WoW", "players": {"Вася": "orc-male"}}'   # JSON has no \\G
            path.write_text(broken, encoding="utf-8")
            self.assertTrue(read_config(path)["exit_with_wow"])                     # defaults to show the window
            self.assertIn("config.json", config_problem(path))
            with self.assertRaises(BrokenConfig):
                update_config(path, game="retail")
            self.assertEqual(path.read_text(encoding="utf-8"), broken)                 # the player's file as it was
            path.write_text("[]", encoding="utf-8")
            self.assertRaises(BrokenConfig, update_config, path, game="retail")
            path.write_text('\ufeff{"rate": 3}', encoding="utf-8")                   # Notepad's BOM: fine
            self.assertIsNone(config_problem(path))
            self.assertEqual(update_config(path, game="retail")["rate"], 3)
            self.assertIsNone(config_problem(Path(d) / "none.json"))                 # none yet: written fresh

    def test_a_broken_file_said_once_in_the_players_words(self):
        # the main window had it twice - the launcher's WARN with the parser's English, the voice-over's ERROR - and
        # the voice-over started only to stop with code 5: one ERROR now, line and column, what to do; the parser's
        # words a DEBUG line; no voice-over (start_companion) until the file reads again
        from bv.launcher import app
        from bv.launcher.model import config_error
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text('{\n  "rate": 3,\n  rate: 4\n}', encoding="utf-8")      # a name without quotes
            error = config_error(path)
            self.assertEqual(str(error), "config.json не читается: ошибка в строке 3, столбец 3")
            self.assertIn("Expecting property name", error.detail)
            self.assertIn("char", error.detail)
        said = []
        win = SimpleNamespace(checked={}, config_told=set(), config_error=None,
                              say=lambda text, level="INFO": said.append((level, text)))
        win.say_config_error = lambda e: app.App.say_config_error(win, e)
        app.App.check_config(win, 1.0, error)
        self.assertEqual([lvl for lvl, _ in said], ["ERROR", "DEBUG"])
        self.assertTrue(said[0][1].startswith("config.json не читается: ошибка в строке 3, столбец 3 — исправь его "
                                              "(«Открыть config.json» в «Настройках»)"), said[0][1])
        self.assertNotIn(";", said[0][1])
        self.assertEqual(win.config_told, {"broken"})                        # set_config: no second word of it
        app.App.check_config(win, 3.0, error)
        self.assertEqual(len(said), 2)                                        # the same: said once
        app.App.check_config(win, 5.0, None)
        self.assertEqual(said[-1], ("INFO", "config.json снова читается"))
        self.assertEqual((win.config_error, win.config_told, win.checked["config"]), (None, set(), 5.0))
        # not opened (another program holds it, its permissions): another remedy, said the same way
        from bv.launcher.model import ConfigNotOpened
        app.App.check_config(win, 7.0, ConfigNotOpened("config.json не открывается: Permission denied"))
        self.assertEqual(said[-1], ("ERROR", "config.json не открывается: Permission denied — закрой программу, которая "
                                             "его держит, и проверь права на файл, до тех пор лаунчер в него не пишет и "
                                             "озвучку не запускает"))
        # «Играть» with it broken: said again (a click asked), not only the red state line
        said.clear()
        win.busy, win.running_game, win.selected = "", None, "forever"
        win.companion = SimpleNamespace(running=False)
        win.game = lambda key: SimpleNamespace(addon="0.4.0")
        win.start_companion = lambda args=None: False
        app.App.play(win)
        self.assertEqual([lvl for lvl, _ in said], ["ERROR"])
        said.clear()
        win.running_game = "forever"                      # the game runs, the voice-over not: the same
        app.App.play(win)
        self.assertEqual([lvl for lvl, _ in said], ["ERROR"])

    def test_a_file_held_a_moment_is_read_again(self):
        # an antivirus or an editor's save holds config.json for a moment: read again after 0.1 s, no error for it
        from unittest import mock
        from bv.launcher import model
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text('{"rate": 7}', encoding="utf-8")
            real, calls = Path.read_text, []

            def held_once(self, *a, **k):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError(13, "held")
                return real(self, *a, **k)
            with mock.patch.object(Path, "read_text", held_once), mock.patch("time.sleep"):
                self.assertEqual(model.read_config(path, strict=True)["rate"], 7)
            with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")), \
                    mock.patch("time.sleep"):
                self.assertIsInstance(model.config_error(path), model.ConfigNotOpened)

    def test_unreadable_file_never_written_over(self):
        # in use (a sharing violation, an antivirus scan) or its permissions: the launcher wrote the defaults over it
        from unittest import mock
        from bv.launcher.model import BrokenConfig, config_problem, read_config, update_config
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            mine = json.dumps({"players": {"Громмаш": "orc-male"}, "rate": 3})
            path.write_text(mine, encoding="utf-8")
            real = Path.read_text

            def locked(self, *a, **k):
                if self == path:
                    raise PermissionError(13, "The process cannot access the file")
                return real(self, *a, **k)
            with mock.patch.object(Path, "read_text", locked):
                self.assertTrue(read_config(path)["exit_with_wow"])                 # the window still opens
                self.assertIn("config.json", config_problem(path))
                with self.assertRaises(BrokenConfig):
                    update_config(path, game="retail")
            self.assertEqual(path.read_text(encoding="utf-8"), mine)               # nothing written
            self.assertEqual(update_config(path, game="retail")["rate"], 3)          # readable again: written

    def test_failed_write_said_once_and_cleaned(self):
        # read-only, or os.replace meeting a reader: the error escaped set_config every 2 s (the game's folder
        # remembered at each tick) with a traceback, and config.tmp stayed beside the file
        from types import SimpleNamespace
        from unittest import mock
        from bv.launcher import app, model
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            mine = json.dumps({"rate": 3})
            path.write_text(mine, encoding="utf-8")
            said = []
            win = config_window(model.read_config(path), lambda m, level="INFO": said.append((level, m)))
            real = model.update_config
            with mock.patch.object(model, "update_config", lambda **kw: real(path, **kw)):
                with mock.patch("os.replace", side_effect=PermissionError(13, "Access is denied")):
                    with self.assertRaises(model.ConfigNotWritten):
                        real(path, game="retail")
                    app.App.set_config(win, game_dirs={"forever": "W"})
                    app.App.set_config(win, game="retail")
                self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["config.json"])   # no config.tmp
                self.assertEqual(path.read_text(encoding="utf-8"), mine)
                self.assertEqual((win.config["game_dirs"], win.config["game"]), ({"forever": "W"}, "retail"))
                self.assertEqual(len(said), 1)
                self.assertEqual(said[0][0], "WARN")
                self.assertIn("config.json не записывается: Access is denied", said[0][1])
                app.App.set_config(win, rate=4)                                         # written again: said anew
                saved = json.loads(path.read_text(encoding="utf-8"))
                # and the changes the file did not take are written with it, not lost to it
                self.assertEqual((saved["rate"], saved["game"], saved["game_dirs"]), (4, "retail", {"forever": "W"}))
                self.assertEqual((win.config_told, win.pending), (set(), {}))
                with mock.patch("pathlib.Path.write_text", side_effect=OSError(28, "No space left on device")):
                    app.App.set_config(win, rate=5)
            self.assertEqual(len(said), 2)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["config.json"])

    def test_window_keeps_the_change_and_says_once(self):
        from types import SimpleNamespace
        from unittest import mock
        from bv.launcher import app, model
        said = []
        win = config_window({"game": "forever"}, lambda m, level="INFO": said.append(level))
        with mock.patch.object(model, "update_config", side_effect=model.BrokenConfig("config.json не читается")):
            app.App.set_config(win, game="retail")
            app.App.set_config(win, launch_wow=True)
        self.assertEqual(win.config, {"game": "retail", "launch_wow": True})
        self.assertEqual(said, ["WARN"])
        # then the other kind: said too (one flag for both kept it quiet), each once
        with mock.patch.object(model, "update_config", side_effect=model.ConfigNotWritten("config.json не записывается")):
            app.App.set_config(win, game="forever")
            app.App.set_config(win, game="retail")
        self.assertEqual(said, ["WARN", "WARN"])
        self.assertEqual(win.pending, {"game": "retail", "launch_wow": True})
        written = []
        with mock.patch.object(model, "update_config", side_effect=lambda **kw: written.append(kw) or kw):
            app.App.set_config(win, rate=4)                                            # written: said anew after
        self.assertEqual(written, [{"game": "retail", "launch_wow": True, "rate": 4}])
        self.assertEqual(win.pending, {})
        with mock.patch.object(model, "update_config", side_effect=model.BrokenConfig("config.json не читается")):
            app.App.set_config(win, game="forever")
        self.assertEqual(said, ["WARN", "WARN", "WARN"])


class LaunchFromTheWindow(unittest.TestCase):
    def test_the_launch_details_stay_out_of_the_main_window(self):
        # «Играть» with the companion running launches from the launcher: launch.launch's lines took the
        # launcher's INFO, and «Battle.net: --exec=launch WoWF» showed in the main window
        from bv.launcher import app
        said = []
        win = SimpleNamespace(say=lambda message, level="INFO": said.append((level, message)))
        for line in ("запускаю игру через Battle.net ...", "    Battle.net: --exec=launch WoWF",
                     "Battle.net не найден: укажи его в «Настройках»"):
            app.App.say_as_companion(win, line)
        self.assertEqual([level for level, _ in said], ["INFO", "DEBUG", "WARN"])


class LiveFromTheWindow(unittest.TestCase):
    def test_a_hand_edit_made_while_the_window_was_open_is_taken_at_the_next_start(self):
        # the window read config.json at its start; the player then opened it from Settings and set «Запускать
        # игру…» off. The next start must go by the file (no --launch) unless the window holds a change of its own
        # the file did not take (pending)
        from unittest import mock
        from bv.launcher import app, model
        file = {"game": "forever", "launch_wow": False, "exit_with_wow": False}
        for pending, launch in (({}, "--no-launch"), ({"launch_wow": True}, "--launch")):
            got = []
            win = SimpleNamespace(companion=SimpleNamespace(running=False, start=got.append), busy="", selected="forever",
                                  addon_missing=lambda: False, pending=dict(pending), extra=[], say=lambda *a: None,
                                  settle_due=False, config={"game": "forever", "launch_wow": True, "exit_with_wow": True})
            with mock.patch.object(model, "read_config", lambda strict=False: dict(file)):
                self.assertTrue(app.App.start_companion(win))
            self.assertEqual(got[0], ["--forever", launch])
        # a file that cannot be read now: no voice-over (it would end at once on the same file, its error twice), and
        # the window is told what is wrong (check_config: the ERROR line, the state line)
        got, told = [], []
        broken = model.BrokenConfig("config.json не читается: ошибка в строке 3, столбец 3", "Expecting value (char 9)")
        win = SimpleNamespace(companion=SimpleNamespace(running=False, start=got.append), busy="", selected="forever",
                              addon_missing=lambda: False, pending={}, extra=[], say=lambda *a: None, settle_due=False,
                              config={"game": "forever", "launch_wow": True, "exit_with_wow": True},
                              check_config=lambda now, error: told.append(error))
        with mock.patch.object(model, "read_config", side_effect=broken):
            self.assertFalse(app.App.start_companion(win))
        self.assertEqual((got, told), ([], [broken]))


class StateLines(unittest.TestCase):
    def test_running_and_ready(self):
        from bv.launcher.model import GREEN, describe
        lines = describe({"phase": "running", "voices": "ready", "device": "RTX", "linked": True}, True, None,
                         "WoW Forever", False)
        self.assertEqual([c for c, _ in lines], [GREEN] * 4)
        self.assertEqual(lines[2][1], "Голоса рас готовы — RTX")
        self.assertEqual(lines[1][1], "WoW Forever запущена")

    def test_stopped_and_problems(self):
        from bv.launcher.model import GREY, RED, describe
        self.assertEqual(describe({}, False, None, None, True)[0][0], RED)                  # no addon
        self.assertIn("ошибкой", describe({}, False, 1, None, False)[0][1])
        self.assertIn("уже запущен", describe({}, False, 4, None, False)[0][1])
        self.assertIn("config.json", describe({}, False, 5, None, False)[0][1])
        self.assertIn("игра закрыта", describe({"phase": "game_closed"}, False, 0, None, False)[0][1])
        error = describe({"phase": "error", "error": "аддон не установлен"}, False, 1, None, False)[0]
        self.assertEqual(error, (RED, "Озвучка: аддон не установлен"))
        self.assertEqual(describe({"voices": "ready"}, False, 0, None, False)[2][0], GREY)  # stale file: not ready

    def test_bridge(self):
        from bv.launcher.model import describe
        self.assertIn("свёрнуто", describe({"window": False}, True, None, "WoW Forever", False)[3][1])
        self.assertIn("перекрывает", describe({"damaged": True}, True, None, "WoW Forever", False)[3][1])
        self.assertIn("ждём игру", describe({}, True, None, None, False)[3][1])


class CompanionState(unittest.TestCase):
    def test_file(self):
        from bv import state
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tmp" / "state.json"
            state.report(voices="x")                    # before init: nothing written anywhere
            state.init(path, phase="starting")
            state.report(voices="ready", linked=True)
            got = state.read(path)
            self.assertEqual((got["phase"], got["voices"], got["linked"]), ("starting", "ready", True))
            state.init(path)                            # a new run: a fresh file
            self.assertNotIn("voices", state.read(path))
            state._path = None
        self.assertEqual(state.read(Path(d) / "none.json"), {})


class GameOfPath(unittest.TestCase):
    def test_folders(self):
        from bv.launch import game_of_path
        self.assertEqual(game_of_path(r"E:\World of Warcraft\_forever_\Wow.exe".replace("\\", "/")), "forever")
        self.assertEqual(game_of_path("D:/World of Warcraft/_retail_/Wow.exe"), "retail")
        self.assertIsNone(game_of_path("D:/World of Warcraft/_classic_era_/WowClassic.exe"))
        self.assertEqual(game_of_path(r"D:\Games\World of Warcraft\_forever_\Wow.exe"), "forever")   # either slash

    def test_only_the_game_is_the_game(self):
        # any wow*.exe counted: with WowUp running --launch neither made the voices nor started the
        # game, the exit with the game never came, and the capture could pick the manager's window
        from unittest import mock

        from bv import launch
        game = r"D:\Games\World of Warcraft\_forever_\Wow.exe"
        self.assertTrue(launch.is_game(game))
        for other in (r"C:\Programs\WowUp\WowUp.exe", r"C:\Programs\WowUp-CF\WowUp-CF.exe",
                      r"D:\Games\World of Warcraft\_forever_\WoWChatVoice.exe", r"D:\Games\_forever_\Battle.net.exe",
                      r"D:\Games\World of Warcraft\_classic_era_\WowClassic.exe", ""):
            self.assertFalse(launch.is_game(other), other)
        with mock.patch.object(launch, "process_paths", lambda match: [p for p in [r"C:\Programs\WowUp\WowUp.exe"]
                                                                       if match("WowUp.exe")]):
            self.assertFalse(launch.wow_running())
            self.assertEqual(launch.running_game(), (None, None))
        with mock.patch.object(launch, "process_paths", lambda match: [r"C:\Programs\WowUp\WowUp.exe", game]):
            self.assertTrue(launch.wow_running())
            self.assertEqual(launch.running_game()[0], "forever")


class Roots(unittest.TestCase):
    def test_remembered_and_wow_dir_first(self):
        from bv.dictionary import wow_roots
        roots = wow_roots({"game": "retail", "game_dirs": {"retail": "X:/WoW/_retail_"}, "wow_dir": "Y:/WoW/_forever_"})
        self.assertEqual(roots[:2], [Path("X:/WoW/_retail_"), Path("Y:/WoW/_retail_")])   # the sibling, not _forever_
        self.assertEqual(wow_roots({"wow_dir": "Y:/World of Warcraft"})[0], Path("Y:/World of Warcraft/_forever_"))

    def test_remember(self):
        from bv.launcher.model import remember
        self.assertEqual(remember({}, "forever", Path("E:/W/_forever_")), {"game_dirs": {"forever": str(Path("E:/W/_forever_"))}})
        self.assertIsNone(remember({"game_dirs": {"forever": str(Path("E:/W/_forever_"))}}, "forever", Path("E:/W/_forever_")))
        kept = remember({"game_dirs": {"retail": "D:/x"}}, "forever", Path("E:/f"))
        self.assertEqual(set(kept["game_dirs"]), {"retail", "forever"})


class LogLines(unittest.TestCase):
    def test_glued_line_still_read(self):
        # a library's bar without its line end, then the companion's own line: one line on the pipe
        from bv.logs import parse
        self.assertEqual(parse("\x1b[A 40%|####   |\r04:45:46 INFO  собираю голоса рас из игры"),
                         ("04:45:46", "INFO", "собираю голоса рас из игры"))
        self.assertEqual(parse("04:45:46 INFO  связь с аддоном есть"), ("04:45:46", "INFO", "связь с аддоном есть"))
        self.assertEqual(parse("some library text"), ("", "DEBUG", "some library text"))

    def test_raw_stderr(self):
        from datetime import datetime

        from bv.logs import parse, raw_line
        now = datetime(2026, 11, 17, 4, 45, 40)
        self.assertEqual(raw_line(" 10%|#\r 50%|##\r100%|####\r\n", now), "04:45:40 DEBUG [stderr] 100%|####")
        self.assertEqual(raw_line("\x1b[0;93mW: onnxruntime warning\x1b[m\n", now), "04:45:40 DEBUG [stderr] W: onnxruntime warning")
        self.assertIsNone(raw_line("\r\x1b[A  \r\n", now))
        self.assertEqual(parse(raw_line("x\n", now))[1], "DEBUG")

    def test_stderr_apart_from_the_log(self):
        # stdout carries the companion's log lines only: what libraries write below Python has its own pipe
        import inspect

        from bv.launcher import process
        source = inspect.getsource(process)
        self.assertIn("stderr=subprocess.PIPE", source)
        self.assertNotIn("stderr=subprocess.STDOUT", source)

    def test_glue_in_a_real_process(self):
        # the case seen: a library writes a bar to fd 2 with no line end, then the companion logs a line;
        # a stand-in companion (a "bv" package of its own in the folder the launcher starts it in)
        import tempfile
        import time

        from bv.launcher.process import Companion
        from bv.logs import parse
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bv").mkdir()
            (Path(d) / "bv" / "__init__.py").write_text("")
            (Path(d) / "bv" / "__main__.py").write_text(
                "import os, sys\n"
                "os.write(2, b'\\x1b[A 40%|####   |\\r 80%|########|\\r')\n"   # below Python, no line end
                "print('04:45:46 INFO  собираю голоса рас из игры', flush=True)\n"
                "os.write(2, b'W: onnxruntime\\n')\n"
                "print('04:45:47 INFO  связь с аддоном есть', flush=True)\n", encoding="utf-8")
            got = []
            companion = Companion(Path(d), got.append)
            companion.start()
            companion.proc.wait(timeout=30)
            deadline = time.monotonic() + 5
            pipes = (companion.proc.stdout, companion.proc.stderr)
            while (len(got) < 3 or not all(p.closed for p in pipes)) and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertTrue(all(p.closed for p in pipes))          # read to the end and closed (no ResourceWarning)
        lines = [parse(x) for x in got]
        info = [m for _, lv, m in lines if lv == "INFO"]
        self.assertEqual(info, ["собираю голоса рас из игры", "связь с аддоном есть"], got)
        # the bar and the warning after it are one line of stderr: as in a console, the warning overwrote the bar
        self.assertEqual([m for _, lv, m in lines if lv == "DEBUG"], ["[stderr] W: onnxruntime"], got)

    def test_the_log_scrolls(self):
        # the main window's log showed only the newest lines that fit: scrolled back with the wheel or the bar
        from bv.launcher.model import log_back_at, log_thumb, log_view
        self.assertEqual(log_view(10, 14, 5), (0, 0))              # all fit: nothing to scroll
        self.assertEqual(log_view(40, 14, 0), (26, 0))             # the newest at the bottom
        self.assertEqual(log_view(40, 14, 6), (20, 6))
        self.assertEqual(log_view(40, 14, 99), (0, 26))            # not past the oldest
        self.assertEqual(log_view(40, 14, -3), (26, 0))            # nor past the newest
        self.assertIsNone(log_thumb(14, 14, 0, 0, 100))            # no bar when all fit
        self.assertEqual(log_thumb(40, 10, 30, 0, 100), (75.0, 100.0))   # a quarter shown, at the bottom
        self.assertEqual(log_thumb(40, 10, 0, 0, 100), (0.0, 25.0))
        self.assertEqual(log_thumb(1000, 10, 0, 0, 100), (0.0, 24.0))    # never thinner than a finger's grip
        for back in (0, 7, 30):                                     # dragging: the thumb's place back to rows
            first, _ = log_view(40, 10, back)
            self.assertEqual(log_back_at(40, 10, log_thumb(40, 10, first, 0, 100)[0], 0, 100), back)
        self.assertEqual(log_back_at(40, 10, -50, 0, 100), 30)
        self.assertEqual(log_back_at(40, 10, 500, 0, 100), 0)
        # a scrolled view stays put by the lines that came after the newest it saw, also when 200 lines are kept and
        # the oldest goes as a new one comes (the count of rows stays)
        from bv.launcher.model import log_rows_after
        a, b, c, d = ("1", "INFO", "a"), ("2", "INFO", "b"), ("3", "INFO", "c"), ("4", "INFO", "d")
        self.assertEqual(log_rows_after(b, [a, b, c, d], [1, 1, 2, 1]), 3)
        self.assertEqual(log_rows_after(b, [b, c, d], [1, 2, 1]), 3)              # «a» gone above, as many rows
        self.assertEqual(log_rows_after(d, [a, b, c, d], [1, 1, 2, 1]), 0)        # nothing new: a scroll's redraw
        self.assertEqual(log_rows_after(None, [a], [1]), 0)
        self.assertEqual(log_rows_after(("0", "INFO", "x"), [a, b], [1, 1]), 0)    # gone (a progress line moved on)
        # the log's text stops short of the bar's track: the bar never covers a letter
        from bv.launcher import app
        self.assertLess(app.LX0 + 14 + (app.LX1 - app.LX0 - 28 - app.LOG_GUTTER), app.LX1 - 14 - 4)

    def test_progress_kept_in_one_line(self):
        from bv.launcher.model import main_lines
        items = [("04:45:32", "INFO", "озвучка запускается"), ("04:45:32", "DEBUG", "детали"),
                 ("04:45:46", "INFO", "собираю голоса рас из игры — несколько минут ..."),
                 ("04:45:56", "INFO", "собираю голоса рас: 3 из 66"), ("04:45:57", "WARN", "что-то"),
                 ("04:46:06", "INFO", "собираю голоса рас: 7 из 66")]
        self.assertEqual([m for _, _, m in main_lines(items)],
                         ["озвучка запускается", "что-то", "собираю голоса рас: 7 из 66"])
        items.append(("04:49:06", "INFO", "голоса рас собраны: 66 из 66 за 3 мин"))
        self.assertEqual([m for _, _, m in main_lines(items)][-1], "голоса рас собраны: 66 из 66 за 3 мин")
        self.assertEqual(len([m for _, _, m in main_lines(items) if "голоса рас" in m]), 1)
        many = [("t", "INFO", str(i)) for i in range(30)]
        self.assertEqual([m for _, _, m in main_lines(many)], [str(i) for i in range(16, 30)])


class Mismatch(unittest.TestCase):
    def test_bridge_line(self):
        from bv.launcher.model import RED, describe
        self.assertEqual(describe({"mismatch": "newer", "linked": True}, True, None, "WoW Forever", False)[3],
                         (RED, "Мост: аддон новее лаунчера — обнови Barrens Voice Launcher"))
        self.assertIn("/reload", describe({"mismatch": "older"}, True, None, "WoW Forever", False)[3][1])
        # a minimized game: its window gone, whatever «linked» last said (it stayed true under a minimized game)
        from bv.launcher.model import GREEN, YELLOW
        self.assertEqual(describe({"window": False, "linked": True}, True, None, "WoW Forever", False)[3],
                         (YELLOW, "Мост: окно игры свёрнуто или не найдено"))
        self.assertEqual(describe({"window": True, "linked": True}, True, None, "WoW Forever", False)[3],
                         (GREEN, "Мост: связь с аддоном есть"))


class MainLog(unittest.TestCase):
    def test_main_levels_only(self):
        # a busy chat through the window itself: test_layout.Layout.test_a_busy_chat_keeps_the_main_window_filled
        from bv.launcher.model import main_lines
        kept = [("20:13:37", "INFO", "голоса рас готовы")]
        self.assertEqual([m for _, _, m in main_lines(kept)], ["голоса рас готовы"])


class AddonsFollowTheApp(unittest.TestCase):
    def test_older_addons_of_closed_games(self):
        from bv.launcher.model import Game, addons_to_update

        def game(key, addon):
            return Game(key=key, name=key, root=Path("/x") / key, addon=addon)
        games = [game("forever", "0.3.0"), game("retail", "0.3.0")]
        self.assertEqual([g.key for g in addons_to_update(games, "0.3.1", None)], ["forever", "retail"])
        self.assertEqual([g.key for g in addons_to_update(games, "0.3.1", "forever")], ["retail"])   # the game runs: later
        self.assertEqual(addons_to_update(games, "0.3.0", None), [])                                  # the same version
        self.assertEqual(addons_to_update([game("forever", "0.4.0")], "0.3.1", None), [])           # newer (CurseForge): kept
        self.assertEqual(addons_to_update([game("forever", None)], "0.3.1", None), [])              # not installed: asked


class AddonVersion(unittest.TestCase):
    """A released addon version is never changed: the launcher replaces a game's addon only when its version is
    older, so an addon changed under the same version would never reach the game (and a version would stand for
    two different addons)."""

    def test_one_version_one_addon(self):
        # every release (a test build included) whose addon has the version the .toc has now had this very addon:
        # a change to the addon after a release raises ## Version before the next one
        import re
        import subprocess
        root = Path(__file__).resolve().parents[2]
        toc = "addon/BarrensVoice/BarrensVoice.toc"
        now = re.search(r"## Version:\s*(\S+)", (root / toc).read_text(encoding="utf-8")).group(1)
        tags = [t for t in subprocess.run(["git", "tag", "-l", "v*"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
                if re.fullmatch(r"v\d+\.\d+\.\d+(-beta\.\d+)?", t)]
        if not tags:
            self.skipTest("no release tags here (a shallow clone)")
        for tag in tags:
            # the release's own .toc, wherever the addon was then (0.3.0's in addon/WowChatVoice, before the rename)
            listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", tag, "addon"], cwd=root,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
            old_toc = next((f for f in listed if re.fullmatch(r"addon/([^/]+)/\1\.toc", f)), None)
            if old_toc is None:
                continue
            then = re.search(r"## Version:\s*(\S+)", subprocess.run(["git", "show", f"{tag}:{old_toc}"], cwd=root,
                                                                       capture_output=True, text=True, encoding="utf-8", errors="replace").stdout)
            if not then or then.group(1) != now:
                continue
            same = (old_toc == toc and subprocess.run(["git", "diff", "--quiet", tag, "--", "addon/BarrensVoice"],
                                                      cwd=root).returncode == 0)
            self.assertTrue(same, f"{tag} released addon {now} with other files: raise ## Version in {toc}")

    def test_a_release_carries_a_released_addon(self):
        # test builds carry the addon's test builds (0.4.0b1, 0.4.0b2...), a release its release version
        import os
        import sys
        from unittest import mock
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import release
        with mock.patch.object(release, "versions", return_value={"version": "0.4.0", "addon": "0.4.0b1", "protocol": 1}), \
                mock.patch.dict(os.environ, {"RELEASE_TAG": "v0.4.0"}), self.assertRaises(SystemExit) as stop:
            release.build()
        self.assertIn("test build of the addon", str(stop.exception))


class CommandLine(unittest.TestCase):
    def test_parse(self):
        from bv.launcher.model import parse_args
        a = parse_args(["--retail", "--f5", "--console", "--open", "help", "--scale", "1.5"])
        self.assertEqual((a["game"], a["companion"], a["console"], a["open"], a["scale"]), ("retail", ["--f5"], True, "help", 1.5))
        self.assertEqual(parse_args(["--bogus"])["unknown"], ["--bogus"])
        self.assertEqual(parse_args(["--scale", "x"])["unknown"], ["--scale x"])
        self.assertTrue(parse_args(["/?"])["help"])
        self.assertTrue(parse_args(["--tray"])["tray"])
        self.assertTrue(parse_args(["--updated"])["updated"] and not parse_args([])["updated"])
        self.assertEqual(parse_args(["--pos", "100,-20"])["pos"], (100, -20))
        for bad in ("100", "a,b", "1,2,3"):                                # said, not a crash at the window's start
            self.assertEqual(parse_args(["--pos", bad]), {**parse_args([]), "unknown": [f"--pos {bad}"]})

    def test_usage_names_every_switch(self):
        from bv.launcher.model import COMPANION_FLAGS, USAGE
        for flag in COMPANION_FLAGS + ("--forever", "--retail", "--console", "--tray", "--open", "--pos", "--scale",
                                       "--updated", "--help"):
            self.assertIn(flag, USAGE)


class Report(unittest.TestCase):
    def setUp(self):
        global model
        from bv.launcher import model

    def test_settings_changed_without_personal_data(self):
        defaults = {"volume": 100, "wow_dir": "", "exclude": [], "players": {}, "game_dirs": {}}
        config = dict(defaults, volume=80, wow_dir=r"D:\Games\World of Warcraft", exclude=["Вася", "Петя"],
                      players={"Ивар": "orc-1"}, game_dirs={"forever": r"E:\WoW\_forever_"},
                      launcher_dismissed="v0.4.1", f5_onnx_dir=r"C:\Users\someone\models")
        shown = model.changed_settings(config, defaults)
        text = "\n".join(shown)
        self.assertIn("volume = 80", shown)
        self.assertIn("exclude = 2 шт.", shown)
        self.assertIn("players = 1 шт.", shown)
        self.assertIn("game_dirs = forever", shown)
        self.assertIn("wow_dir = свой путь", shown)
        self.assertIn("f5_onnx_dir = свой путь", shown)
        for private in ("Вася", "Ивар", "Games", "someone", "launcher_dismissed"):
            self.assertNotIn(private, text)

    def test_hide_private(self):
        line = r"C:\Users\Someone\AppData\x.log; E:\WoW\_forever_\WTF\Account\MYACCOUNT\SavedVariables"
        out = model.hide_private(line, home=r"C:\Users\someone")
        self.assertEqual(out, r"~\AppData\x.log; E:\WoW\_forever_\WTF\Account\***\SavedVariables")

    def test_header(self):
        game = model.Game("forever", "WoW Forever", Path("E:/x"), "0.4.0")
        state = {"phase": "running", "voices": "ready", "device": "RTX", "provider": "CUDAExecutionProvider"}
        head = model.report_header("0.4.0", state, {"volume": 70}, [game], "forever", gpus=["RTX"],
                                   runtime="onnxruntime-gpu 1.32.0")
        self.assertIn("Barrens Voice 0.4.0", head)
        self.assertIn("Голоса рас: готовы на RTX (CUDAExecutionProvider)", head)
        self.assertIn("WoW Forever (аддон 0.4.0). Запущена: WoW Forever", head)
        self.assertIn("Настройки не по умолчанию:\n  volume = 70", head)          # a line each: values may have commas
        two = model.report_header("0.4.0", state, {"volume": 70, "game_dirs": {"forever": "E:/a", "retail": "E:/b"}},
                                  [game], "forever", gpus=["RTX"], runtime="x")
        self.assertIn("Настройки не по умолчанию:\n  game_dirs = forever, retail\n  volume = 70", two)
        self.assertNotIn(";", two.split("Настройки не по умолчанию:")[1])    # (Windows' own version may have one)
        stopped = model.report_header("0.4.0", dict(state, phase="stopped"), {"_help": "x"}, [], None,
                                      running=False, gpus=[], runtime="x")
        self.assertIn("Видеокарты: RTX", stopped)                       # from the state when Windows lists none
        self.assertIn("Озвучка: не запущена (в прошлый раз: stopped)", stopped)
        self.assertIn("Настройки не по умолчанию: нет", stopped)


class Install(unittest.TestCase):
    """bv.install: a release unpacked beside, made current through app/current, old ones pruned."""

    def release(self, d: Path, version: str) -> Path:
        import zipfile
        archive = d / f"barrens-voice-{version}.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("pyproject.toml", f'[project]\nname = "barrens-voice"\nversion = "{version}"\n')
            z.writestr("uv.lock", "version = 1\n")
            z.writestr("companion/BarrensVoice.pyw", "")
            z.writestr("companion/bv/__init__.py", "")
        return archive

    def test_install_switch_prune(self):
        from unittest import mock
        from bv import install
        from bv.paths import layout
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "BarrensVoice"
            with mock.patch("bv.install.build", side_effect=built) as build:
                for version in ("0.4.0", "0.4.1", "0.4.2"):
                    self.assertEqual(install.install(root, self.release(d, version), "cuda", d / "uv.exe", log=lambda *a: None), version)
            self.assertEqual(build.call_args[0][2], "cuda")
            self.assertEqual(install.current_version(root), "0.4.2")
            code = Path(os.path.realpath(root / "app" / "current" / "companion"))
            self.assertEqual(layout(code, {})["root"], root.resolve())          # the code knows it is installed
            self.assertEqual(install.prune(root), ["0.4.0"])                   # current + one to go back to
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.1", "0.4.2", "current"])
            self.assertFalse(any(p.name.endswith(".part") for p in (root / "app").iterdir()))

    def test_switch_never_leaves_no_current(self):
        # the new link is made beside and renamed in: a failure on the way keeps the old current
        from unittest import mock
        from bv import install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            with mock.patch("bv.install.make_link", side_effect=OSError("mklink failed")), self.assertRaises(OSError):
                install.point_current(root, root / "app" / "0.4.1")
            self.assertEqual(install.current_version(root), "0.4.0")
            real = os.rename

            def second_fails(a, b):
                if Path(a).name == "current.new":
                    raise OSError("in use")
                real(a, b)
            with mock.patch("os.rename", side_effect=second_fails), self.assertRaises(OSError):
                install.point_current(root, root / "app" / "0.4.1")
            self.assertEqual(install.current_version(root), "0.4.0")                     # renamed back
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current"])
            install.make_link(root / "app" / "current.old", root / "app" / "0.4.0")     # a swap cut short before
            install.point_current(root, root / "app" / "0.4.1")
            self.assertEqual(install.current_version(root), "0.4.1")
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current"])
            self.assertEqual(install.prune(root, keep=1), ["0.4.0"])                   # links are never pruned
            self.assertTrue((root / "app" / "0.4.1").is_dir())

    def test_failed_omnivoice_preparation_keeps_current_install(self):
        import subprocess
        from unittest import mock
        from bv import install, setup_omni
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            uv = root / "uv.exe"
            with mock.patch("bv.install.build", side_effect=built):
                install.install(root, self.release(root, "0.4.1"), "directml", uv)
                with (mock.patch("bv.setup_omni.subprocess.run", side_effect=subprocess.CalledProcessError(1, "bv")),
                      self.assertRaises(subprocess.CalledProcessError)):
                    setup_omni.prepare(root, self.release(root, "0.5.0"))
            self.assertEqual(install.current_version(root), "0.4.1")
            self.assertEqual(install.runtime_of(root), "directml")

    def test_prune_never_takes_a_newer_version(self):
        # by number alone a refused test build left on disk (newer) kept itself and pruned the prepared version
        # (newer than current too, lower than the build): the switch then pointed app\\current at nothing
        from bv import install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.3.8", "0.3.9", "0.4.0", "0.4.1", "0.4.2", "0.4.3", "0.5.0b1"):
                make_version(root, v)
            self.assertEqual(install.prune(root), [])                                   # no current: nothing goes
            install.point_current(root, root / "app" / "0.4.2")
            install.make_link(root / "app" / "current.old", root / "app" / "0.3.8")     # a switch cut short
            self.assertEqual(sorted(install.prune(root, also=["0.4.0", None])), ["0.3.9"])
            # kept: current, the newer ones (prepared 0.4.3, refused 0.5.0b1), the newest older one (0.4.1),
            # `also` (update.json's previous) and what current.old points at
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()),
                             ["0.3.8", "0.4.0", "0.4.1", "0.4.2", "0.4.3", "0.5.0b1", "current", "current.old"])

    def test_prune_takes_only_versions(self):
        # any folder in app\\ whose name is no version (a player's «backup», «Новая папка», a copy) sorted as the
        # oldest version, and went
        from bv import install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.3.0", "0.4.0b9", "0.4.0", "0.4.1"):
                make_version(root, v)
            app = root / "app"
            theirs = ("backup", "Новая папка", "0.4.0 - копия", "0.4.0rc1", "v0.3.9", "0.2.0.part", "x.pruned")
            for name in theirs:
                (app / name / "sub").mkdir(parents=True)
            (app / "0.1.0.pruned" / "sub").mkdir(parents=True)                       # a prune cut short: goes
            (app / "notes.txt").write_text("", encoding="utf-8")
            install.point_current(root, app / "0.4.1")
            self.assertEqual(sorted(install.prune(root)), ["0.3.0", "0.4.0b9"])
            self.assertEqual(sorted(p.name for p in app.iterdir()),
                             sorted(["0.4.0", "0.4.1", "current", "notes.txt", *theirs]))

    def test_prune_cut_short_leaves_no_half_version(self):
        # the launcher ends (a daemon thread) or a file is in use: never a half version under a version's name
        # (one the next update could take as its previous, or a repair as whole); the rest goes next time
        import shutil
        from unittest import mock
        from bv import install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.3.8", "0.3.9", "0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.1")
            real_rename = os.rename

            def in_use(a, b):
                if Path(a).name == "0.3.8":
                    raise PermissionError(32, "in use")
                real_rename(a, b)
            with mock.patch("shutil.rmtree"), mock.patch("os.rename", side_effect=in_use):
                self.assertEqual(install.prune(root), ["0.3.9"])                        # cut short: not removed
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()),
                             ["0.3.8", "0.3.9.pruned", "0.4.0", "0.4.1", "current"])
            self.assertTrue(install.whole(root / "app" / "0.3.8"))                       # in use: whole, kept
            self.assertEqual(install.prune(root), ["0.3.8"])                            # next time: both go
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current"])
            shutil.copytree(root / "app" / "0.4.0", root / "app" / "0.3.9.pruned")      # a leftover that stays
            make_version(root, "0.3.9")
            with mock.patch("shutil.rmtree"):
                self.assertEqual(install.prune(root), [])                               # its name taken: next time
            self.assertTrue(install.whole(root / "app" / "0.3.9"))

    def test_switch_refuses_a_folder_without_the_launcher(self):
        # pointed at a version gone (pruned) or half there, app\\current left nothing the starter could run
        from bv import install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_version(root, "0.4.0")
            install.point_current(root, root / "app" / "0.4.0")
            (root / "app" / "0.4.1" / "companion").mkdir(parents=True)               # no BarrensVoice.pyw
            for target in ("0.4.1", "0.4.2"):                                          # half there, not there
                with self.assertRaises(FileNotFoundError):
                    install.point_current(root, root / "app" / target)
            self.assertEqual(install.current_version(root), "0.4.0")
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current"])

    def test_junction_on_windows(self):
        # Windows only, so with fakes: _winapi.CreateJunction first; when it is missing or fails, mklink with
        # both paths quoted in one string (a folder named with "&" split the old list2cmdline command)
        import sys
        import types
        from unittest import mock
        from bv import install
        link, target = Path(r"C:\Tom & Jerry\BV\app\current.new"), Path(r"C:\Tom & Jerry\BV\app\0.4.1")
        made, ran = [], []
        ok = mock.patch.multiple("os.path", isjunction=lambda p: p in made, realpath=lambda p: str(target))
        winapi = types.SimpleNamespace(CreateJunction=lambda src, dst: made.append(Path(dst)))
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"_winapi": winapi}), ok, \
                mock.patch("subprocess.run", side_effect=lambda *a, **k: ran.append(a[0])):
            install.make_link(link, target)
        self.assertEqual((made, ran), ([link], []))
        made.clear()

        def fails(src, dst):
            raise OSError("no")

        def mklink(command, **k):
            ran.append(command)
            made.append(link)
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"_winapi": types.SimpleNamespace(CreateJunction=fails)}), \
                ok, mock.patch("os.path.isdir", return_value=False), mock.patch("subprocess.run", side_effect=mklink):
            install.make_link(link, target)
        self.assertEqual(ran, [f'cmd /c mklink /J "{link}" "{target}"'])
        made.clear()
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"_winapi": types.SimpleNamespace()}), \
                ok, mock.patch("os.path.isdir", return_value=False), mock.patch("subprocess.run"), self.assertRaises(OSError):
            install.make_link(link, target)                                             # nothing made: an error


class Update(unittest.TestCase):
    """bv.update: only a signed manifest, only an archive matching it, back to the previous on early failures."""

    def keys(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        raw = serialization.Encoding.Raw
        public = base64.b64encode(key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)).decode()
        return key, public

    def test_signature(self):
        import base64
        from bv import update
        key, public = self.keys()
        manifest = b'{"version": "0.4.1"}'
        sig = base64.b64encode(key.sign(manifest)).decode()
        self.assertTrue(update.verify(manifest, sig, public))
        self.assertFalse(update.verify(manifest.replace(b"0.4.1", b"0.4.2"), sig, public))   # changed: refused
        self.assertFalse(update.verify(manifest, sig, self.keys()[1]))                     # another key: refused
        self.assertFalse(update.verify(manifest, "not base64!", public))

    def test_download_checks_the_archive(self):
        import hashlib
        import io
        from bv import update
        data = b"release bytes" * 1000

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        manifest = {"base": "https://x/", "archive": "barrens-voice-0.4.1.zip", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
        with tempfile.TemporaryDirectory() as d:
            got = update.download(manifest, Path(d), fetch_stream=lambda *a, **k: Response(data))
            self.assertEqual(got.read_bytes(), data)
            with self.assertRaises(ValueError):
                update.download({**manifest, "sha256": "0" * 64}, Path(d), fetch_stream=lambda *a, **k: Response(data))
            self.assertFalse((Path(d) / "barrens-voice-0.4.1.part").exists())

    def test_back_to_the_previous_after_two_early_failures(self):
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            update.apply(root, "0.4.1")
            self.assertEqual(install.current_version(root), "0.4.1")
            self.assertIsNone(update.record_start(root, 5, failed=True))                  # one failure: wait
            self.assertEqual(update.record_start(root, 5, failed=True), "0.4.0")         # two: back
            self.assertEqual(install.current_version(root), "0.4.0")
            self.assertEqual(update.refused(root), "0.4.1")                               # remembered
            update.apply(root, "0.4.1")
            self.assertIsNone(update.record_start(root, 300, failed=True))               # ran a while: not a start failure
            self.assertFalse(update.state_path(root).exists())
            update.pruning.join(10)

    def test_no_rollback_to_a_version_not_there(self):
        # the previous version gone or half removed: no rollback onto it (a rollback that raised, every start)
        import shutil
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            update.apply(root, "0.4.1")
            shutil.rmtree(root / "app" / "0.4.0" / "companion")
            for _ in range(3):
                self.assertIsNone(update.record_start(root, 5, failed=True))
            self.assertEqual(install.current_version(root), "0.4.1")
            self.assertIsNone(update.refused(root))

    def test_no_way_back_is_said_once(self):
        # the previous version not whole: the failures only grew, and nothing said that going back is impossible
        import shutil
        from types import SimpleNamespace
        from unittest import mock
        from bv import install, update
        from bv.launcher import app
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            update.apply(root, "0.4.1")
            shutil.rmtree(root / "app" / "0.4.0" / "companion")
            said = []
            win = SimpleNamespace(companion_started=1.0, companion_game=None, running_game=None, resident=False,
                                  say=lambda m, level="INFO": said.append((level, m)), game=lambda key: None,
                                  companion=SimpleNamespace(state=dict, running=False), config={},
                                  tray=SimpleNamespace(notify=lambda m: said.append(("tray", m))))
            win.start_failed = lambda code: app.App.start_failed(win, code)
            with mock.patch.object(app, "INSTALL_DIR", root), mock.patch.object(app.time, "monotonic", return_value=6.0):
                for _ in range(4):
                    app.App.companion_ended(win, 1)
            stuck = [m for level, m in said if level == "ERROR" and "вернуться не на что" in m]
            self.assertEqual(stuck, ["версия 0.4.1 не запускается, а вернуться не на что: версии 0.4.0 нет целиком "
                                     "— установи Barrens Voice заново"])
            self.assertEqual(json.loads(update.state_path(root).read_text(encoding="utf-8"))["fails"], 4)
            self.assertEqual(install.current_version(root), "0.4.1")
            update.state_path(root).write_text(json.dumps({"version": "0.4.1", "previous": None, "fails": 1}))
            told = []
            self.assertIsNone(update.record_start(root, 5, failed=True, say=told.append))
            self.assertIsNone(update.record_start(root, 5, failed=True, say=told.append))
            self.assertEqual(told, ["версия 0.4.1 не запускается, а вернуться не на что: прежней версии нет "
                                    "— установи Barrens Voice заново"])

    def test_switch_watched_from_before_it(self):
        # update.json is written before app\\current moves: a switch is never left unwatched, a failed one leaves
        # no watch, an error after it (the old link in use) does not undo it, a retry keeps the version before
        from unittest import mock
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            with mock.patch("bv.update.write_json", side_effect=PermissionError("denied")), self.assertRaises(OSError):
                update.apply(root, "0.4.1")
            self.assertEqual(install.current_version(root), "0.4.0")                     # no watch: no switch
            seen = []

            def switch(root_, version_dir):
                seen.append(json.loads(update.state_path(root).read_text(encoding="utf-8")))
                raise OSError("mklink failed")
            with mock.patch("bv.install.point_current", side_effect=switch), self.assertRaises(OSError):
                update.apply(root, "0.4.1")
            self.assertEqual(seen, [{"version": "0.4.1", "previous": "0.4.0", "fails": 0}])   # there during it
            self.assertFalse(update.state_path(root).exists())                              # gone with the failure
            self.assertEqual(install.current_version(root), "0.4.0")
            real, calls = install.remove_link, []

            def old_in_use(path):                       # the second removal of current.old: the one after the switch
                calls.append(Path(path).name)
                if calls.count("current.old") == 2:
                    raise PermissionError("in use")
                real(path)
            with mock.patch("bv.install.remove_link", side_effect=old_in_use):
                update.apply(root, "0.4.1")                                               # no error: switched
            self.assertEqual(install.current_version(root), "0.4.1")
            self.assertTrue(os.path.lexists(root / "app" / "current.old"))                 # left for next time
            update.apply(root, "0.4.1")                                                   # a retry: watch kept
            self.assertEqual(json.loads(update.state_path(root).read_text(encoding="utf-8"))["previous"], "0.4.0")
            install.point_current(root, root / "app" / "0.4.1")                           # the leftover goes next time
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current"])

    def test_cut_between_the_renames(self):
        # killed between current -> current.old and current.new -> current: the starter runs current.new (the new
        # version, watched); its failed starts still go back, and the leftovers are cleaned
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            update.state_path(root).parent.mkdir(parents=True)
            update.write_json(update.state_path(root), {"version": "0.4.1", "previous": "0.4.0", "fails": 0})
            os.rename(root / "app" / "current", root / "app" / "current.old")
            install.make_link(root / "app" / "current.new", root / "app" / "0.4.1")
            self.assertIsNone(update.record_start(root, 5, failed=True))
            self.assertEqual(update.record_start(root, 5, failed=True), "0.4.0")
            self.assertEqual(install.current_version(root), "0.4.0")
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current"])

    def test_launcher_start_repairs_a_cut_switch(self):
        # cut between the renames: only current.new and current.old, the starter runs current.new. The launcher's
        # start makes app\\current again (the next update recorded no version to go back to), and the watch holds
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.3.9", "0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            self.assertFalse(install.repair_current(root, root / "app" / "0.4.0"))      # current there: nothing
            update.state_path(root).parent.mkdir(parents=True)
            update.write_json(update.state_path(root), {"version": "0.4.1", "previous": "0.4.0", "fails": 0})
            os.rename(root / "app" / "current", root / "app" / "current.old")
            install.make_link(root / "app" / "current.new", root / "app" / "0.4.1")
            self.assertFalse(install.repair_current(root, root / "app" / "0.3.9"))      # not what the links name
            self.assertIsNone(install.current_version(root))
            self.assertTrue(install.repair_current(root, root / "app" / "current.new" / "companion" / ".."))
            self.assertEqual(install.current_version(root), "0.4.1")
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.3.9", "0.4.0", "0.4.1", "current"])
            self.assertIsNone(update.record_start(root, 5, failed=True))                 # still watched:
            self.assertEqual(update.record_start(root, 5, failed=True), "0.4.0")        # its failures go back
            # the starter ran current.old (current.new not whole): current is the old version, and the watch of
            # the switch that did not happen is dropped at the next end
            update.write_json(update.state_path(root), {"version": "0.4.1", "previous": "0.4.0", "fails": 0})
            os.rename(root / "app" / "current", root / "app" / "current.old")
            install.make_link(root / "app" / "current.new", root / "app" / "0.4.1")
            self.assertTrue(install.repair_current(root, root / "app" / "0.4.0"))
            self.assertEqual(install.current_version(root), "0.4.0")
            self.assertIsNone(update.record_start(root, 5, failed=True))
            self.assertFalse(update.state_path(root).exists())
            self.assertEqual(install.current_version(root), "0.4.0")

    def test_rollback_cut_short_is_remembered(self):
        # the launcher killed between the rollback's two renames: the refusal came after the switch, so the failed
        # version was not remembered and was prepared again by itself. The starter runs current.new (the previous
        # version), whose start repairs app\\current and drops the watch of the failed one at its end
        from unittest import mock
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            update.apply(root, "0.4.1")
            update.record_start(root, 5, failed=True)
            real = os.rename

            def killed(a, b):
                if Path(a).name == "current.new":
                    raise SystemExit("killed")                     # nothing of point_current's own clean-up runs
                real(a, b)
            with mock.patch("os.rename", side_effect=killed), self.assertRaises(SystemExit):
                update.record_start(root, 5, failed=True)
            self.assertEqual(update.refused(root), "0.4.1")
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current.new", "current.old"])
            self.assertTrue(install.repair_current(root, root / "app" / "0.4.0"))
            self.assertIsNone(update.record_start(root, 5, failed=True))           # the previous one's end
            self.assertFalse(update.state_path(root).exists())
            self.assertEqual((install.current_version(root), update.refused(root)), ("0.4.0", "0.4.1"))

    def test_watch_of_a_switch_that_did_not_happen(self):
        # update.json names another version than current (the launcher killed after writing it, before the
        # switch): the current version's failures are not the other's - forgotten, nothing refused
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            update.state_path(root).parent.mkdir(parents=True)
            update.write_json(update.state_path(root), {"version": "0.4.1", "previous": "0.4.0", "fails": 1})
            self.assertIsNone(update.record_start(root, 5, failed=True))
            self.assertFalse(update.state_path(root).exists())
            self.assertIsNone(update.refused(root))
            self.assertEqual(install.current_version(root), "0.4.0")

    def test_config_error_does_not_go_back(self):
        # exit 3 (start again online), 4 (another companion), 5 (config.json cannot be read), an error the run
        # explained: no verdict - no rollback, but nothing settled either (update.json kept, nothing pruned), so
        # the version's own failed starts after them still send the launcher back
        from types import SimpleNamespace
        from unittest import mock
        from bv import install, update
        from bv.launcher import app
        explained = {"phase": "error", "error": "аддон не установлен", "pid": 7}
        for code, state in ((3, {}), (4, {}), (5, {}), (1, explained)):
            win = SimpleNamespace(companion=SimpleNamespace(state=lambda s=state: s, proc=SimpleNamespace(pid=7)))
            with self.subTest(code=code), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                for v in ("0.3.9", "0.4.0", "0.4.1"):
                    make_version(root, v)
                install.point_current(root, root / "app" / "0.4.0")
                update.apply(root, "0.4.1")
                with mock.patch("bv.install.prune") as prune:
                    for ran_for in (5, 5, 3600):                                       # however long it ran
                        self.assertIsNone(update.record_start(root, ran_for, failed=app.App.start_failed(win, code)))
                prune.assert_not_called()
                self.assertTrue(update.state_path(root).is_file())                   # still watched
                self.assertEqual(install.current_version(root), "0.4.1")
                self.assertIsNone(update.refused(root))
                self.assertIsNone(update.record_start(root, 5, failed=True))           # then it really fails
                self.assertEqual(update.record_start(root, 5, failed=True), "0.4.0")  # twice: back

    def test_settled_start_prunes(self):
        import threading
        from unittest import mock
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1", "0.4.2"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.1")
            update.apply(root, "0.4.2")
            self.assertIsNone(update.record_start(root, 5, failed=True))                 # not settled: all kept
            self.assertEqual(len(list((root / "app").iterdir())), 4)
            go, real = threading.Event(), install.prune
            with mock.patch("bv.install.prune", side_effect=lambda r, **k: go.wait(10) and real(r, **k)):
                self.assertIsNone(update.record_start(root, 5, failed=False))            # a good start: settled -
                self.assertEqual(len(list((root / "app").iterdir())), 4)                 # returned before the prune
                go.set()                                                                 # (off the window's thread)
                update.pruning.join(10)
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.1", "0.4.2", "current"])

    def test_start_prunes_unless_watched(self):
        # a few seconds after every launcher start (the settle's own prune is cut short when the launcher closes
        # with the game right after it), but not while a version is watched: its next start decides
        from types import SimpleNamespace
        from unittest import mock
        from bv import install, update
        from bv.launcher import app
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.3.9", "0.4.0", "0.4.1"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.0")
            update.apply(root, "0.4.1")
            self.assertIsNone(update.prune_unwatched(root))
            self.assertEqual(len(list((root / "app").iterdir())), 4)
            update.state_path(root).unlink()
            with mock.patch.object(app, "INSTALL_DIR", root):
                with mock.patch("bv.update.prune_unwatched") as later:
                    app.App.prune_versions(SimpleNamespace(closed=True))                  # closed meanwhile: nothing
                later.assert_not_called()
                app.App.prune_versions(SimpleNamespace(closed=False))
            update.pruning.join(10)
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "current"])

    def test_prune_reads_the_watch_after_current(self):
        # the prune read update.json, then (after clearing *.pruned leftovers: seconds) app\\current. An update
        # applied in between (its watch written before the switch) was seen as current with no watch: the version
        # to go back to went when a newer one (refused here, say) stood between. And while watched, its previous
        # and itself are kept whatever their number
        from unittest import mock
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1", "0.4.2", "0.4.3"):    # 0.4.1 current, 0.4.2 refused, 0.4.3 its fix, ready
                make_version(root, v)
            (root / "app" / "0.3.9.pruned").mkdir()
            install.point_current(root, root / "app" / "0.4.1")
            real, applied = install.current_version, []

            def switched_now(r):                              # the window's thread applies 0.4.3 right here
                if not applied:
                    applied.append(1)
                    update.apply(root, "0.4.3")
                return real(r)
            with mock.patch("bv.install.current_version", side_effect=switched_now):
                update.prune_unwatched(root).join(10)
            self.assertEqual(install.current_version(root), "0.4.3")
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.1", "0.4.2", "0.4.3", "current"])
            self.assertEqual(update.record_start(root, 5, failed=True), None)
            self.assertEqual(update.record_start(root, 5, failed=True), "0.4.1")       # and back it goes
            install.point_current(root, root / "app" / "0.4.3")
            make_version(root, "0.4.0")
            update.write_json(update.state_path(root), {"version": "0.4.0", "previous": "0.4.1", "fails": 0})
            update.prune_later(root).join(10)                                          # an odd watch: kept all the same
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.0", "0.4.1", "0.4.2", "0.4.3", "current"])

    def test_a_refused_version_is_none_to_go_back_to(self):
        # back from R to P, then N: R (forgotten for updating once N came out) stayed as the newest older version
        # and the prune took P, the one that worked
        from bv import install, update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("0.4.0", "0.4.1", "0.4.2", "0.4.3"):
                make_version(root, v)
            install.point_current(root, root / "app" / "0.4.1")
            update.apply(root, "0.4.2")
            update.record_start(root, 5, failed=True)
            self.assertEqual(update.record_start(root, 5, failed=True), "0.4.1")
            update.refuse(root, None)                                  # 0.4.3 came out (Updater.check)
            self.assertIsNone(update.refused(root))
            self.assertEqual(update.refused_versions(root), {"0.4.2"})
            update.apply(root, "0.4.3")
            update.record_start(root, 3600, failed=False)
            update.pruning.join(10)
            update.prune_unwatched(root).join(10)                       # the next start's
            self.assertEqual(sorted(p.name for p in (root / "app").iterdir()), ["0.4.1", "0.4.3", "current"])
            update.refuse(root, "0.4.4")
            update.refuse(root, "0.4.5")
            self.assertEqual((update.refused(root), update.refused_versions(root)), ("0.4.5", {"0.4.2", "0.4.4", "0.4.5"}))
            self.assertEqual(json.loads(update.refused_path(root).read_text(encoding="utf-8"))["version"], "0.4.5")
            update.refused_path(root).unlink()
            update.refuse(root, None)                                  # nothing to forget: nothing written
            self.assertFalse(update.refused_path(root).exists())

    def test_version_runs(self):
        # the new version's own Python must import the launcher and the companion (a broken launcher could never
        # update itself again); here the test's Python and this repository stand in for the version's
        import sys
        from bv import update
        repo = Path(__file__).resolve().parents[2]
        self.assertIsNone(update.version_runs(repo, python=Path(sys.executable)))
        with tempfile.TemporaryDirectory() as d:
            bv = Path(d) / "companion" / "bv"
            (bv / "launcher").mkdir(parents=True)
            (bv / "__init__.py").write_text("", encoding="utf-8")
            (bv / "launcher" / "__init__.py").write_text("", encoding="utf-8")
            (bv / "launcher" / "app.py").write_text("import no_such_module\n", encoding="utf-8")
            why = update.version_runs(Path(d), python=Path(sys.executable))
            self.assertIn("no_such_module", why)                                       # no selfcheck: the fixed list
            # with bv/selfcheck.py the version decides: here it moved its launcher and checks it where it is now
            (bv / "selfcheck.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
            self.assertIsNone(update.version_runs(Path(d), python=Path(sys.executable)))
            (bv / "selfcheck.py").write_text("raise SystemExit('нет модуля голосов')\n", encoding="utf-8")
            self.assertEqual(update.version_runs(Path(d), python=Path(sys.executable)), "нет модуля голосов")
            with self.assertRaises(RuntimeError):                                       # no environment: the check
                update.version_runs(Path(d))                                            # could not run, not a verdict


class WindowCode(unittest.TestCase):
    """What the tests cannot draw: the window's snapshot and its unpacking in redraw must agree (an item
    added to one and not the other broke the launcher at its first draw, 0.4.0-beta.3)."""

    def test_snapshot_matches_redraw(self):
        import ast
        tree = ast.parse((Path(__file__).resolve().parents[1] / "bv" / "launcher" / "app.py").read_text(encoding="utf-8"))
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        made = [n.value for n in ast.walk(funcs["snapshot"]) if isinstance(n, ast.Return)][-1]
        taken = next(n.targets[0] for n in ast.walk(funcs["redraw"])
                     if isinstance(n, ast.Assign) and isinstance(n.value, ast.Name) and n.value.id == "snap"
                     and isinstance(n.targets[0], ast.Tuple))
        self.assertEqual(len(made.elts), len(taken.elts))


class Wrap(unittest.TestCase):
    """The main window's log: a line that does not fit is wrapped under its time, not cut."""

    def test_wrap(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no display")
        try:
            from bv.launcher.paint import Painter
            p = Painter(tk.Canvas(root), "TkDefaultFont")
            short = "03:12:01 связь с аддоном есть"
            self.assertEqual(p.wrap(short, p.mono, 412, " " * 9), [short])
            long = "03:12:01 " + "слово " * 40
            lines = p.wrap(long.strip(), p.mono, 412, " " * 9)
            self.assertGreater(len(lines), 1)
            self.assertTrue(all(x.startswith(" " * 9) for x in lines[1:]))
            self.assertTrue(all(p.measure(x, p.mono) <= 412 for x in lines))
            self.assertEqual(" ".join(x.strip() for x in lines), long.strip())     # nothing lost
        finally:
            root.destroy()


class Starter(unittest.TestCase):
    """Barrens Voice.exe comes with each release and follows the current version (installs and updates)."""

    def test_follows_the_version(self):
        import zipfile
        from unittest import mock
        from bv import install, update

        def release(d, version, exe):
            archive = d / f"barrens-voice-{version}.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("pyproject.toml", f'[project]\nname = "barrens-voice"\nversion = "{version}"\n')
                z.writestr("uv.lock", "version = 1\n")
                z.writestr("companion/BarrensVoice.pyw", "")
                if exe is not None:
                    z.writestr("Barrens Voice.exe", exe)
            return archive
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "BarrensVoice"
            starter = root / "Barrens Voice.exe"
            with mock.patch("bv.install.build", side_effect=built), mock.patch("bv.update.version_runs", return_value=None):
                install.install(root, release(d, "0.4.0", b"v1"), "cuda", d / "uv.exe", log=lambda *a: None)
                self.assertEqual(starter.read_bytes(), b"v1")                       # put there by the install
                v = update.prepare(root, release(d, "0.4.1", b"v2"))
                update.apply(root, v)
                self.assertEqual(starter.read_bytes(), b"v2")                       # an update brings the new one
                v = update.prepare(root, release(d, "0.4.2", None))
                update.apply(root, v)
                self.assertEqual(starter.read_bytes(), b"v2")                       # a release without it: kept


    def test_one_definition_of_a_version_that_starts(self):
        # install.whole asked only for the launcher's script, the starter's Starts for pythonw.exe too: a version
        # pointed at, or gone back to, without its environment left «установлен не до конца»
        import re
        from bv import install
        source = (Path(__file__).resolve().parents[2] / "installer" / "launcher.cs").read_text(encoding="utf-8")
        body = re.search(r"static bool Starts\(string version\)\s*\{(.*?)\n    \}", source, re.S).group(1)
        checked = {tuple(re.findall(r'"([^"]+)"', args)) for args in re.findall(r"Path\.Combine\(version, ([^)]*)\)", body)}
        self.assertEqual(checked, {f.parts for f in install.STARTS})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_version(root, "0.4.0")
            install.point_current(root, root / "app" / "0.4.0")
            (make_version(root, "0.4.1") / ".venv" / "Scripts" / "pythonw.exe").unlink()     # never built
            self.assertFalse(install.whole(root / "app" / "0.4.1"))
            with self.assertRaises(FileNotFoundError) as refused:
                install.point_current(root, root / "app" / "0.4.1")
            self.assertIn("pythonw.exe", str(refused.exception))
            self.assertEqual(install.current_version(root), "0.4.0")


class ReleaseUv(unittest.TestCase):
    """The uv a release names: kept when installed already, else fetched, checked and put in place after the build."""

    def test_kept_fetched_refused(self):
        import hashlib
        import io
        import zipfile
        from unittest import mock
        from bv import update
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("uv.exe", b"new uv")
        data = buf.getvalue()
        want = {"uv": {"version": "0.14.0", "url": "https://x/uv.zip", "sha256": hashlib.sha256(data).hexdigest()}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "uv.exe").write_bytes(b"old uv")
            with mock.patch("bv.update.uv_version", return_value="0.14.0"):
                self.assertEqual(update.release_uv(root, want, fetch=lambda u: data), root / "uv.exe")   # same: kept
            with mock.patch("bv.update.uv_version", return_value="0.13.7"):
                fresh = update.release_uv(root, want, log=lambda *a: None, fetch=lambda u: data)
                self.assertEqual((fresh.name, fresh.read_bytes()), ("uv.new.exe", b"new uv"))
                with self.assertRaises(ValueError):                                                # tampered: refused
                    update.release_uv(root, {"uv": {**want["uv"], "sha256": "0" * 64}}, log=lambda *a: None,
                                      fetch=lambda u: data)
            self.assertEqual(update.release_uv(root, {}), root / "uv.exe")                          # an old manifest


class UvInPrepare(unittest.TestCase):
    """prepare() with a release naming another uv: replaced after a good build, kept after a failed one."""

    def setUp(self):
        import hashlib
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("uv.exe", b"new uv")
        self.data = buf.getvalue()
        self.manifest = {"uv": {"version": "0.14.0", "url": "https://x/uv.zip",
                                "sha256": hashlib.sha256(self.data).hexdigest()}}

    def release(self, d: Path) -> Path:
        import zipfile
        archive = d / "barrens-voice-0.4.1.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("pyproject.toml", '[project]\nname = "barrens-voice"\nversion = "0.4.1"\n')
            z.writestr("uv.lock", "version = 1\n")
            z.writestr("companion/BarrensVoice.pyw", "")
        return archive

    def run_prepare(self, build):
        from unittest import mock
        from bv import update
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "BarrensVoice"
            (root / "app").mkdir(parents=True)
            (root / "uv.exe").write_bytes(b"old uv")
            with mock.patch("bv.update.uv_version", return_value="0.13.7"), \
                 mock.patch("bv.update.get", return_value=self.data), \
                 mock.patch("bv.install.build", side_effect=build), \
                 mock.patch("bv.update.version_runs", return_value=None):
                try:
                    update.prepare(root, self.release(Path(d)), log=lambda *a: None, manifest=self.manifest)
                except RuntimeError:
                    pass
            return (root / "uv.exe").read_bytes(), (root / "uv.new.exe").exists()

    def test_good_build_replaces_uv(self):
        used = []
        uv, leftover = self.run_prepare(lambda root, folder, runtime, uv, log: used.append(uv.name))
        self.assertEqual(used, ["uv.new.exe"])                     # the version was built with the new uv
        self.assertEqual((uv, leftover), (b"new uv", False))

    def test_failed_build_keeps_the_old_uv(self):
        def fail(*a):
            raise RuntimeError("uv sync failed")
        self.assertEqual(self.run_prepare(fail), (b"old uv", False))

    def test_uv_version_parsed(self):
        from unittest import mock
        from bv import update
        done = mock.Mock(stdout="uv 0.13.7 (316c827e0 2026-11-03 x86_64-pc-windows-msvc)\n")
        with mock.patch("subprocess.run", return_value=done):
            self.assertEqual(update.uv_version(Path("uv.exe")), "0.13.7")
        with mock.patch("subprocess.run", side_effect=OSError("missing")):
            self.assertIsNone(update.uv_version(Path("uv.exe")))


class UpdaterFlow(unittest.TestCase):
    """The launcher's whole update flow (bv.update.Updater) against a fake GitHub: releases signed with a
    test key, the environment build stubbed. What breaks here breaks every player's updates."""

    def setUp(self):
        import base64
        import io
        from unittest import mock
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.key = Ed25519PrivateKey.generate()
        raw = serialization.Encoding.Raw
        self.public = base64.b64encode(self.key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)).decode()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.root = self.d / "BarrensVoice"
        make_version(self.root, "0.4.0")
        (self.root / "uv.exe").write_bytes(b"uv")
        from bv import install
        install.point_current(self.root, self.root / "app" / "0.4.0")
        self.files: dict[str, bytes] = {}
        self.said: list[tuple[str, str]] = []
        self.settings = {"update_channel": "stable", "auto_update": True}
        self.build = mock.patch("bv.install.build", side_effect=built).start()
        self.runs = mock.patch("bv.update.version_runs", return_value=None).start()   # no environment built here
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)

        class Stream(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        self.Stream = Stream

    def publish(self, base: str, version: str, signed_with=None, archive_bytes=None) -> None:
        """A release at `base`: its archive, manifest and signature, as GitHub serves them."""
        import base64
        import hashlib
        import io
        import json
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("pyproject.toml", f'[project]\nname = "barrens-voice"\nversion = "{version}"\n')
            z.writestr("uv.lock", "version = 1\n")
            z.writestr("companion/BarrensVoice.pyw", "")
        data = archive_bytes or buf.getvalue()
        name = f"barrens-voice-{version}.zip"
        manifest = json.dumps({"version": version, "addon": "0.3.0", "protocol": 1, "archive": name,
                               "size": len(buf.getvalue()), "sha256": hashlib.sha256(buf.getvalue()).hexdigest()}).encode()
        self.files[base + name] = data
        self.files[base + "manifest.json"] = manifest
        self.files[base + "manifest.json.sig"] = base64.b64encode((signed_with or self.key).sign(manifest))

    def fetch(self, url):
        if url not in self.files:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.files[url]

    def updater(self, current="0.4.0"):
        from bv import update
        return update.Updater(self.root, current, lambda: self.settings, lambda m, level="INFO": self.said.append((level, m)),
                              fetch=self.fetch, fetch_stream=lambda req, timeout=0: self.Stream(self.fetch(req.full_url)),
                              public=self.public)

    def test_found_prepared_applied_when_idle(self):
        from bv import install, update
        self.publish(update.STABLE, "0.4.1")
        u = self.updater()
        self.assertEqual(u.check()["version"], "0.4.1")
        self.assertEqual((u.status, u.ready), ("ready", "0.4.1"))
        self.assertTrue((self.root / "app" / "0.4.1" / "uv.lock").is_file())       # unpacked beside
        self.assertEqual(install.current_version(self.root), "0.4.0")              # not current yet
        self.assertFalse(u.due(game_running=True, companion_running=False, busy=False))
        self.assertFalse(u.due(game_running=False, companion_running=True, busy=False))
        self.assertFalse(u.due(game_running=False, companion_running=False, busy=True))
        self.assertTrue(u.due(game_running=False, companion_running=False, busy=False))
        self.assertEqual(u.apply(), "0.4.1")
        self.assertEqual(install.current_version(self.root), "0.4.1")
        self.assertTrue(update.state_path(self.root).is_file())                    # watched for a failed start
        self.assertIsNone(u.ready)

    def test_settle_keeps_the_prepared_version(self):
        # a refused test build left on disk (0.5.0b1), 0.4.1 applied and watched, 0.4.2 prepared meanwhile: the
        # settle of 0.4.1 prunes, and the prepared 0.4.2 must still be there to switch to
        from bv import install, update
        make_version(self.root, "0.5.0b1")
        update.refuse(self.root, "0.5.0b1")
        make_version(self.root, "0.4.1")
        update.apply(self.root, "0.4.1")
        self.publish(update.STABLE, "0.4.2")
        u = self.updater("0.4.1")
        u.check()
        self.assertEqual(u.ready, "0.4.2")
        update.record_start(self.root, 3600, failed=False)
        update.pruning.join(10)
        self.assertEqual(sorted(p.name for p in (self.root / "app").iterdir()),
                         ["0.4.0", "0.4.1", "0.4.2", "0.5.0b1", "current"])
        self.assertEqual(u.apply(), "0.4.2")
        self.assertEqual(install.current_version(self.root), "0.4.2")

    def test_nothing_newer(self):
        from bv import update
        for version in ("0.4.0", "0.3.9", "0.4.0b9"):                               # same, older, its own beta
            self.files.clear()
            self.publish(update.STABLE, version)
            self.assertIsNone(self.updater().check(), version)
        self.build.assert_not_called()

    def test_bad_signature_refused(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from bv import update
        self.publish(update.STABLE, "0.4.1", signed_with=Ed25519PrivateKey.generate())
        u = self.updater()
        self.assertIsNone(u.check())
        self.assertIn("WARN", [level for level, _ in self.said])
        self.assertFalse((self.root / "app" / "0.4.1").exists())

    def test_tampered_archive_refused_and_tried_again(self):
        from bv import update
        self.publish(update.STABLE, "0.4.1", archive_bytes=b"not what the manifest says")
        u = self.updater()
        u.check()
        self.assertEqual(u.status, "error")
        self.assertFalse((self.root / "app" / "0.4.1").exists())
        self.publish(update.STABLE, "0.4.1")                                         # fixed upstream
        u.check()                                                                    # an error does not block the next try
        self.assertEqual(u.status, "ready")

    def test_no_release_yet_is_quiet(self):
        self.assertIsNone(self.updater().check())                                    # 404: only pre-releases so far
        self.assertEqual(self.said, [])

    def test_manual_when_automatic_is_off(self):
        from bv import update
        self.settings["auto_update"] = False
        self.publish(update.STABLE, "0.4.1")
        u = self.updater()
        self.assertEqual(u.check()["version"], "0.4.1")
        self.assertEqual(u.status, "")                                               # only told
        self.assertEqual(u.prepare(), "0.4.1")                                       # «Обновить»

    def test_beta_channel_picks_the_newest_published(self):
        import json
        from bv import update
        self.settings["update_channel"] = "beta"
        base = f"https://github.com/{update.REPO}/releases/download/"
        self.publish(base + "v0.4.1-beta.2/", "0.4.1b2")
        self.publish(base + "v0.4.0/", "0.4.0")
        listing = [{"tag_name": "v0.4.1-beta.3", "draft": True, "assets": [{"name": "manifest.json"}, {"name": "manifest.json.sig"}]},
                   {"tag_name": "v0.4.1-beta.9", "draft": False, "assets": []},     # no manifest (a broken upload)
                   {"tag_name": "v0.4.1-beta.2", "draft": False, "assets": [{"name": "manifest.json"}, {"name": "manifest.json.sig"}]},
                   {"tag_name": "v0.4.0", "draft": False, "assets": [{"name": "manifest.json"}, {"name": "manifest.json.sig"}]}]
        self.files[update.RELEASES] = json.dumps(listing).encode()
        u = self.updater()
        self.assertEqual(u.check()["version"], "0.4.1b2")
        self.assertEqual(u.url(), f"https://github.com/{update.REPO}/releases/tag/v0.4.1-beta.2")

    def test_beta_channel_in_githubs_order(self):
        # GitHub lists releases by the tag as text: beta.9 ... beta.2, beta.10, beta.1 - the highest version wins
        import json
        from bv import update
        self.settings["update_channel"] = "beta"
        base = f"https://github.com/{update.REPO}/releases/download/"
        self.publish(base + "v0.4.0-beta.10/", "0.4.0b10")
        has = [{"name": "manifest.json"}, {"name": "manifest.json.sig"}]
        listing = [{"tag_name": f"v0.4.0-beta.{n}", "draft": False, "assets": has} for n in (9, 8, 2, 10, 1)]
        self.files[update.RELEASES] = json.dumps(listing).encode()
        self.assertEqual(self.updater(current="0.4.0b9").check()["version"], "0.4.0b10")

    def test_relaunch_command(self):
        from bv.update import relaunch_command
        self.assertEqual(relaunch_command('"C:\\BV\\Barrens Voice.exe"', resident=True),
                         'cmd /c ping -n 3 127.0.0.1 >nul & start "" "C:\\BV\\Barrens Voice.exe" --tray')
        self.assertTrue(relaunch_command('"x.exe"', resident=False).endswith('start "" "x.exe"'))

    def test_archive_removed_after_prepare(self):
        from bv import update
        downloads = self.root / "cache" / "downloads"
        downloads.mkdir(parents=True)
        (downloads / "barrens-voice-0.3.9.zip").write_bytes(b"an old one")
        self.publish(update.STABLE, "0.4.1")
        self.updater().check()
        self.assertEqual(list(downloads.iterdir()), [])

    def test_version_that_does_not_start_refused(self):
        from bv import update
        self.runs.return_value = "ModuleNotFoundError: No module named 'tkinter'"
        self.publish(update.STABLE, "0.4.1")
        u = self.updater()
        u.check()
        self.assertEqual((u.status, u.ready), ("error", None))
        self.assertEqual(update.refused(self.root), "0.4.1")
        self.assertTrue(any("tkinter" in m for _, m in self.said))
        self.build.reset_mock()
        self.assertEqual(self.updater().check()["version"], "0.4.1")
        self.build.assert_not_called()                                                 # not rebuilt every hour

    def test_check_that_could_not_run_refuses_nothing(self):
        # a timeout or an OSError of the check (an antivirus scan, a slow disk) is not the version's: tried again
        from bv import update
        self.runs.side_effect = RuntimeError("не получилось проверить, запускается ли версия: timed out")
        self.publish(update.STABLE, "0.4.1")
        u = self.updater()
        u.check()
        self.assertEqual((u.status, u.ready), ("error", None))
        self.assertIsNone(update.refused(self.root))
        self.runs.side_effect = None
        u.check()
        self.assertEqual((u.status, u.ready), ("ready", "0.4.1"))

    def test_only_the_refused_version_is_refused(self):
        # a tester sent back from 0.5.0b1 who leaves the test channel: the stable 0.4.2 installs as usual
        from bv import update
        update.refuse(self.root, "0.5.0b1")
        self.publish(update.STABLE, "0.4.2")
        u = self.updater()
        self.assertEqual(u.check()["version"], "0.4.2")
        self.assertEqual((u.status, u.ready), ("ready", "0.4.2"))
        self.assertFalse(any("не запустилась" in m for _, m in self.said))
        self.assertEqual(update.refused(self.root), "0.5.0b1")                         # kept for the test channel
        for text in ("", "[]", '"0.4.2"', '{"version": 42}', '{"version": null}'):      # a damaged file: nothing refused
            update.refused_path(self.root).write_text(text, encoding="utf-8")
            self.assertIsNone(update.refused(self.root), text)

    def test_rolled_back_version_not_prepared_again(self):
        # two failed starts sent the launcher back: the same release is only told about, «Обновить» tries it
        from bv import install, update
        self.publish(update.STABLE, "0.4.1")
        u = self.updater()
        u.check()
        u.apply()
        update.record_start(self.root, 5, failed=True)
        self.assertEqual(update.record_start(self.root, 5, failed=True), "0.4.0")
        self.build.reset_mock()
        self.said.clear()
        u = self.updater()                                                              # the launcher's next start
        self.assertEqual(u.check()["version"], "0.4.1")                                # the banner stays
        self.assertEqual(u.status, "")
        self.build.assert_not_called()
        self.assertIn("WARN", [level for level, _ in self.said])
        u.check()
        self.assertEqual(len([m for _, m in self.said if "не запустилась" in m]), 1)    # said once
        self.assertEqual(u.prepare(), "0.4.1")                                         # «Обновить»: by hand
        self.publish(update.STABLE, "0.4.2")                                           # a newer one: as usual
        u = self.updater()
        self.assertEqual(u.check()["version"], "0.4.2")
        self.assertEqual(u.ready, "0.4.2")
        self.assertIsNone(update.refused(self.root))
        self.assertEqual(install.current_version(self.root), "0.4.0")

    def test_stable_channel_takes_no_test_build(self):
        from bv import update
        self.publish(update.STABLE, "0.4.1b1")                                         # releases/latest served one
        self.assertIsNone(self.updater().check())
        self.build.assert_not_called()
        self.assertTrue(update.test_build("0.4.1b1") and update.test_build("v0.4.1-beta.1"))
        self.assertFalse(update.test_build("0.4.1") or update.test_build("0.4.10") or update.test_build(""))

    def test_failed_switch_keeps_the_prepared_version(self):
        from unittest import mock
        from bv import install, update
        self.publish(update.STABLE, "0.4.1")
        u = self.updater()
        u.check()
        with mock.patch("bv.install.make_link", side_effect=OSError("mklink failed")):
            self.assertIsNone(u.apply())
        self.assertEqual((u.status, u.ready), ("ready", "0.4.1"))                      # «Установить сейчас» still there
        self.assertEqual(install.current_version(self.root), "0.4.0")
        self.assertFalse(u.due(game_running=False, companion_running=False, busy=False))   # not every tick
        self.assertEqual(u.apply(), "0.4.1")                                           # by hand: tried again
        self.assertEqual(install.current_version(self.root), "0.4.1")
        self.assertIsNone(u.apply())                                                   # nothing prepared: nothing


class ReleaseBuild(unittest.TestCase):
    """tools/release.py: what CI publishes is what bv.install puts in place."""

    def build(self, tag, exe=None, uv=None):
        import importlib
        import sys
        from unittest import mock
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "tools"))
        release = importlib.import_module("release")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dist = Path(tmp.name)
        if exe is not None:
            (dist / "Barrens Voice.exe").write_bytes(exe)
        uv = uv or {"version": "0.13.7", "url": "https://x/uv.zip", "sha256": "0" * 64}
        with mock.patch.object(release, "DIST", dist), mock.patch.object(release, "uv_entry", return_value=uv), \
             mock.patch.object(release, "released_tags", return_value=None), \
             mock.patch.dict("os.environ", {"RELEASE_TAG": tag} if tag else {}, clear=False):
            if not tag:
                os.environ.pop("RELEASE_TAG", None)
            archive = release.build()
        import json
        return archive, json.loads((dist / "manifest.json").read_text(encoding="utf-8"))

    def test_beta_build_installs(self):
        import hashlib
        import zipfile
        from unittest import mock
        from bv import install, paths
        archive, manifest = self.build(f"v{WORKING}-beta.7", exe=b"starter")
        self.assertEqual((archive.name, manifest["version"]), (f"barrens-voice-{WORKING}b7.zip", f"{WORKING}b7"))
        data = archive.read_bytes()
        self.assertEqual((manifest["size"], manifest["sha256"]), (len(data), hashlib.sha256(data).hexdigest()))
        self.assertIn("uv", manifest)
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            self.assertIn(f'version = "{WORKING}b7"', z.read("pyproject.toml").decode())
            toc = z.read("addon/BarrensVoice/BarrensVoice.toc").decode()
            self.assertNotIn("X-Curse-Project-ID", toc)                    # the sites have releases only
            self.assertNotIn("X-Wago-ID", toc)
            self.assertIn("## Interface:", toc)
        for need in ("pyproject.toml", "uv.lock", "LICENSE", "news.toml", "Barrens Voice.exe", "companion/BarrensVoice.pyw",
                     "companion/bv/__main__.py", "companion/bv/update.py", "companion/bv/launcher/app.py",
                     "companion/bv/selfcheck.py",
                     "addon/BarrensVoice/BarrensVoice.toc"):
            self.assertIn(need, names)
        self.assertFalse(any(n.startswith("companion/tests/") or "__pycache__" in n for n in names))
        with tempfile.TemporaryDirectory() as d, mock.patch("bv.install.build", side_effect=built):
            root = Path(d) / "BarrensVoice"
            self.assertEqual(install.install(root, archive, "cuda", Path(d) / "uv.exe", log=lambda *a: None), f"{WORKING}b7")
            self.assertEqual((root / "Barrens Voice.exe").read_bytes(), b"starter")
            code = Path(os.path.realpath(root / "app" / "current" / "companion"))
            self.assertEqual(paths.layout(code, {})["root"], root.resolve())

    def test_tag_must_name_the_version(self):
        with self.assertRaises(SystemExit):
            self.build("v9.9.9")
        _, manifest = self.build("")
        self.assertNotIn("b", manifest["version"])                                  # no tag: pyproject's own


class InstallerScript(unittest.TestCase):
    """installer/BarrensVoice.iss, as Inno Setup hands its lines on (not compiled here)."""

    @staticmethod
    def expand(text: str, app: str) -> str:
        """Inno's reading of a parameter that takes constants: "{{" is "{", "{...}" a constant, a lone "}" itself
        (its help, "Constants"); then "" is " in a quoted value."""
        out, i = [], 0
        while i < len(text):
            if text.startswith("{{", i):
                out.append("{")
                i += 2
            elif text[i] == "{":
                end = text.index("}", i)
                out.append(app if text[i + 1:end] in ("app", "code:AppForPowerShell") else "<" + text[i + 1:end] + ">")
                i = end + 1
            else:
                out.append(text[i])
                i += 1
        return "".join(out).replace('""', '"')

    def test_uninstall_command_is_whole(self):
        # "}}" stayed "}}": PowerShell got one brace too many and the uninstaller stopped no process
        import re
        iss = (Path(__file__).resolve().parents[2] / "installer" / "BarrensVoice.iss").read_text(encoding="utf-8")
        line = next(l for l in iss.splitlines() if 'RunOnceId: "StopBarrensVoice"' in l)
        command = self.expand(re.search(r'Parameters: "((?:[^"]|"")*)"', line).group(1), r"C:\BV")
        script = command.split("-Command ", 1)[1].strip('"')
        self.assertEqual(script.count("{"), script.count("}"), script)
        self.assertIn("Where-Object { $_.ExecutablePath", script)
        self.assertTrue(script.endswith("Stop-Process -Id $_.ProcessId -Force }"), script)


class UvSyncFails(unittest.TestCase):
    def test_build_raises_on_uv_error(self):
        from unittest import mock
        from bv import install

        class Proc:
            stdout = iter(["error: no network\n"])
            returncode = 2

            def wait(self):
                return 2
        said = []
        with tempfile.TemporaryDirectory() as d, mock.patch("subprocess.Popen", return_value=Proc()):
            with self.assertRaises(RuntimeError):
                install.build(Path(d), Path(d) / "app" / "0.4.1", "cuda", Path(d) / "uv.exe", log=said.append)
        self.assertTrue(any("no network" in s for s in said))                       # uv's own words in the log


class OldClients(unittest.TestCase):
    """A release must install through the update code players already have (tests/compat: frozen copies of
    released clients). A change to the archive or the manifest that an old client cannot take strands it."""

    # released before tests/compat existed: their install.py is beta 5's but for version_key (numbers only: 0.4.0
    # is older than 0.4.0b4 to them, so they skip 0.4.0 itself and update by themselves from 0.4.1 or a later beta)
    BEFORE_COMPAT = {"v0.4.0-beta.1", "v0.4.0-beta.2", "v0.4.0-beta.3", "v0.4.0-beta.4"}

    def test_every_release_has_its_copy(self):
        # CLAUDE.md: a release that changes update.py or install.py adds a frozen copy of them to tests/compat
        import re
        import subprocess
        root = Path(__file__).resolve().parents[2]
        tags = [t for t in subprocess.run(["git", "tag", "-l", "v*"], cwd=root, capture_output=True, text=True,
                                          encoding="utf-8", errors="replace").stdout.split()
                if re.fullmatch(r"v\d+\.\d+\.\d+(-beta\.\d+)?", t)]
        if not tags:
            self.skipTest("no release tags here (a shallow clone)")
        # on Windows git checks the copies out with CRLF, and git show gives the tag's LF: compared without "\r"
        copies = [((c / "update.py").read_bytes() + b"\0" + (c / "install.py").read_bytes()).replace(b"\r\n", b"\n")
                  for c in (root / "companion" / "tests" / "compat").glob("v*") if (c / "update.py").is_file()]
        for tag in sorted(set(tags) - self.BEFORE_COMPAT):
            files = [subprocess.run(["git", "show", f"{tag}:companion/bv/{name}"], cwd=root, capture_output=True)
                     for name in ("update.py", "install.py")]
            if any(f.returncode for f in files):
                continue                                                # before the installer: no updater
            self.assertTrue(files[0].stdout + b"\0" + files[1].stdout in copies,
                          f"{tag}: add its update.py and install.py to companion/tests/compat (and to OldClients)")

    def test_beta5_client_installs_a_new_release(self):
        import base64
        import io
        import json
        from unittest import mock
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from compat.v0_4_0b5 import install as old_install, update as old_update
        from bv import install
        archive, manifest = ReleaseBuild.build(self, f"v{WORKING}-beta.7", exe=b"starter")
        base = "https://example/download/"
        raw = json.dumps(manifest).encode()
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        files = {base + "manifest.json": raw, base + "manifest.json.sig": base64.b64encode(key.sign(raw)),
                 base + archive.name: archive.read_bytes()}

        class Stream(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "BarrensVoice"
            make_version(root, "0.4.0b5")
            (root / "uv.exe").write_bytes(b"uv")
            old_install.point_current(root, root / "app" / "0.4.0b5")
            real_verify = old_update.verify      # its default key is bound at definition: give it the test key
            with mock.patch.object(old_update, "verify", lambda m, s, *a: real_verify(m, s, public)):
                found = old_update.signed_manifest(base, fetch=files.__getitem__)       # the old client reads it
                self.assertIsNotNone(found)
                self.assertTrue(old_update.newer(found["version"], "0.4.0b5"))
                got = old_update.download(found, root / "cache" / "downloads",
                                          fetch_stream=lambda req, timeout=0: Stream(files[req.full_url]))
                with mock.patch.object(old_install, "build") as build:
                    version = old_update.prepare(root, got)
                    build.assert_called_once()
                old_update.apply(root, version)
            self.assertEqual(install.current_version(root), f"{WORKING}b7")                 # the old client switched
            from bv.paths import CODE_DIR  # noqa: F401 - the new launcher's start puts its starter in place
            install.place_starter(root, root / "app" / f"{WORKING}b7")
            self.assertEqual((root / "Barrens Voice.exe").read_bytes(), b"starter")

    def test_beta6_client_updates_through_its_updater(self):
        # beta.6 and beta.7 have the launcher's Updater: check, download, prepare (uv, build), apply when idle
        self.updater_client("v0_4_0b6", "0.4.0b7")

    def test_beta8_client_updates_through_its_updater(self):
        self.updater_client("v0_4_0b8", "0.4.0b8")

    def test_beta9_client_updates_through_its_updater(self):
        # beta.9 and beta.10: the Windows «Apps» entry follows the version (note_version)
        self.updater_client("v0_4_0b9", "0.4.0b9")

    def test_beta11_client_updates_through_its_updater(self):
        self.updater_client("v0_4_0b11", "0.4.0b11")

    def test_beta14_client_updates_through_its_updater(self):
        # the first client with the selfcheck, the exact-version refusal and the watch before the switch
        # it keeps test builds out of the stable channel, so it gets a release: 0.4.0 as pyproject.toml has it (no tag,
        # so no refusal of the test-build addon a tagged release would get)
        self.updater_client("v0_4_0b14", "0.4.0b14", release=None)

    def test_beta17_client_updates_through_its_updater(self):
        # pruning by name and after current, settling while running, the refusal history: a release still installs
        self.updater_client("v0_4_0b17", "0.4.0b17", release=None)

    def test_beta19_client_updates_through_its_updater(self):
        # the ready version said without «когда закроешь игру» (the launcher says what it waits for): a release
        # still installs
        self.updater_client("v0_4_0b19", "0.4.0b19", release=None)

    def updater_client(self, client: str, current: str, release: str | None = f"v{WORKING}-beta.99"):
        import base64
        import io
        import json
        from unittest import mock
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        import hashlib
        import importlib
        import zipfile
        old_install = importlib.import_module(f"compat.{client}.install")
        old_update = importlib.import_module(f"compat.{client}.update")
        from bv import install
        uv_zip = io.BytesIO()                            # the release names another uv: the old client fetches it
        with zipfile.ZipFile(uv_zip, "w") as z:
            z.writestr("uv.exe", b"new uv")
        uv_zip = uv_zip.getvalue()
        uv = {"version": "0.99.0", "url": "https://x/uv-0.99.0.zip", "sha256": hashlib.sha256(uv_zip).hexdigest()}
        archive, manifest = ReleaseBuild.build(self, release, exe=b"starter", uv=uv)
        base = old_update.STABLE
        raw = json.dumps(manifest).encode()
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        files = {base + "manifest.json": raw, base + "manifest.json.sig": base64.b64encode(key.sign(raw)),
                 base + archive.name: archive.read_bytes(), uv["url"]: uv_zip}

        class Stream(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        said = []
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "BarrensVoice"
            make_version(root, current)
            (root / "uv.exe").write_bytes(b"uv")
            old_install.point_current(root, root / "app" / current)
            updater = old_update.Updater(root, current, lambda: {"update_channel": "stable", "auto_update": True},
                                         lambda m, level="INFO": said.append(m), fetch=files.__getitem__,
                                         fetch_stream=lambda req, timeout=0: Stream(files[req.full_url]), public=public)
            # the old release_uv reads the manifest's "uv" (its own get(), the installed uv's version asked)
            import contextlib

            def whole(build_root, folder, *a, **k):       # what the starter runs (beta 14 on points only at a whole version)
                (Path(folder) / "companion").mkdir(parents=True, exist_ok=True)
                (Path(folder) / "companion" / "BarrensVoice.pyw").write_text("", encoding="utf-8")
                built(build_root, folder)
            with mock.patch.object(old_install, "build", side_effect=whole) as build, \
                    mock.patch.object(old_update, "get", side_effect=lambda url, timeout=0: files[url]), \
                    mock.patch.object(old_update, "uv_version", return_value="0.13.7"), \
                    (mock.patch.object(old_update, "version_runs", return_value=None)      # beta 14 on: the selfcheck
                     if hasattr(old_update, "version_runs") else contextlib.nullcontext()):   # of a built version's Python
                self.assertIsNotNone(updater.check())
                build.assert_called_once()
                self.assertEqual(build.call_args[0][3].name, "uv.new.exe")          # built with the release's uv
            self.assertEqual(updater.status, "ready", said)
            self.assertEqual((root / "uv.exe").read_bytes(), b"new uv")                 # then put in place
            self.assertFalse(updater.due(game_running=True, companion_running=False, busy=False))
            self.assertTrue(updater.due(game_running=False, companion_running=False, busy=False))
            updater.apply()
            self.assertEqual(install.current_version(root), manifest["version"])        # the old client switched


class UpdateInWindow(unittest.TestCase):
    """The launcher's own update steps, on a stand-in for its window (no Tk drawn)."""

    def setUp(self):
        from types import SimpleNamespace
        from bv.launcher import app, model
        from bv.launcher.model import Game
        self.app, self.ns = app, SimpleNamespace
        self.installed = []
        self.win = SimpleNamespace(updater=SimpleNamespace(ready="0.4.1"), config={"auto_update": True},
                                   addon_version="0.3.1", running_game=None, busy="", addons_tried=set(),
                                   games=[Game("forever", "WoW Forever", Path("/f"), "0.3.0"),
                                          Game("retail", "WoW Retail", Path("/r"), "0.4.0")],
                                   install_addon=self.installed.append)

    def test_addons_follow_only_with_automatic_updates(self):
        self.app.App.update_addons(self.win)
        self.assertEqual([g.key for g in self.installed], ["forever"])                 # older only; newer kept
        self.app.App.update_addons(self.win)
        self.assertEqual(len(self.installed), 1)                                       # tried once, not every 15 s
        self.installed.clear()
        self.win.addons_tried.clear()
        self.win.config["auto_update"] = False
        self.app.App.update_addons(self.win)
        self.assertEqual(self.installed, [])                                           # off: by hand («обновить аддон»)
        self.win.config["auto_update"] = True
        self.win.updater = None                                                        # run from the source: never
        self.app.App.update_addons(self.win)
        self.assertEqual(self.installed, [])

    def test_install_now(self):
        from unittest import mock
        done = []
        self.win.updater.apply = lambda: done.append("apply") or "0.4.1"
        self.win.relaunch = lambda updated=False: done.append("relaunch" if updated else "relaunch without --updated")
        self.win.root = None
        self.win.stop_companion = lambda then, why: (done.append("stop"), then())
        self.win.running_game = "forever"
        with mock.patch.object(self.app.messagebox, "askyesno", return_value=False):
            self.app.App.install_update_now(self.win)
        self.assertEqual(done, [])                                                     # the player said no
        with mock.patch.object(self.app.messagebox, "askyesno", return_value=True):
            self.app.App.install_update_now(self.win)
        self.assertEqual(done, ["stop", "apply", "relaunch"])                          # stopped first, then switched
        done.clear()
        self.win.updater.apply = lambda: done.append("apply")                          # the switch failed: no restart
        with mock.patch.object(self.app.messagebox, "askyesno", return_value=True):
            self.app.App.install_update_now(self.win)
        self.assertEqual(done, ["stop", "apply"])
        done.clear()
        self.win.updater.ready = None
        self.app.App.install_update_now(self.win)
        self.assertEqual(done, [])                                                     # nothing prepared: nothing


    def test_start_failure_counts_only_the_versions_fault(self):
        state = {}
        win = self.ns(companion=self.ns(state=lambda: state, proc=self.ns(pid=7)))
        failed = lambda code: self.app.App.start_failed(win, code)                    # noqa: E731
        self.assertFalse(failed(0))                                                    # a good end
        self.assertEqual([failed(c) for c in (None, 3, 4, 5)], [None] * 4)            # no verdict either way
        self.assertTrue(failed(1))                                                     # died: the version's fault
        state.update(phase="error", error="аддон не установлен", pid=6)
        self.assertIsNone(failed(1))           # this run said why itself (its pid may be the venv launcher's child)
        state.update(phase="starting")
        self.assertTrue(failed(1))                                                     # died after its start

    def test_state_file_is_this_runs(self):
        # start_failed trusts state.json's phase without a pid: every start deletes the one of the run before
        from unittest import mock
        from bv.launcher import process
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bv").mkdir()
            (Path(d) / "bv" / "__init__.py").write_text("")
            (Path(d) / "bv" / "__main__.py").write_text("")
            state = Path(d) / "state.json"
            state.write_text('{"phase": "error", "error": "аддон не установлен"}', encoding="utf-8")
            with mock.patch.object(process, "STATE_FILE", state):
                companion = process.Companion(Path(d), lambda line: None)
                companion.start()
                companion.proc.wait(timeout=30)
                self.assertFalse(state.exists())
                self.assertEqual(companion.state(), {})


    def ended_with_the_game(self, exit_with_wow, autostart=False):
        """The companion ended because the game closed, a version prepared: what the launcher does."""
        done = []
        win = self.ns(exit_seen=False, last_code=None, companion_started=0.0, companion_game=None, running_game=None,
                      game=lambda key: None, tray=self.ns(notify=done.append), say=lambda *a: None, busy="",
                      companion=self.ns(state=lambda: {"phase": "game_closed"}, running=False),
                      config={"exit_with_wow": exit_with_wow}, autostart=autostart,
                      quit=lambda: done.append("quit"), relaunch=lambda updated=False: done.append(("relaunch", updated)))
        win.closes_with_game = lambda: self.app.App.closes_with_game(win)
        win.updater = self.ns(due=lambda *a: True, apply=lambda: done.append("apply") or "0.4.1")
        win.closed = False
        self.app.App.companion_ended(win, 0)
        if "quit" not in done:                                                         # the tick goes on
            self.app.App.apply_update_when_idle(win)
        return done

    def test_update_after_the_game_closed(self):
        # closing with the game: the version is switched, no restart (it reopened the window, or started the game
        # again with «Запускать игру вместе с лаунчером»); else the restart that does not start the game. With the
        # start with Windows on the launcher always stays (the check is grey), whatever the check says
        self.assertEqual(self.ended_with_the_game(exit_with_wow=True), ["apply", "quit"])
        self.assertEqual(self.ended_with_the_game(exit_with_wow=False), ["apply", ("relaunch", True)])
        self.assertEqual(self.ended_with_the_game(exit_with_wow=True, autostart=True), ["apply", ("relaunch", True)])
        self.assertEqual(self.ended_with_the_game(exit_with_wow=False, autostart=True), ["apply", ("relaunch", True)])

    def test_switch_waits_for_the_companions_end(self):
        done = []
        win = self.ns(exit_seen=False, running_game=None, busy="", companion=self.ns(running=False),
                      updater=self.ns(due=lambda *a: True, apply=lambda: done.append("apply") or "0.4.1"),
                      relaunch=lambda updated=False: done.append("relaunch"))
        self.app.App.apply_update_when_idle(win)
        self.assertEqual(done, [])
        win.exit_seen = True
        self.app.App.apply_update_when_idle(win)
        self.assertEqual(done, ["apply", "relaunch"])

    def test_the_wait_is_said_only_when_there_is_one(self):
        # «готова — установится, когда закроешь игру» came with no game at all, the switch a second later
        said = []

        def win(game, running):
            return self.ns(exit_seen=True, running_game=game, busy="", companion=self.ns(running=running),
                           updater=self.ns(ready="0.4.1", due=lambda g, c, b: not g and not c, apply=lambda: None),
                           relaunch=lambda updated=False: None, say=lambda text, *a: said.append(text),
                           config={"exit_with_wow": False})
        w = win("forever", True)
        self.app.App.apply_update_when_idle(w)
        self.app.App.apply_update_when_idle(w)
        self.assertEqual(said, ["версия 0.4.1 установится, когда закроешь игру"])      # once
        said.clear()
        self.app.App.apply_update_when_idle(win(None, False))  # nothing to wait for: switched, no word of waiting
        self.assertEqual(said, [])
        # the voice-over ends by itself a moment after the game it saw, «Закрываться вместе с игрой» or not: no word
        w.running_game = None
        self.app.App.apply_update_when_idle(w)
        self.assertEqual(said, [])
        # ... but a voice-over that has seen no game yet waits for one: said
        self.app.App.apply_update_when_idle(win(None, True))
        self.assertEqual(said, ["версия 0.4.1 установится, когда остановится озвучка"])

    def test_the_launch_hint_says_nothing_of_the_windows_start(self):
        # «При входе в Windows игра сама не запускается» under «Запускать игру вместе с лаунчером» confused the player
        self.assertNotIn("При входе в Windows", self.app.TIPS["launch_wow"])

    def test_the_start_with_windows_never_launches_the_game(self):
        # «Запускать игру вместе с лаунчером»: the game only for a start by hand, not for the one at Windows
        # sign-in (--tray), even without a tray to wait in
        started, said = [], []
        for resident, launched in ((False, True), (True, False)):
            started.clear()
            win = self.ns(args={"updated": False}, running_game=None, resident=resident, config={"launch_wow": True},
                          start_companion=lambda args=None: started.append(args), say=said.append)
            self.app.App.start_on_open(win)
            self.assertEqual(started == [None], launched, resident)

    def test_a_second_start_by_hand_launches_the_game(self):
        # the launcher waits in the tray; its shortcut asks it to show itself and, with «Запускать игру вместе с
        # лаунчером», to launch the game - a second start with Windows only shows it
        import tempfile
        for written, launch_wow, game, running, played in (("play", True, None, False, True),
                                                            ("", True, None, False, False),          # --tray
                                                            ("play", False, None, False, False),
                                                            ("play", True, "forever", False, False),
                                                            ("play", True, None, True, False)):
            done = []
            win = self.ns(config={"launch_wow": launch_wow}, running_game=game, companion=self.ns(running=running),
                          busy="", show=lambda: done.append("show"), play=lambda: done.append("play"))
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "launcher.show"
                path.write_text(written, encoding="utf-8")
                self.app.App.second_start(win, path)
                self.assertFalse(path.exists())
            self.assertEqual(done, ["show", "play"] if played else ["show"], (written, launch_wow, game, running))

    def test_turning_auto_updates_on_checks_at_once(self):
        # a version found with automatic updates off waited for the next hourly check after the box was ticked
        from unittest import mock
        started = []
        win = self.ns(updater=self.ns(), _check_update=lambda: started.append("check"))
        with mock.patch.object(self.app.threading, "Thread", lambda target, **kw: self.ns(start=target)):
            self.app.App.update_settings_changed(win, {"auto_update": False, "update_channel": "stable"},
                                                 {"auto_update": True, "update_channel": "stable"})
            self.assertEqual(started, ["check"])
            self.app.App.update_settings_changed(win, {"auto_update": True, "update_channel": "stable"},
                                                 {"auto_update": True, "update_channel": "beta"})
            self.assertEqual(started, ["check", "check"])              # test builds asked for: looked for now
            self.app.App.update_settings_changed(win, {"auto_update": True, "update_channel": "beta"},
                                                 {"auto_update": False, "update_channel": "beta"})
            self.assertEqual(len(started), 2)                           # turned off: nothing to do
            self.app.App.update_settings_changed(self.ns(updater=None, _check_update=lambda: started.append("x")),
                                                 {"auto_update": False}, {"auto_update": True})
            self.assertEqual(len(started), 2)                           # from the source: no updater

    def test_companion_starts_as_the_window_says(self):
        # a change config.json did not take held in the window only: «компаньон запускается (WoW Retail)» while the
        # companion read Forever from the file, and with «Запускать игру…» the window went to the tray for a game
        # the companion did not launch. The game and the launch choice go on its command line
        from unittest import mock

        def started(selected, launch_wow, args=None, extra=()):
            got = []
            win = self.ns(companion=self.ns(running=False, start=got.append), busy="", selected=selected,
                          addon_missing=lambda: False,
                          config={"game": selected, "launch_wow": launch_wow, "exit_with_wow": not launch_wow},
                          extra=list(extra), say=lambda *a: None, settle_due=False, pending={})
            with mock.patch.object(self.app.model, "read_config", lambda strict=False: dict(win.config)):
                self.assertTrue(self.app.App.start_companion(win, args))
            self.assertTrue(win.settle_due)                                            # a run that may settle
            return got[0]
        self.assertEqual(started("retail", True), ["--retail", "--launch"])
        self.assertEqual(started("forever", False), ["--forever", "--no-launch"])
        self.assertEqual(started("forever", True, ["--no-launch"]), ["--forever", "--no-launch"])     # Play, a running game
        self.assertEqual(started("retail", False, ["--launch"]), ["--retail", "--launch"])
        self.assertEqual(started("forever", True, None, ["--f5", "--no-launch"]), ["--forever", "--f5", "--no-launch"])
        self.assertEqual(started("forever", False, ["--launch"], ["--f5", "--no-launch"]), ["--forever", "--f5", "--launch"])

    def test_start_after_an_update_does_not_start_the_game(self):
        def opened(running_game, launch_wow, updated):
            started = []
            win = self.ns(args={"updated": updated}, running_game=running_game, config={"launch_wow": launch_wow},
                          resident=False, start_companion=lambda args=None: started.append(args), say=lambda *a: None)
            self.app.App.start_on_open(win)
            return started
        self.assertEqual(opened(None, True, False), [None])                               # the player's start: the game
        self.assertEqual(opened(None, True, True), [])                                   # after an update: no game
        self.assertEqual(opened("forever", True, True), [["--no-launch"]])               # the game runs: voices back
        self.assertEqual(opened("forever", False, False), [None])
        self.assertEqual(opened(None, False, False), [])


    def test_log_written_when_it_cannot_rotate(self):
        # launcher.log is the windowless launcher's stderr too: Windows refuses to move it, and the line was lost
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "logs" / "launcher.log"
            log.parent.mkdir()
            log.write_text("x" * 1_000_001, encoding="utf-8")
            with mock.patch.object(self.app, "LAUNCHER_LOG", log), \
                    mock.patch.object(Path, "replace", side_effect=PermissionError("in use")):
                self.app.launcher_log("строка")
            self.assertTrue(log.read_text(encoding="utf-8").endswith(" строка\n"))
            with mock.patch.object(self.app, "LAUNCHER_LOG", log):
                self.app.launcher_log("ещё")
            self.assertTrue(log.with_suffix(".log.1").read_text(encoding="utf-8").endswith(" строка\n"))   # rotated now
            self.assertTrue(log.read_text(encoding="utf-8").endswith(" ещё\n"))


class Changelog(unittest.TestCase):
    """CHANGELOG.md in its format (Keep a Changelog, CLAUDE.md)."""

    def test_format(self):
        import re
        text = (Path(__file__).resolve().parents[2] / "CHANGELOG.md").read_text(encoding="utf-8")
        versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d\d-\d\d|Unreleased)$", text, re.M)
        self.assertEqual(len(versions), len(re.findall(r"^## ", text, re.M)), "a version heading out of form")
        self.assertTrue(all(d != "Unreleased" for _, d in versions[1:]), "only the newest may be unreleased")
        order = ["Added", "Changed", "Removed", "Fixed"]
        for version, body in re.findall(r"^## \[([^\]]+)\][^\n]*\n(.*?)(?=^## |^\[[^\]\n]+\]: |\Z)", text, re.M | re.S):
            sections = re.findall(r"^### (.+)$", body, re.M)
            self.assertTrue(sections, version)
            self.assertEqual(sections, [s for s in order if s in sections], f"{version}: sections {sections}")
            for entry in re.split(r"^- ", body, flags=re.M)[1:]:
                title, _, rest = entry.partition("\n")
                rest = rest.split("\n### ")[0]                  # a title-only last entry: the next section is not its text
                self.assertFalse(title.endswith("."), f"{version}: a title ends with a full stop: {title}")
                if rest.strip():
                    self.assertTrue(rest.startswith("\n  "), f"{version}: «{title}» - a blank line, then an indented paragraph")
            self.assertRegex(text, rf"(?m)^\[{re.escape(version)}\]: https://github\.com/", f"{version}: no link line")


class AppsEntry(unittest.TestCase):
    """Windows «Apps» shows the version in use after an update or a rollback (the installer's entry)."""

    class Reg:
        HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 4, 1

        def __init__(self, location):
            self.values = {"InstallLocation": location, "DisplayVersion": "0.4.0b6"}

        def OpenKey(self, *a):
            import contextlib
            return contextlib.nullcontext(self)

        def QueryValueEx(self, key, name):
            return self.values[name], 1

        def SetValueEx(self, key, name, _, kind, value):
            self.values[name] = value

    def test_note_version(self):
        from bv import update
        with tempfile.TemporaryDirectory() as d:
            reg = self.Reg(d + "\\")                                         # Inno writes it with a trailing backslash
            self.assertTrue(update.note_version(Path(d), "0.4.0b8", reg))
            self.assertEqual(reg.values["DisplayVersion"], "0.4.0b8")
            other = self.Reg(str(Path(d) / "elsewhere"))                       # another install's entry: untouched
            self.assertFalse(update.note_version(Path(d), "0.4.0b8", other))
            self.assertEqual(other.values["DisplayVersion"], "0.4.0b6")
            self.assertFalse(update.note_version(Path(d), None, reg))           # not known (a junction unread): no blank
            self.assertEqual(reg.values["DisplayVersion"], "0.4.0b8")
