"""Sample camera sharpness and motion only while selecting an inference frame."""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps


CAPTURE_TIMEOUT = 3.0
STABLE_SECONDS = 0.25
MIN_SAMPLES = 3
MIN_SAMPLE_SPACING = 0.055
MAX_SAMPLE_GAP = 0.22
SAMPLE_BOX = (128, 96)
MIN_LAPLACIAN_ENERGY = 52.0
MIN_EDGE_FRACTION = 0.015
MAX_MEAN_CHANGE = 7.0
MAX_CHANGED_FRACTION = 0.08


def scene_thumbnail(rgb: np.ndarray) -> np.ndarray:
    image = ImageOps.contain(Image.fromarray(rgb), SAMPLE_BOX, Image.Resampling.BILINEAR)
    return np.asarray(image, dtype=np.int16)


def scene_changed(previous: np.ndarray, current: np.ndarray) -> bool:
    if previous.shape != current.shape:
        return True
    change = np.abs(previous - current).max(axis=2)
    return bool(np.mean(change) >= 6 or np.mean(change >= 24) >= 0.035)


@dataclass(frozen=True)
class StableFrame:
    rgb: np.ndarray
    frame_number: int
    received_at: str
    received_monotonic: float


def _is_sharp(gray: np.ndarray) -> bool:
    if min(gray.shape) < 16:
        return False
    center = gray[1:-1, 1:-1]
    laplacian = gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4 * center
    return bool(np.mean(laplacian ** 2) >= MIN_LAPLACIAN_ENERGY
                and np.mean(np.abs(laplacian) >= 10) >= MIN_EDGE_FRACTION)


def _is_similar(first: np.ndarray, second: np.ndarray) -> bool:
    if first.shape != second.shape:
        return False
    change = np.abs(first - second)
    return bool(np.mean(change) <= MAX_MEAN_CHANGE
                and np.mean(change >= 24) <= MAX_CHANGED_FRACTION)


class FrameStability:
    def __init__(self) -> None:
        self._last_sample: float | None = None
        self._run_started = 0.0
        self._run_samples = 0
        self._anchor: np.ndarray | None = None
        self._previous: np.ndarray | None = None

    def accepts(self, image: Image.Image, received_at: float) -> bool:
        gap = received_at - self._last_sample if self._last_sample is not None else float('inf')
        if gap < MIN_SAMPLE_SPACING:
            return False
        self._last_sample = received_at

        thumbnail = ImageOps.contain(image, SAMPLE_BOX, Image.Resampling.BILINEAR)
        gray = np.asarray(thumbnail.convert('L'), dtype=np.float32)
        if not _is_sharp(gray):
            self._anchor = self._previous = None
            self._run_samples = 0
            return False

        # Keep the run's first thumbnail so small per-frame movements cannot
        # add up unnoticed while the camera drifts.
        if (gap > MAX_SAMPLE_GAP or self._anchor is None or self._previous is None
                or not _is_similar(self._previous, gray) or not _is_similar(self._anchor, gray)):
            self._anchor = gray
            self._run_started = received_at
            self._run_samples = 0
        self._previous = gray
        self._run_samples += 1
        return self._run_samples >= MIN_SAMPLES and received_at - self._run_started >= STABLE_SECONDS
