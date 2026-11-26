"""The engine manager with a fake F5: loaded on demand, notices, unloaded, falls back on error."""
import sys
import threading
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bv.protocol import Line  # noqa: E402


class FakeF5:
    fail = None
    missing = False     # the failure was a model missing from the cache (f5engine.models_missing)
    made = 0

    def __init__(self, *a):
        FakeF5.made += 1
        self.ready = threading.Event()
        self.error = None
        self.stopped = False
        self.cleared = False
        self.assigner = None
        self.lines = __import__("queue").Queue()

    def clear(self):
        self.cleared = True

    def start(self):
        self.error = FakeF5.fail
        self.models_missing = FakeF5.missing
        if FakeF5.fail is not None:          # failed at once; else ready when the test says
            self.ready.set()

    def shutdown(self):
        self.stopped = True


class FakeSapi:
    def __init__(self):
        self.said = []

    def say(self, line):
        self.said.append(line.text)

    def clear(self):
        pass


from bv.engines import Engines  # noqa: E402


def _ort_stand_in():
    """onnxruntime where it is installed; else (Windows without a video card's extra, as in CI: the module comes
    only with onnxruntime-gpu or -directml) a stand-in with what the tests below touch, left in sys.modules for
    the run - a per-test patch of sys.modules would drop numpy and the rest a test loaded, and the next import
    of them fails."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        ort = types.ModuleType("onnxruntime")
        ort.get_available_providers = lambda: ["CPUExecutionProvider"]

        class SessionOptions:
            enable_mem_pattern = True
            graph_optimization_level = None
            execution_mode = None
        ort.SessionOptions = SessionOptions
        ort.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_ALL=99)
        ort.ExecutionMode = types.SimpleNamespace(ORT_SEQUENTIAL=0)
        sys.modules["onnxruntime"] = ort


_ort_stand_in()

FAKE_F5 = types.SimpleNamespace(F5Speaker=FakeF5)


def wait():
    """Until the engine manager has heard how a load ended (its f5-wait threads are done)."""
    for t in threading.enumerate():
        if t.name == "f5-wait":
            t.join(2)


class EnginesTest(unittest.TestCase):
    def setUp(self):
        # the engine imports F5Speaker when it loads; a fake one only for these tests
        patcher = unittest.mock.patch.dict(sys.modules, {"bv.f5engine": FAKE_F5})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, home=Path("."), clock=None, *, fallback=True):
        self.sapi = FakeSapi()
        self.logged = []
        return Engines({"engine": "f5", "spoken_notices": True, "race_voice_fallback": fallback},
                       self.logged.append, home, self.sapi, None,
                       **({"clock": clock} if clock else {}))

    def test_same_guild_character_uses_race_voice_only_after_warmup(self):
        class LoadingOmni(FakeF5):
            engine_name = "OmniVoice"
            on_fallback = None

        FakeF5.fail = None
        engines = self.make(fallback=False)
        engines.config["race_voice_backend"] = "omnivoice"
        line = Line("guild", "Орк", "Orc", "2", "", "", "Сообщение", 100, "f5")
        with unittest.mock.patch.dict(sys.modules, {"bv.omni": types.SimpleNamespace(OmniSpeaker=LoadingOmni)}):
            self.assertIsNone(engines.for_line(line))
            speaker = engines.f5
            self.assertIsNone(speaker.on_fallback)
            speaker.ready.set()
            wait()
            self.assertIs(engines.for_line(line), speaker)
            engines.want("sapi")
            self.assertIs(engines.for_line(Line("guild", line.sender, line.race, line.sex, "", "", "текст",
                                                voice="sapi")), self.sapi)

    def test_omnivoice_start_failure_loads_f5(self):
        fallback_started = threading.Event()

        class FailingOmni(FakeF5):
            engine_name = "OmniVoice"

            def start(self):
                self.error = "CUDA unavailable"
                self.ready.set()

        class FallbackF5(FakeF5):
            def start(self):
                super().start()
                fallback_started.set()

        FakeF5.fail = None
        engines = self.make()
        engines.config["race_voice_backend"] = "omnivoice"
        with (unittest.mock.patch.object(FAKE_F5, "F5Speaker", FallbackF5),
              unittest.mock.patch.dict(sys.modules, {"bv.omni": types.SimpleNamespace(OmniSpeaker=FailingOmni)})):
            engines.want("f5")
            self.assertTrue(fallback_started.wait(2))
            self.assertIs(type(engines.f5), FallbackF5)
            self.assertTrue(engines.omni_failed)
            engines.f5.ready.set()
            wait()
        self.assertTrue(engines.ready)
        self.assertTrue(any("CUDA unavailable" in text for text in self.logged))

    def test_native_omnivoice_failure_uses_windows_voices_without_f5(self):
        class FailingOmni(FakeF5):
            engine_name = "OmniVoice"

            def start(self):
                self.error = "CUDA unavailable"
                self.ready.set()

        engines = self.make()
        engines.config["race_voice_backend"] = "f5"  # older config.json
        line = Line("say", "Орк", "Orc", "2", "", "", "текст", 100, "f5")
        with (unittest.mock.patch("bv.omni_runtime.native_only", return_value=True),
              unittest.mock.patch.object(FAKE_F5, "F5Speaker", side_effect=AssertionError("F5 was loaded")),
              unittest.mock.patch.dict(sys.modules, {"bv.omni": types.SimpleNamespace(OmniSpeaker=FailingOmni)})):
            engines.want("f5")
            wait()
            self.assertIs(engines.for_line(line), self.sapi)
        self.assertTrue(any("CUDA unavailable" in text for text in self.logged))

    def test_load_on_demand_then_unload(self):
        FakeF5.fail = None
        e = self.make()
        line = Line("say", "Орк", "Orc", "2", "", "", "текст", 100, "f5")
        self.assertIs(e.for_line(line), self.sapi)          # still loading: Windows voices meanwhile
        e.f5.ready.set()
        wait()
        self.assertIn("Голоса рас готовы.", self.sapi.said)
        self.assertIsInstance(e.for_line(line), FakeF5)
        f5 = e.f5
        e.want("sapi")
        self.assertTrue(f5.stopped)
        self.assertIsNone(e.f5)
        self.assertIn("Голоса рас выгружены.", self.sapi.said)
        sapi_line = Line("say", "Орк", "Orc", "2", "", "", "текст", 100, "sapi")
        self.assertIs(e.for_line(sapi_line), self.sapi)

    def test_failure_falls_back(self):
        FakeF5.fail = "нет CUDA"
        e = self.make()
        e.want("f5")
        wait()
        self.assertIsNone(e.f5)
        self.assertIn("Голоса рас недоступны, читаю голосами Windows.", self.sapi.said)

    def test_online_restart_only_for_a_missing_model(self):
        # TensorRT not starting is no reason to go online and drop the models' mark; a model not in the cache is
        for missing, want in ((False, []), (True, ["нет в кэше"])):
            with self.subTest(missing=missing):
                FakeF5.fail, FakeF5.missing = "нет в кэше", missing
                e = self.make()
                asked = []
                e.on_models_missing = asked.append
                e.want("f5")
                wait()
                self.assertIn("Голоса рас недоступны, читаю голосами Windows.", self.sapi.said)
                self.assertEqual(asked, want)
        FakeF5.missing = False

    def test_an_empty_error_is_still_a_failure(self):
        # str() of some exceptions is "" (StopIteration): never taken for "ready"
        FakeF5.fail = ""
        e = self.make()
        e.want("f5")
        wait()
        self.assertFalse(e.ready)
        self.assertIsNone(e.f5)
        self.assertNotIn("Голоса рас готовы.", self.sapi.said)
        line = Line("say", "Орк", "Orc", "2", "", "", "текст", 100, "f5")
        self.assertIs(e.for_line(line), self.sapi)


    def test_a_failed_load_is_not_started_again_for_every_line(self):
        from bv import engines
        FakeF5.fail, FakeF5.made = "нет CUDA", 0
        now = [0.0]
        e = self.make(clock=lambda: now[0])
        line = Line("say", "Орк", "Orc", "2", "", "", "текст", 100, "f5")
        for _ in range(5):                                  # each frame and line asks for F5
            e.want("f5")
            self.assertIs(e.for_line(line), self.sapi)
            wait()
        self.assertEqual(FakeF5.made, 1)
        self.assertEqual(self.sapi.said.count("Голоса рас недоступны, читаю голосами Windows."), 1)
        now[0] = engines.RETRY_AFTER + 1                    # a while later: tried once more
        e.want("f5")
        wait()
        e.want("f5")
        wait()
        self.assertEqual(FakeF5.made, 2)
        e.want("sapi")                                      # the addon switched to Windows voices and back
        e.want("f5")
        wait()
        self.assertEqual(FakeF5.made, 3)

    def test_unloaded_stopped_or_reset_while_loading(self):
        import tempfile
        FakeF5.fail = None
        with tempfile.TemporaryDirectory() as d:
            e = self.make(home=Path(d))
            e.want("f5")
            loading = e.f5
            e.stop()                                        # a stop while loading: its queue is dropped
            self.assertTrue(loading.cleared)
            (Path(d) / "assignments.json").write_text("{}", encoding="utf-8")
            e.reset_assignments()                           # no assigner yet: the file is forgotten
            self.assertFalse((Path(d) / "assignments.json").exists())
            e.want("sapi")                                  # unloaded before it was ready
            self.assertTrue(loading.stopped)
            self.assertIn("загрузка голосов рас отменена", self.logged)
            loading.ready.set()                             # its load ends after all: nobody listens
            wait()
            self.assertFalse(e.ready)
            self.assertIsNone(e.f5)
            self.assertNotIn("Голоса рас готовы.", self.sapi.said)


    def test_a_windows_voice_sample_keeps_the_race_voices(self):
        # the voices window's play button: voice=sapi;sapi=<name> for that test line only
        FakeF5.fail = None
        e = self.make()
        e.follow(Line("control", "", "", "", "", "", "", 100, "f5"))
        loaded = e.f5
        self.assertIsNotNone(loaded)
        sample = Line("test", "Проверка", "", "", "", "", "Привет!", 100, "sapi", sapi_voice="Irina")
        e.follow(sample)
        self.assertIs(e.f5, loaded)
        self.assertFalse(loaded.stopped)
        self.assertIs(e.for_line(sample), self.sapi)       # the sample itself in that Windows voice
        e.follow(Line("control", "", "", "", "", "", "", 100, "sapi"))   # the addon switched: unloaded
        self.assertTrue(loaded.stopped)
        loaded.ready.set()
        wait()


class Accent:
    def accent(self, text):
        return text.replace("о", "+о")          # stress marks: the bytes F5 is given are more than typed

    def load(self):
        pass


class F5SpeakerLoad(unittest.TestCase):
    """F5Speaker's own thread with ONNX Runtime and the game client faked (they are Windows only)."""

    def speaker(self, home, slots, onnx=None, player=None):
        from bv import f5engine
        from bv.__main__ import DEFAULT_CONFIG
        from bv.assign import Assigner
        logged = []
        speaker = f5engine.F5Speaker(dict(DEFAULT_CONFIG), logged.append, home, Accent(),
                                     player=player or types.SimpleNamespace(generation=0))

        def load_onnx():
            speaker.assigner = Assigner(slots, home / "assignments.json", {})
            speaker.onnx, speaker.ref_audio, speaker.device = onnx, {}, "test"
        speaker._load_onnx = load_onnx
        return speaker, logged

    def voice(self, home, speech=2.0):
        """One voice of the bank: a 3 s reference clip with 2 s of speech in it."""
        import numpy as np
        import soundfile as sf
        from bv.gamevoices import Slot
        path = home / "orc-male.wav"
        sf.write(str(path), np.zeros(72000, dtype="float32"), 24000)
        return Slot("orc-male", "orc-male", "orc", "male", 0.0, str(path), "Мне это не по силам.", speech)

    def test_the_banks_pace_in_the_units_of_a_line(self):
        # the target pace counted the reference's bytes without stress marks, a voice's own pace with
        # them: the same voice was drawn toward itself by ~7 %
        import tempfile
        from bv import f5engine

        class Onnx:
            in_rate = 24000

            def synth(self, ref_audio, ref_text, text, total):
                return __import__("numpy").zeros(2400, dtype="float32"), 24000
        with tempfile.TemporaryDirectory() as d:
            slot = self.voice(Path(d))
            speaker, _ = self.speaker(Path(d), [slot], Onnx())
            speaker._load()
            # the unit: seconds of speech per byte of the reference's text as F5 gets it, stress marks
            # included - as a line is counted (its text is stressed before `duration`)
            stressed = Accent().accent(slot.text).encode("utf-8")
            self.assertGreater(len(stressed), len(slot.text.encode("utf-8")))
            self.assertAlmostEqual(speaker.pace(slot), slot.speech / len(stressed))
            self.assertAlmostEqual(speaker.target_pace, speaker.pace(slot))
            config = speaker.config
            text = "Привет всем"
            expected = 3.0 + f5engine.line_seconds(speaker.pace(slot), 0.0, len(text.encode("utf-8")), 100, "",
                                                   {**config, "character_tempo": 0})
            config["character_tempo"] = 0
            self.assertAlmostEqual(speaker.duration(slot, text, 100), expected)   # its own pace: not drawn

    def test_a_line_from_the_queue_to_the_player(self):
        import tempfile

        import numpy as np
        made, played = [], []

        class Onnx:
            in_rate = 24000

            def synth(self, ref_audio, ref_text, text, total):
                made.append(text)
                tone = np.sin(np.arange(24000) / 24000 * 2 * np.pi * 200).astype("float32") * 0.3
                return np.concatenate([tone, np.zeros(4800, dtype="float32")]), 24000

        class Player:
            generation = 0

            def play(self, data, generation):
                played.append((data[:4], generation))
        from unittest import mock
        from bv import f5engine
        made_at = []
        real_wav_bytes = f5engine.wav_bytes

        def wav_bytes(samples, rate, volume, gain_db=0.0):
            made_at.append(volume)
            return real_wav_bytes(samples, rate, volume, gain_db)
        self.addCleanup(lambda: self.assertTrue(made_at and all(v == 1.0 for v in made_at),
                                                f"lines made at full loudness, the player applies the volume: {made_at}"))
        patcher = mock.patch.object(f5engine, "wav_bytes", wav_bytes)
        patcher.start()
        self.addCleanup(patcher.stop)
        with tempfile.TemporaryDirectory() as d:
            speaker, logged = self.speaker(Path(d), [self.voice(Path(d))], Onnx(), Player())
            loop = threading.Thread(target=speaker._synth_loop)
            loop.start()
            self.assertTrue(speaker.ready.wait(5))
            self.assertIsNone(speaker.error)
            # the addon's volume at 50 %: the line is still made at full loudness, the player applies the 50 %
            speaker.say(Line("say", "Громмаш", "Orc", "2", "", "", "Кто в рейд", 100, "f5", volume=50))
            speaker.lines.put(None)                  # the end, after that line
            loop.join(5)
            self.assertFalse(loop.is_alive())
        self.assertEqual(made, ["Проверка.", "Кт+о в рейд."])   # the warm-up; the line stressed, a full stop
        self.assertIsNone(speaker.onnx)              # the model freed at the end
        self.assertEqual(played, [(b"RIFF", 0)])

    def test_a_bad_gain_costs_the_line_not_the_thread(self):
        # "kind_gain_db": {"yell": null} raised outside the loop's try: the f5-synth thread died, F5 still "ready"
        import tempfile

        import numpy as np
        played = []

        class Onnx:
            in_rate = 24000

            def synth(self, ref_audio, ref_text, text, total):
                return np.sin(np.arange(24000) / 24000 * 2 * np.pi * 200).astype("float32") * 0.3, 24000

        class Player:
            generation = 0

            def play(self, data, generation):
                played.append(data[:4])
        with tempfile.TemporaryDirectory() as d:
            speaker, logged = self.speaker(Path(d), [self.voice(Path(d))], Onnx(), Player())
            speaker.config["kind_gain_db"] = {"yell": None}
            loop = threading.Thread(target=speaker._synth_loop)
            loop.start()
            self.assertTrue(speaker.ready.wait(5))
            speaker.say(Line("yell", "Громмаш", "Orc", "2", "", "", "За Орду", 100, "f5"))
            speaker.say(Line("say", "Громмаш", "Orc", "2", "", "", "Кто в рейд", 100, "f5"))
            speaker.lines.put(None)
            loop.join(5)
            self.assertFalse(loop.is_alive())
        self.assertEqual(played, [b"RIFF"])                  # the yell lost, the next line said
        self.assertTrue(any(m.startswith("F5-TTS: ") for m in logged), logged)

    def test_an_empty_voice_bank_is_an_error(self):
        # a client without the Russian voice-over: the bank is made with no voices in it. Said before RUAccent
        # and F5's graphs are loaded - a retry every RETRY_AFTER loaded them only to fail
        import tempfile
        from unittest import mock

        from bv import f5engine
        loaded = []

        class Accentor(Accent):
            def load(self):
                loaded.append("RUAccent")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "F5_Metadata.onnx").write_bytes(b"")
            speaker, _ = self.speaker(Path(d), [])
            del speaker._load_onnx                           # the real one, with ONNX Runtime and the bank faked
            speaker.accentor = Accentor()
            speaker.config.update(f5_onnx_provider="CUDAExecutionProvider", f5_onnx_dir=d)
            with mock.patch("onnxruntime.get_available_providers", lambda: ["CUDAExecutionProvider"]), \
                    mock.patch.object(f5engine, "preload", lambda engine: None), \
                    mock.patch.object(f5engine, "make_bank", lambda *a: []), \
                    mock.patch("bv.f5onnx.F5Onnx", lambda *a: loaded.append("F5")):
                loop = threading.Thread(target=speaker._synth_loop)
                loop.start()
                loop.join(5)
            self.assertFalse(loop.is_alive())
            self.assertTrue(speaker.ready.is_set())
            self.assertIn("русской озвучки", speaker.error)
            self.assertEqual(loaded, [])

    def test_an_error_without_words_is_still_an_error(self):
        # str(e) of KeyError() or RuntimeError() is "": an error of "" would read as a load that worked
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            speaker, _ = self.speaker(Path(d), [])

            def load_onnx():
                raise RuntimeError()
            speaker._load_onnx = load_onnx
            loop = threading.Thread(target=speaker._synth_loop)
            loop.start()
            loop.join(5)
            self.assertTrue(speaker.ready.is_set())
            self.assertEqual(speaker.error, "RuntimeError")


class PrepareVoices(unittest.TestCase):
    """The bank made before the game is launched: never an error that ends the companion at «Играть», and
    «игра запустится, когда соберутся голоса рас» only when it is about to be made."""

    def prepare(self, stale, providers=("CUDAExecutionProvider", "CPUExecutionProvider")):
        from unittest import mock

        from bv import f5engine
        logged, made = [], []
        with mock.patch("bv.gamevoices.stale", stale), \
                mock.patch("onnxruntime.get_available_providers", lambda: list(providers)), \
                mock.patch.object(f5engine, "make_bank", lambda *a: made.append(1)):
            f5engine.prepare_voices({"f5_onnx_provider": "CUDAExecutionProvider"}, Path("."), logged.append)
        return logged, made

    def test_made_with_its_word(self):
        logged, made = self.prepare(lambda config, out: True)
        self.assertEqual((logged, made), (["игра запустится, когда соберутся голоса рас"], [1]))

    def test_nothing_to_make(self):
        self.assertEqual(self.prepare(lambda config, out: False), ([], []))

    def test_no_word_when_the_card_cannot_make_it(self):
        # the word was logged before prepare_voices found it would build nothing
        self.assertEqual(self.prepare(lambda config, out: True, ("CPUExecutionProvider",)), ([], []))

    def test_a_failing_look_at_the_bank_is_no_end(self):
        # stale() ran in main outside the guard: anything it raised ended the companion at «Играть» (exit 1,
        # a failed start for the update's rollback)
        def stale(config, out):
            raise TypeError("expected str, bytes or os.PathLike object, not int")
        logged, made = self.prepare(stale)
        self.assertEqual(made, [])
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0].startswith("голоса рас из клиента: "), logged)


class OnCard(unittest.TestCase):
    """A session ONNX Runtime quietly put on the processor (DirectML without DirectX 12) is refused: the voice bank
    would take many minutes on Whisper there and the race voices would not run anyway."""

    def test_on_card(self):
        from bv.f5engine import on_card

        class Session:
            def __init__(self, *providers):
                self.providers = list(providers)

            def get_providers(self):
                return self.providers
        on_card(Session("DmlExecutionProvider", "CPUExecutionProvider"), "DmlExecutionProvider")
        on_card(Session("CUDAExecutionProvider", "CPUExecutionProvider"), "CUDAExecutionProvider")
        on_card(Session("CPUExecutionProvider"), "CPUExecutionProvider")          # asked for the processor: fine
        with self.assertRaises(RuntimeError) as told:
            on_card(Session("CPUExecutionProvider"), "DmlExecutionProvider")
        self.assertIn("DirectX 12", str(told.exception))
        # a provider of f5_onnx_provider outside PREFERRED was let run on the processor: F5 "ready" there
        on_card(Session("TensorrtExecutionProvider", "CPUExecutionProvider"), "TensorrtExecutionProvider")
        with self.assertRaises(RuntimeError):
            on_card(Session("CPUExecutionProvider"), "TensorrtExecutionProvider")

    def test_card_session(self):
        # F5 and Whisper (the voice bank) set their sessions up in one place: the same card, DirectML's needs
        from bv.f5engine import card_session
        options, providers = card_session("DmlExecutionProvider")
        self.assertFalse(options.enable_mem_pattern)
        self.assertEqual(providers, [("DmlExecutionProvider", {"device_id": 0, "performance_preference": "high_performance"}),
                                     "CPUExecutionProvider"])
        options, providers = card_session("CUDAExecutionProvider")
        self.assertTrue(options.enable_mem_pattern)
        self.assertEqual(providers, ["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.assertEqual(card_session("CPUExecutionProvider")[1], ["CPUExecutionProvider"])
        # any other provider asked for gets it, the processor as the fallback (it got the processor alone)
        self.assertEqual(card_session("TensorrtExecutionProvider")[1], ["TensorrtExecutionProvider", "CPUExecutionProvider"])


class ModelsMissing(unittest.TestCase):
    """Which F5 failure a restart online can fix: a model missing from the cache, nothing else."""

    def test_models_missing_by_the_error(self):
        from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled

        from bv.f5engine import models_missing as missing_of
        self.assertTrue(missing_of(LocalEntryNotFoundError("not in the cache")))
        self.assertTrue(missing_of(OfflineModeIsEnabled("offline mode is enabled")))
        try:                                      # wrapped by another library: its cause counts
            try:
                raise LocalEntryNotFoundError("x")
            except LocalEntryNotFoundError as inner:
                raise OSError("RUAccent: could not load") from inner
        except OSError as outer:
            self.assertTrue(missing_of(outer))
        self.assertFalse(missing_of(RuntimeError("[TensorRT EP] failed to build the engine")))
        self.assertFalse(missing_of(FileNotFoundError("no game client")))   # a missing file, but not a model
        self.assertFalse(missing_of(None))


class FetchModels(unittest.TestCase):
    """F5's models: the word about a ~760 MB download only when they are not in the cache."""

    def test_the_download_said_only_when_it_happens(self):
        from unittest import mock

        from huggingface_hub.errors import LocalEntryNotFoundError

        from bv.f5engine import fetch_models
        calls, said = [], []

        def cached(repo, allow_patterns, local_files_only=False):
            calls.append(local_files_only)
            return "/cache/f5"

        def missing(repo, allow_patterns, local_files_only=False):
            calls.append(local_files_only)
            if local_files_only:
                raise LocalEntryNotFoundError("not in the cache")
            return "/cache/f5"
        with mock.patch.dict("os.environ", {"HF_HUB_OFFLINE": "0"}):
            with mock.patch("huggingface_hub.snapshot_download", cached):
                self.assertEqual(fetch_models({}, said.append), Path("/cache/f5"))
            self.assertEqual((calls, said), ([True], []))               # in the cache: no network, no word
            calls.clear()
            with mock.patch("huggingface_hub.snapshot_download", missing):
                fetch_models({}, said.append)
            self.assertEqual(calls, [True, False])                      # then downloaded
            self.assertEqual(len(said), 1)
            self.assertIn("в первый раз скачивается", said[0])


if __name__ == "__main__":
    unittest.main()
