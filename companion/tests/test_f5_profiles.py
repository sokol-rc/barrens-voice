"""The fast graphs, the line that fails over, and the first packet's place in the player queue."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bv import audio, f5engine, f5schedule
from bv.__main__ import DEFAULT_CONFIG
from bv.f5onnx import F5Onnx
from bv.protocol import Line


class Profiles(unittest.TestCase):
    def test_only_known_profiles_on_cuda_change_the_graph(self):
        for value in (None, {}, "other", "quality"):
            self.assertIsNone(f5engine.profile_steps({"f5_profile": value}, "CUDAExecutionProvider"))
        for name, count in f5engine.PROFILE_STEPS.items():
            self.assertEqual(f5engine.profile_steps({"f5_profile": name}, "CUDAExecutionProvider"), count)
            self.assertIsNone(f5engine.profile_steps({"f5_profile": name}, "DmlExecutionProvider"))
        self.assertEqual(DEFAULT_CONFIG["f5_profile"], "quality")

    def test_every_export_visits_the_same_end_with_matching_left_embeddings(self):
        time = np.linspace(0, 1, 11, dtype=np.float32)**2
        embedding = np.stack((time, time + 1), axis=-1)[None]
        for steps, boundaries in f5schedule.BOUNDARIES.items():
            emb, delta = f5schedule.tables(embedding, np.diff(time)[:, None, None], boundaries)
            self.assertEqual(len(delta), steps)
            np.testing.assert_allclose(emb[0, :, 0], time[list(boundaries)])
            np.testing.assert_allclose(np.cumsum(delta), emb[0, 1:, 0], atol=1e-7)
            self.assertAlmostEqual(float(delta.sum()), 1.0)

    def test_unknown_export_keeps_its_original_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "model.onnx"
            source.write_bytes(b"another export")
            with self.assertRaises(ValueError):
                f5schedule.transformer(source, Path(d) / "cache", 6)
            self.assertEqual(source.read_bytes(), b"another export")
            self.assertFalse((Path(d) / "cache").exists())

    def test_cuda_decode_load_failure_uses_original_transformer_and_cpu_vocoder(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            vocab = home / "vocab.txt"
            vocab.write_text("а\n", encoding="utf-8")
            model = F5Onnx(home, vocab, "CUDAExecutionProvider", log=lambda msg: None, steps=6, cache=home)
            meta = dict(sample_rate="24000", in_sample_rate="24000", out_sample_rate="24000",
                        hop_length="256", nfe_step="10", max_signal_length="4096",
                        model_file_name_preprocess="pre", model_file_name_transformer="net", model_file_name_decode="dec")
            calls = []
            def session(path, options=None, providers=None):
                name = Path(path).name
                names = [v[0] if isinstance(v, tuple) else v for v in providers]
                calls.append((name, names[0]))
                if name == "dec" and names[0] == "CUDAExecutionProvider":
                    raise RuntimeError("unsupported CUDA operator")
                return SimpleNamespace(get_modelmeta=lambda: SimpleNamespace(custom_metadata_map=meta),
                                       get_providers=lambda: names,
                                       get_inputs=lambda: [SimpleNamespace(type="tensor(float)")])
            runtime = SimpleNamespace(InferenceSession=session, SessionOptions=SimpleNamespace,
                                      GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=1))
            with mock.patch.dict(sys.modules, onnxruntime=runtime), \
                    mock.patch.object(f5schedule, "transformer", return_value=home / "reduced"):
                model.load()
            self.assertFalse(model.accelerated)
            self.assertEqual(model.steps, 10)
            self.assertIn(("dec", "CUDAExecutionProvider"), calls)
            self.assertEqual(calls[-2:], [("net", "CUDAExecutionProvider"), ("dec", "CPUExecutionProvider")])

    def test_failed_cuda_inference_retries_same_input_on_quality_once(self):
        model = F5Onnx(Path("."), Path("."), log=lambda msg: None)
        model.loaded = model.accelerated = True
        model.audio_type, model.vocab = np.float32, {}
        model.out_rate = model.in_rate = model.sample_rate = 24000
        model.hop, model.max_frames = 256, 4096
        model._cuda = mock.Mock(side_effect=RuntimeError("out of memory"))
        model._quality = mock.Mock(side_effect=lambda: setattr(model, "accelerated", False))
        model.pre = mock.Mock()
        model.pre.run.return_value = [object()] * 8
        model._denoise = mock.Mock()
        model.dec = mock.Mock()
        model.dec.run.return_value = [np.ones(2048, dtype=np.float32)]
        for _ in range(2):
            samples, rate = model.synth(np.zeros(2400, dtype=np.float32), "Образец.", "Ответ.", 2)
            self.assertEqual(rate, 24000)
            self.assertGreater(samples.max(), 0)
        model._quality.assert_called_once()
        model._cuda.assert_called_once()
        self.assertEqual(model.pre.run.call_count, 2)
        self.assertIs(model._cuda.call_args.args[0], model.pre.run.call_args_list[0].args[1])

    def test_tensorrt_runtime_failure_retries_on_cuda_before_quality(self):
        model = F5Onnx(Path("."), Path("."), log=lambda msg: None)
        model.using_trt = model.accelerated = True
        model._cuda = mock.Mock(side_effect=[RuntimeError("builder failed"), "audio"])
        model._cuda_transformer = mock.Mock(side_effect=lambda: setattr(model, "using_trt", False))
        model._quality = mock.Mock()
        feed = {"text_ids": np.zeros((1, 1), dtype=np.int32)}
        self.assertEqual(model._infer(feed), "audio")
        self.assertEqual(model._cuda.call_args_list, [mock.call(feed), mock.call(feed)])
        model._cuda_transformer.assert_called_once()
        model._quality.assert_not_called()

    def test_first_sentence_plays_before_the_rest_and_stop_drops_the_rest(self):
        for stop, expected in [(False, ["Один.", "play", "Два.", "play"]), (True, ["Один.", "play"])]:
            events = []
            player = SimpleNamespace(generation=0)
            def play(data, generation, *, low_latency):
                self.assertTrue(low_latency)
                events.append("play")
                if stop:
                    player.generation += 1
            player.play = play
            config = dict(DEFAULT_CONFIG, f5_profile="fastest")
            speaker = f5engine.F5Speaker(config, lambda msg: None, Path("."), player=player)
            speaker._load = lambda: None
            speaker.onnx = SimpleNamespace(accelerated=True)
            speaker.accentor = SimpleNamespace(accent=lambda text: text)
            speaker.assigner = SimpleNamespace(slot_for=lambda *args: "voice")
            def synth(slot, text, rate, sender):
                events.append(text)
                return np.zeros(2400, np.float32), 24000
            speaker._synth = synth
            speaker.say(Line("say", "Автор", "Orc", "2", "", "", "Один. Два."))
            speaker.lines.put(None)
            speaker._synth_loop()
            self.assertEqual(events, expected)

    def test_clause_packet_keeps_its_punctuation_and_questions_stay_whole(self):
        first = "Подождите у портала,"
        rest = "пока подойдут остальные участники группы и наш последний целитель."
        self.assertEqual(f5engine.first_packets(first + " " + rest), [first, rest])
        question = (first + " " + rest)[:-1] + "?"
        self.assertEqual(f5engine.first_packets(question), [question])
        self.assertEqual(f5engine.first_packets("Погоди, скоро буду."), ["Погоди, скоро буду."])

    def test_low_latency_stream_can_retry_with_normal_buffers(self):
        output = audio.StreamOut(SimpleNamespace())
        with mock.patch.object(output, "_stream", side_effect=[OSError("unsupported buffer"), True]) as stream:
            output.play(audio.wav(b"\0\0"*1000, 8000), lambda: 1, threading.Event(), 1, low_latency=True)
        self.assertEqual([call.args[-1] for call in stream.call_args_list], [True, False])
