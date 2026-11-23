"""Whisper through ONNX Runtime (0.4.0): it hears the client's voice lines while the voice bank is built,
the checker gamevoices.build takes (load, transcribe, score).

The model is OpenAI's whisper-large-v3-turbo (MIT) as onnx-community exports it: the encoder and the
decoder without a cache, in float16. The turbo decoder has 4 layers and a voice line is a few seconds,
so running it over the whole prefix for each new token costs little, and the loop stays plain: the
prompt (start, Russian, transcribe, no timestamps), then the likeliest token until the end one, with
the model's own suppressed tokens. The log-mel features and the tokenizer are transformers' (numpy
code: no torch).
"""
from __future__ import annotations

import numpy as np

from .refcheck import similarity

REPO = "onnx-community/whisper-large-v3-turbo"
FILES = ("onnx/encoder_model_fp16.onnx", "onnx/decoder_model_fp16.onnx", "config.json", "generation_config.json",
         "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
         "added_tokens.json", "special_tokens_map.json", "normalizer.json")
RATE = 16000
MAX_TOKENS = 120          # a voice line is a few seconds: far below Whisper's 448


def resample(samples: np.ndarray, rate: int, to: int = RATE) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if rate == to:
        return samples
    try:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(rate, to)
        return resample_poly(samples, to // g, rate // g).astype(np.float32)
    except ImportError:                   # linear: good enough to hear a few words
        n = int(round(len(samples) * to / rate))
        return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


class OnnxChecker:
    def __init__(self, log, provider: str = "DmlExecutionProvider"):
        self.log, self.provider = log, provider
        self.encoder = self.decoder = None

    def load(self) -> bool:
        try:
            import json

            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from transformers import WhisperFeatureExtractor, WhisperTokenizerFast
            paths = {f: hf_hub_download(REPO, f) for f in FILES}
            folder = str(__import__("pathlib").Path(paths["config.json"]).parent)
            self.features = WhisperFeatureExtractor.from_pretrained(folder)
            self.tokenizer = WhisperTokenizerFast.from_pretrained(folder)
            gen = json.loads(open(paths["generation_config.json"], encoding="utf-8").read())
            self.prompt = [gen["decoder_start_token_id"], gen["lang_to_id"]["<|ru|>"], gen["task_to_id"]["transcribe"],
                           gen["no_timestamps_token_id"]]
            self.eos = gen["eos_token_id"]
            self.suppress = np.array(gen.get("suppress_tokens") or [], dtype=np.int64)
            self.begin_suppress = np.array(gen.get("begin_suppress_tokens") or [], dtype=np.int64)
            from .f5engine import card_session, on_card
            options, providers = card_session(self.provider)    # the same card as F5's
            self.encoder = ort.InferenceSession(paths["onnx/encoder_model_fp16.onnx"], options, providers=providers)
            on_card(self.encoder, self.provider)            # not quietly on the processor: the bank would take ages
            self.decoder = ort.InferenceSession(paths["onnx/decoder_model_fp16.onnx"], options, providers=providers)
            on_card(self.decoder, self.provider)
            self.feature_type = np.float16 if "float16" in self.encoder.get_inputs()[0].type else np.float32
            return True
        except Exception as e:  # noqa: BLE001 - reported; the bank cannot be made without it
            self.log(f"Whisper (ONNX) недоступен: {e}")
            return False

    def transcribe(self, samples, rate: int) -> str:
        audio = resample(samples, rate)
        feats = self.features(audio, sampling_rate=RATE, return_tensors="np").input_features.astype(self.feature_type)
        hidden = self.encoder.run(None, {"input_features": feats})[0]
        tokens = list(self.prompt)
        for i in range(MAX_TOKENS):
            logits = self.decoder.run(None, {"input_ids": np.array([tokens], dtype=np.int64),
                                             "encoder_hidden_states": hidden})[0][0, -1].astype(np.float32)
            if self.suppress.size:
                logits[self.suppress] = -np.inf
            if i == 0 and self.begin_suppress.size:
                logits[self.begin_suppress] = -np.inf
            token = int(np.argmax(logits))
            if token == self.eos:
                break
            tokens.append(token)
        return self.tokenizer.decode(tokens[len(self.prompt):], skip_special_tokens=True)

    def score(self, samples, rate: int, expected: str) -> tuple[float, str]:
        heard = self.transcribe(samples, rate)
        return similarity(expected, heard), heard
