"""Bounded in-memory speech endpoints and wake-phrase matching."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import deque

import numpy as np


SAMPLE_RATE = 16_000
WINDOW_MS = 20
WINDOW_SAMPLES = SAMPLE_RATE * WINDOW_MS // 1_000
MAX_SECONDS = 20
ONSET_WINDOWS = 4
MIN_VOICED_WINDOWS = 6
PRE_ROLL_WINDOWS = 12
TAIL_WINDOWS = 6
START_RMS_SQUARED = 320**2
CONTINUE_RMS_SQUARED = 220**2

_WAKE_PHRASE = re.compile(
    r"^[\s\"'“‘]*(?:hey|헤이)[\s,，\-–—]*(?:sense|센스|센쓰|쎈스)(?=$|[\s,.:;!?\"'”’、。–—\-])",
    re.IGNORECASE,
)
_WAKE_DELIMITERS = " \t\r\n,.:;!?、。-–—"


def split_wake_phrase(text: str) -> str | None:
    """Extract a question following an anchored wake phrase."""
    # Match using the normalized text. Keep the ordinary question punctuation.
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = _WAKE_PHRASE.match(normalized)
    if match is None:
        return None
    suffix = normalized[match.end():].lstrip(_WAKE_DELIMITERS)
    return suffix if suffix.strip("\"'”’" + _WAKE_DELIMITERS) else ""


class VoiceActivity:
    """Endpoint one utterance from 16 kHz int16 PCM using short energy windows."""

    def __init__(self, *, end_silence_ms: int = 600, max_seconds: float = MAX_SECONDS):
        if not math.isfinite(end_silence_ms) or not 0 < end_silence_ms <= MAX_SECONDS * 1_000:
            raise ValueError("end_silence_ms must be between 0 and 20,000")
        min_seconds = (PRE_ROLL_WINDOWS + MIN_VOICED_WINDOWS) * WINDOW_MS / 1_000
        if not math.isfinite(max_seconds) or not min_seconds <= max_seconds <= MAX_SECONDS:
            raise ValueError(f"max_seconds must be between {min_seconds} and {MAX_SECONDS}")
        self._end_windows = math.ceil(end_silence_ms / WINDOW_MS)
        self._max_windows = int(max_seconds * SAMPLE_RATE) // WINDOW_SAMPLES
        self._pre_roll = deque(maxlen=PRE_ROLL_WINDOWS + ONSET_WINDOWS)
        self._speech: list[np.ndarray] = []
        self.reset()

    def reset(self) -> None:
        self._partial = np.empty(0, dtype=np.int16)
        self._pre_roll.clear()
        self._speech.clear()
        self._onset = self._voiced = self._quiet = 0
        self._waiting_for_quiet = False

    def push(self, pcm: np.ndarray) -> np.ndarray | None:
        """Process the input and return the first completed utterance in this call, if any."""
        data = np.asarray(pcm)
        if data.ndim == 2 and 1 in data.shape:
            data = data.reshape(-1)
        if data.ndim != 1 or data.dtype != np.int16:
            raise ValueError("VoiceActivity expects 16 kHz mono int16 PCM")
        if self._partial.size:
            missing = WINDOW_SAMPLES - self._partial.size
            if data.size < missing:
                self._partial = np.concatenate((self._partial, data))
                return None
            first = self._push_window(np.concatenate((self._partial, data[:missing])))
            data = data[missing:]
        else:
            first = None

        limit = data.size // WINDOW_SAMPLES * WINDOW_SAMPLES
        for offset in range(0, limit, WINDOW_SAMPLES):
            utterance = self._push_window(data[offset:offset + WINDOW_SAMPLES])
            if first is None:
                first = utterance
        self._partial = data[limit:].copy()
        return first

    def _push_window(self, window: np.ndarray) -> np.ndarray | None:
        # Variance is squared AC RMS, so a constant input offset does not trigger speech.
        energy = np.var(window.astype(np.float32))
        if self._waiting_for_quiet:
            self._quiet = self._quiet + 1 if energy < CONTINUE_RMS_SQUARED else 0
            if self._quiet >= self._end_windows:
                self._waiting_for_quiet = False
                self._quiet = 0
            return None

        if not self._speech:
            self._pre_roll.append(window.copy())
            self._onset = self._onset + 1 if energy >= START_RMS_SQUARED else 0
            if self._onset == ONSET_WINDOWS:
                self._speech.extend(self._pre_roll)
                self._pre_roll.clear()
                self._voiced = self._onset
            return None

        self._speech.append(window.copy())
        if energy >= CONTINUE_RMS_SQUARED:
            self._voiced += 1
            self._quiet = 0
        else:
            self._quiet += 1

        endpoint = self._quiet >= self._end_windows
        at_limit = len(self._speech) >= self._max_windows
        if not endpoint and not at_limit:
            return None
        keep_windows = len(self._speech) - max(0, self._quiet - TAIL_WINDOWS)
        utterance = np.concatenate(self._speech[:keep_windows]) if self._voiced >= MIN_VOICED_WINDOWS else None
        self._speech.clear()
        self._onset = self._voiced = 0
        # A sustained sound can trigger at most once until there is a quiet gap.
        self._waiting_for_quiet = at_limit and not endpoint
        if not self._waiting_for_quiet:
            self._quiet = 0
        return utterance
