"""OmniVoice applies saved profile changes to the next synthesis request."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bv.launcher.model import update_config
from bv.omni import OmniSpeaker
from bv.omni_worker import profile_steps


class OmniProfile(unittest.TestCase):
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
