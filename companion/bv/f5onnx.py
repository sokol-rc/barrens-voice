"""F5-TTS through ONNX Runtime (0.4.0): the race voices without torch, on the video card - CUDA on an
NVIDIA card (onnxruntime-gpu), else DirectML on any DirectX 12 card (f5engine.PREFERRED).

The models are the Russian F5 (Misha24-10/F5-TTS_RUSSIAN) exported by DakeQQ/F5-TTS-ONNX: four graphs
in one folder - Metadata (sample rates, hop, the number of steps), Preprocess (reference audio and text
to the starting noise and the conditions), Transformer (one denoising step; our export runs 10 of them,
`nfe_step` in Metadata, fixed at export with CFG 2 and sway -1, as the torch F5 the companion used),
Decode (Vocos: mel to audio). Preprocess and the Transformer run on the card; Decode on the CPU - on
DirectML it gives silence (the exporter says so).

The length of the speech is the caller's, as with the torch engine's `fix_duration`: the total seconds
(reference and line) become the frames of `max_duration`.
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

PUNCTUATION = str.maketrans({";": ",", "“": '"', "”": '"', "‘": "'", "’": "'"})


def read_vocab(path: Path) -> dict[str, int]:
    """F5's vocab.txt read as F5 reads it: one symbol per line (the line without its line break, so a
    line holding a space is the space), its index the line's number."""
    out: dict[str, int] = {}
    lines = Path(path).read_text(encoding="utf-8").split("\n")   # only "\n": a file is read line by line
    if lines and lines[-1] == "":
        lines.pop()
    for i, line in enumerate(lines):
        out[line] = i
    return out


def text_ids(text: str, vocab: dict[str, int]) -> np.ndarray:
    """The text as the model's symbol indices, as F5 makes them for text with no Chinese in it: one per
    character (after its few punctuation replacements); a symbol the vocabulary lacks is index 0."""
    return np.array([[vocab.get(ch, 0) for ch in text.translate(PUNCTUATION)]], dtype=np.int32)


TAIL_FRAMES = 2       # Decode's last frames: a burst at full scale, heard as a click when the line stops
FADE_SECONDS = 0.01


def clean_tail(samples: np.ndarray, hop: int, rate: int) -> np.ndarray:
    """The line without Decode's last TAIL_FRAMES frames and with a short fade-out. The exported Vocos
    ends every line with a few dozen samples at ±1 (torch's F5 gives the line two frames shorter, ending
    in silence): heard as a click, and that false peak also made the line quieter in `tidy`."""
    out = samples[:max(0, len(samples) - TAIL_FRAMES * hop)].copy()
    n = min(len(out), int(rate * FADE_SECONDS))
    if n:
        out[-n:] *= np.linspace(1.0, 0.0, n, dtype=out.dtype)
    return out


DEVICES = {"DmlExecutionProvider": "dml", "CUDAExecutionProvider": "cuda"}


class F5Onnx:
    def __init__(self, model_dir: Path, vocab_path: Path, provider: str = "DmlExecutionProvider", log=print,
                 *, steps: int | None = None, cache: Path | None = None):
        self.model_dir, self.vocab_path, self.provider, self.log = Path(model_dir), Path(vocab_path), provider, log
        self.fast_steps = steps if provider == "CUDAExecutionProvider" else None
        self.cache = cache
        self.accelerated = False
        self.lock = threading.Lock()
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return
        import onnxruntime as ort
        meta = ort.InferenceSession(str(self.model_dir / "F5_Metadata.onnx"),
                                    providers=["CPUExecutionProvider"]).get_modelmeta().custom_metadata_map
        self.sample_rate = int(meta["sample_rate"])
        self.in_rate = int(meta["in_sample_rate"])
        self.out_rate = int(meta["out_sample_rate"])
        self.hop = int(meta["hop_length"])
        self.steps = int(meta["nfe_step"])
        self.max_frames = int(meta["max_signal_length"])
        from .f5engine import card_session, on_card
        options, card = card_session(self.provider)
        self.pre = ort.InferenceSession(str(self.model_dir / meta["model_file_name_preprocess"]), options, providers=card)
        self.transformer_path = self.model_dir / meta["model_file_name_transformer"]
        self.decode_path = self.model_dir / meta["model_file_name_decode"]
        self.export_steps = self.steps
        if self.fast_steps:
            try:
                from .f5schedule import transformer
                path = transformer(self.transformer_path, self.cache, self.fast_steps)
                self.net = ort.InferenceSession(str(path), options, providers=card)
                on_card(self.net, self.provider)
                self.cuda_dec = ort.InferenceSession(str(self.decode_path), options, providers=card)
                on_card(self.cuda_dec, self.provider)
                self.steps = self.fast_steps
                self.time_ids = [ort.OrtValue.ortvalue_from_numpy(np.array([i], np.int32)) for i in range(self.steps)]
                self.scratch = None
                self.verify_decode = True
                self.accelerated = True
            except Exception as e:
                self.log(f"Ускорение F5 недоступно, включаю quality: {e}")
        if not self.accelerated:
            self._quality()
        self.dec = ort.InferenceSession(str(self.decode_path), providers=["CPUExecutionProvider"])
        types = {"tensor(float)": np.float32, "tensor(float16)": np.float16, "tensor(int16)": np.int16}
        self.audio_type = types[self.pre.get_inputs()[0].type]
        self.vocab = read_vocab(self.vocab_path)
        self.device = DEVICES.get(self.provider, "cpu")     # where IO binding keeps the noise between steps
        self.loaded = True
        self.log(f"F5 (ONNX, {self.net.get_providers()[0]}) готов: {self.steps} шагов")

    def _quality(self):
        import onnxruntime as ort
        from .f5engine import card_session, on_card
        self.accelerated = False
        self.cuda_dec = self.scratch = self.net = None
        self.steps = self.export_steps
        options, providers = card_session(self.provider)
        self.net = ort.InferenceSession(str(self.transformer_path), options, providers=providers)
        on_card(self.net, self.provider)

    def frames(self, seconds: float) -> int:
        return int(seconds * self.sample_rate / self.hop)

    def synth(self, ref_audio: np.ndarray, ref_text: str, text: str, total_seconds: float) -> tuple[np.ndarray, int]:
        """The line in the reference's voice: `ref_audio` mono float at `in_rate`, `total_seconds` the
        length of reference and line together. Returns (audio, sample rate) of the line alone."""
        self.load()
        if not ref_text.endswith(". ") and not ref_text.endswith("。"):   # as F5's own preprocessing
            ref_text += " " if ref_text.endswith(".") else ". "
        ids = text_ids(ref_text + text, self.vocab)
        if self.audio_type == np.int16:
            audio = np.clip(ref_audio * 32767, -32768, 32767).astype(np.int16)
        else:
            audio = ref_audio.astype(self.audio_type, copy=False)
        ref_frames = int(len(ref_audio) * self.sample_rate / self.in_rate) // self.hop
        duration = max(ids.shape[1], ref_frames + 1) + 1
        duration = min(max(duration, self.frames(total_seconds)), self.max_frames)
        with self.lock:
            feed = {"audio": audio.reshape(1, 1, -1), "text_ids": ids, "max_duration": np.array([duration], np.int64)}
            if self.accelerated:
                try:
                    out = self._cuda(feed)
                except Exception as e:
                    self.log(f"Ускоренный синтез не удался, повторяю в quality: {e}")
                    self._quality()
            if not self.accelerated:
                noise, cos, sin, cond, cond_drop, ref_len, rms, tail = self.pre.run(None, feed)
                fixed = {"rope_cos": cos, "rope_sin": sin, "cat_mel_text": cond, "cat_mel_text_drop": cond_drop}
                noise = self._denoise(noise, fixed)
                out = self.dec.run(None, {"denoised": noise, "ref_signal_len": ref_len, "rms_scale": rms,
                                          "ref_mel_tail": tail})[0]
        return clean_tail(np.asarray(out, dtype=np.float32).reshape(-1), self.hop, self.out_rate), self.out_rate

    def _cuda(self, feed):
        import onnxruntime as ort
        binding = self.pre.io_binding()
        for name, value in feed.items():
            binding.bind_cpu_input(name, value)
        for output in self.pre.get_outputs():
            binding.bind_output(output.name, "cuda")
        self.pre.run_with_iobinding(binding)
        noise, cos, sin, cond, drop, ref_len, rms, tail = binding.get_outputs()
        if self.scratch is None or self.scratch.shape() != noise.shape():
            self.scratch = ort.OrtValue.ortvalue_from_shape_and_type(noise.shape(), np.float16, "cuda", 0)
        fixed = {"rope_cos": cos, "rope_sin": sin, "cat_mel_text": cond, "cat_mel_text_drop": drop}
        denoised = self._bound_steps((noise, self.scratch), fixed, self.time_ids)
        inputs = {"denoised": denoised, "ref_signal_len": ref_len, "rms_scale": rms, "ref_mel_tail": tail}
        b = self.cuda_dec.io_binding()
        for name, value in inputs.items():
            b.bind_ortvalue_input(name, value)
        b.bind_output(self.cuda_dec.get_outputs()[0].name, "cpu")
        self.cuda_dec.run_with_iobinding(b)
        out = b.get_outputs()[0].numpy()
        # The exported vocoder can leave a full-scale click in its final two frames even on silence.
        speech = out.reshape(-1)[:max(0, out.size - TAIL_FRAMES * self.hop)]
        if not speech.size or not np.isfinite(out).all() or np.max(np.abs(speech)) < 1e-5:
            raise ValueError("CUDA Decode вернул тишину или неверные значения")
        if self.verify_decode:
            reference = self.dec.run(None, {k: v.numpy() for k, v in inputs.items()})[0]
            if (not np.isfinite(reference).all() or out.shape != reference.shape
                    or np.linalg.norm(out - reference) > 0.05 * np.linalg.norm(reference)):
                raise ValueError("результат CUDA Decode расходится с CPU")
            self.verify_decode = False
        return out

    def _denoise(self, noise: np.ndarray, fixed: dict) -> np.ndarray:
        """The denoising steps. On the card the conditions go there once and the noise stays there,
        handed between two buffers from step to step (IO binding): sending them each step cost more
        than the steps themselves (2.3-4.1 s a line instead of the exporter's 1.3-3.2 s)."""
        import onnxruntime as ort
        if self.device == "cpu":
            for step in range(self.steps):
                noise = self.net.run(None, {"noise": noise, **fixed, "time_step": np.array([step], np.int32)})[0]
            return noise
        on_card = {k: ort.OrtValue.ortvalue_from_numpy(np.ascontiguousarray(v), self.device, 0) for k, v in fixed.items()}
        buffers = (ort.OrtValue.ortvalue_from_numpy(np.ascontiguousarray(noise), self.device, 0),
                   ort.OrtValue.ortvalue_from_shape_and_type(noise.shape, noise.dtype, self.device, 0))
        steps = [ort.OrtValue.ortvalue_from_numpy(np.array([s], np.int32)) for s in range(self.steps)]
        return self._bound_steps(buffers, on_card, steps).numpy()

    def _bound_steps(self, buffers, on_card, steps):
        bindings = []
        for i in range(2):
            b = self.net.io_binding()
            for name, value in on_card.items():
                b.bind_ortvalue_input(name, value)
            b.bind_ortvalue_input("noise", buffers[i])
            b.bind_ortvalue_output("denoised", buffers[1 - i])
            bindings.append(b)
        for step in range(self.steps):
            b = bindings[step & 1]
            b.bind_ortvalue_input("time_step", steps[step])
            self.net.run_with_iobinding(b)
        return buffers[self.steps & 1]
