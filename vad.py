"""Silero VAD wrapper using ONNX runtime (no PyTorch needed).

The model processes 512-sample chunks (32ms at 16kHz) and returns a speech
probability between 0 and 1. Internal LSTM state is carried between calls
via the SileroVAD class, so each WebSocket session should have its own instance.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort

log = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "models" / "silero_vad.onnx"
CHUNK_SAMPLES = 512  # 32ms at 16kHz — required by Silero VAD
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 4  # float32
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE


CONTEXT_SAMPLES = 64  # prepended to each 512-sample window (required by Silero v6)


class SileroVAD:
    """Per-session Silero VAD instance with its own LSTM state."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._session = ort.InferenceSession(
            str(MODEL_PATH),
            providers=["CPUExecutionProvider"],
        )
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def process_chunk(self, pcm_bytes: bytes) -> list[float]:
        """Run VAD on a PCM buffer. Returns speech probabilities for each 512-sample window.

        The input can be any length; it will be split into 512-sample chunks.
        Leftover samples shorter than 512 are ignored.
        """
        audio = np.frombuffer(pcm_bytes, dtype=np.float32)
        probabilities = []

        for offset in range(0, len(audio) - CHUNK_SAMPLES + 1, CHUNK_SAMPLES):
            chunk = audio[offset:offset + CHUNK_SAMPLES].reshape(1, -1)
            # Silero expects context (64 samples) prepended to each window
            x = np.concatenate([self._context, chunk], axis=1)
            out, self._state = self._session.run(
                None,
                {
                    "input": x,
                    "sr": self._sr,
                    "state": self._state,
                },
            )
            self._context = x[:, -CONTEXT_SAMPLES:]
            probabilities.append(float(out[0][0]))

        return probabilities

    def is_speech(self, pcm_bytes: bytes) -> bool:
        """Returns True if any window in the chunk exceeds the speech threshold."""
        probs = self.process_chunk(pcm_bytes)
        return any(p >= self.threshold for p in probs)
