"""OmniVoice applies saved profile changes to the next synthesis request."""
import json
import hashlib
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bv import __main__, omni_runtime, whisper_onnx
from bv.audio import Player, PlaybackWindow, wav
from bv.launcher.model import update_config
from bv.omni import OmniSpeaker, Worker
from bv.omni_worker import profile_steps
from bv.protocol import Line


class OmniProfile(unittest.TestCase):
    def test_existing_windows_checkout_environment_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uv.lock").write_bytes(b"version = 1\n")
            version = hashlib.sha256(b"version = 1\r\n").hexdigest()[:12]
            env = root / "omnivoice" / f"runtime-{version}"
            (env / "Scripts").mkdir(parents=True)
            (env / "Scripts" / "python.exe").touch()
            (env / ".ready").touch()
            with patch.object(omni_runtime, "PROJECT", root), patch.object(omni_runtime, "CACHE_DIR", root):
                self.assertEqual(omni_runtime.environment.__wrapped__(), env)

    def test_native_install_only_fetches_whisper_and_accent_models(self):
        with (patch("bv.omni_runtime.native_only", return_value=True),
              patch("huggingface_hub.hf_hub_download") as fetch,
              patch("bv.text.Accentor") as accent,
              patch("bv.__main__.log")):
            self.assertEqual(__main__.fetch_models({}), 0)
        self.assertEqual([c.args for c in fetch.call_args_list],
                         [(whisper_onnx.REPO, name) for name in whisper_onnx.FILES])
        accent.return_value.load.assert_called_once_with()

    def test_native_worker_uses_windows_interpreter_and_reference(self):
        with (tempfile.TemporaryDirectory() as root,
              patch("bv.omni.omni_runtime.ready", return_value=True),
              patch("bv.omni.omni_runtime.interpreter", return_value=Path("managed-python.exe")),
              patch("bv.omni.subprocess.Popen") as launch,
              patch("bv.omni.threading.Thread")):
            worker = Worker({}, Path(root))
        command = launch.call_args.args[0]
        self.assertEqual(command[0], "managed-python.exe")
        self.assertEqual(worker.provider, "OmniVoice / CUDA")
        self.assertEqual(worker.voice_path(r"F:\Voices\ref.wav"), r"F:\Voices\ref.wav")

    def test_next_line_uses_saved_profile_without_replacing_worker(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            settings = home / "config.json"
            player = SimpleNamespace(generation=0)
            speaker = OmniSpeaker({"kind_gain_db": {}}, lambda text: None, home, player=player)
            worker = speaker.worker = Mock()
            worker.synth.return_value = object(), 24000, 0.1
            line = SimpleNamespace(kind="test", rate=100)
            slot = object()

            with patch("bv.omni.wav_bytes", return_value=b"audio"):
                self.assertEqual(speaker._render(slot, "Привет.", line, 0, False), b"audio")
                update_config(settings, omnivoice_profile="balanced")
                self.assertEqual(speaker._render(slot, "Привет.", line, 0, False), b"audio")
                settings.write_text("{", encoding="utf-8")  # an external edit in progress
                self.assertEqual(speaker._render(slot, "Привет.", line, 0, False), b"audio")
                settings.write_text(json.dumps({"omnivoice_profile": "quality"}), encoding="utf-8")
                self.assertEqual(speaker._render(slot, "Привет.", line, 0, False), b"audio")
            self.assertEqual([call.args[3] for call in worker.synth.call_args_list],
                             ["quality", "balanced", "balanced", "quality"])

    def test_old_or_invalid_profile_uses_quality_steps(self):
        self.assertEqual(profile_steps("balanced"), 24)
        for profile in (None, "quality", "fastest", {}):
            with self.subTest(profile=profile):
                self.assertEqual(profile_steps(profile), 32)


class OmniLive(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.player = Mock(generation=3)
        self.speaker = OmniSpeaker({"omnivoice_profile": "balanced", "kind_gain_db": {}, "players": {}},
                                  Mock(), Path(self.temp.name), player=self.player)
        self.speaker.assigner = Mock()
        self.speaker.on_fallback = Mock()
        self.audio = wav(b"\0\0" * 8000, 8000)

    def line(self, text, *, age=0, **kwargs):
        return Line("say", "Орк", "Orc", "2", "", "", text, received_at=time.monotonic() - age, **kwargs)

    def test_warmup_runs_synthesis_and_audio_check_before_ready(self):
        slot = SimpleNamespace(race="Orc")
        worker = Mock(provider="OmniVoice / CUDA")
        worker.receive.return_value = {"gpu": "RTX"}, b""
        worker.synth.return_value = None, 24000, 0.1
        with (patch("bv.omni.Worker", return_value=worker), patch("bv.omni.make_bank", return_value=[slot]),
              patch("bv.omni.Assigner"),
              patch("bv.omni.wav_bytes", return_value=self.audio), patch("bv.omni.check_output") as output):
            self.speaker._load()
        worker.synth.assert_called_once_with(slot, "Проверяю озвучку чата.", 100, "balanced", timeout=120)
        output.assert_called_once_with(self.audio)

    def test_balanced_queues_first_sentence_before_synthesizing_rest(self):
        first = "Собираем группу у входа."
        second = "Подождите минуту у ворот и проверьте, всё ли вы купили перед выходом."

        def render(slot, text, line, generation, trailing_gap, **kwargs):
            self.assertEqual(self.player.play.call_count, 0 if trailing_gap else 1)
            return self.audio

        with patch.object(self.speaker, "_render", side_effect=render) as synthesis:
            self.speaker._read_line(self.line(first + " " + second), 3)
        self.assertEqual([call.args[1] for call in synthesis.call_args_list], [first, second])
        self.assertIs(self.player.play.call_args_list[0].kwargs["window"], self.player.play.call_args.kwargs["window"])

    def test_slow_cuda_falls_back_once_and_ignores_late_audio(self):
        line = self.line("Проверка чата.")

        def render(*args, on_slow, **kwargs):
            on_slow()
            return self.audio

        with patch.object(self.speaker, "_render", side_effect=render):
            self.speaker._read_line(line, 3)
        self.speaker.on_fallback.assert_called_once()
        self.assertGreater(self.speaker.on_fallback.call_args.args[0].expires_at, time.monotonic())
        self.player.play.assert_not_called()
        self.assertFalse(self.speaker.slow.is_set())

    def test_expiration_does_not_affect_manual_replay(self):
        with patch.object(self.speaker, "_render", return_value=self.audio) as synthesis:
            self.speaker._read_line(self.line("Старый чат.", age=30), 3)
            synthesis.assert_not_called()
            self.speaker._read_line(self.line("Повтор.", age=30, options={"replay": "1"}), 3)
            synthesis.assert_called_once()
        self.speaker.on_fallback.assert_not_called()

    def test_playback_drops_stale_messages_and_keeps_started_continuations(self):
        output = Mock()
        player = Player(out=output)
        window = PlaybackWindow(time.monotonic() - 1)
        player.play(self.audio, window=window)
        player._play(player.items.get_nowait())
        output.play.assert_not_called()
        window.started = True
        player.play(self.audio, window=window)
        player._play(player.items.get_nowait())
        output.play.assert_called_once()
