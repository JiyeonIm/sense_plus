"""Bounded model interfaces. Media and transcripts are processed in memory."""

from __future__ import annotations

import base64
import asyncio
import binascii
import io
import json
import wave
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from .config import Config

MAX_RESPONSE_BYTES = 3_000_000
MAX_WAV_BYTES = 2_000_000
MAX_REQUEST_BYTES = 2_000_000
OUTPUT_RATE = 24000


class AdapterError(Exception):
    """A safe user-facing failure, deliberately without remote credentials."""


class TeamResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["info", "direction", "obstacle", "uncertain"] = "info"
    direction: Literal["left", "right", "ahead", "none"] = "none"
    audio_wav_base64: str | None = Field(default=None, max_length=2_700_000)


@dataclass
class AnalysisResult:
    text: str
    kind: str = "info"
    direction: str = "none"
    audio: np.ndarray | None = None
    audio_provider: str | None = None


class AIAdapter(Protocol):
    async def analyze(self, payload: dict) -> AnalysisResult:
        """Analyze a single prepared image and optional question audio."""
        ...


def create_adapter(config: Config) -> AIAdapter:
    if config.ollama_model:
        return LocalAdapter(config)
    return TeamAdapter(config)


def jpeg_bytes(rgb: np.ndarray, *, max_size: tuple[int, int] | None = None) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(rgb.astype(np.uint8, copy=False), mode="RGB")
    if max_size is not None:
        image.thumbnail(max_size)
    image.save(output, format="JPEG", quality=75, optimize=False)
    return output.getvalue()


def encode_jpeg(rgb: np.ndarray) -> str:
    return base64.b64encode(jpeg_bytes(rgb, max_size=(640, 480))).decode("ascii")


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.astype("<i2", copy=False).tobytes())
    return out.getvalue()


def encode_wav(samples: np.ndarray, sample_rate: int) -> str:
    return base64.b64encode(wav_bytes(samples, sample_rate)).decode("ascii")


def read_question_pcm(value: str) -> np.ndarray:
    """Read the app's existing 16 kHz mono question envelope without resampling."""
    try:
        if len(value) > 860_000:
            raise ValueError("question WAV too large")
        with wave.open(io.BytesIO(base64.b64decode(value, validate=True)), "rb") as wav:
            count = wav.getnframes()
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
                raise ValueError("question PCM format")
            if not 0 < count <= 320_000:
                raise ValueError("question PCM length")
            samples = np.frombuffer(wav.readframes(count), dtype="<i2")
            if samples.size != count:
                raise ValueError("incomplete WAV")
            return samples
    except (binascii.Error, wave.Error, EOFError, ValueError) as exc:
        raise AdapterError("질문 음성 형식을 확인할 수 없습니다.") from exc


def decode_wav(value: str) -> np.ndarray:
    """Accept only bounded uncompressed 16-bit PCM WAV, then resample to 24k."""
    try:
        data = base64.b64decode(value, validate=True)
        if len(data) > MAX_WAV_BYTES:
            raise AdapterError("AI 음성 응답이 허용 크기를 초과했습니다.")
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate, channels = wav.getframerate(), wav.getnchannels()
            if wav.getsampwidth() != 2 or channels not in {1, 2} or not 8000 <= rate <= 48000:
                raise AdapterError("AI 음성은 8~48kHz, 16비트 PCM WAV여야 합니다.")
            if wav.getnframes() / rate > 20:
                raise AdapterError("AI 음성 응답은 20초 이하여야 합니다.")
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        if not pcm.size or pcm.size % channels:
            raise AdapterError("AI 음성 응답이 비어 있거나 손상되었습니다.")
        if channels == 2:
            pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.int16)
        if rate != OUTPUT_RATE:
            count = max(1, round(pcm.size * OUTPUT_RATE / rate))
            pcm = np.interp(np.arange(count) * rate / OUTPUT_RATE, np.arange(pcm.size), pcm).astype(np.int16)
        return pcm.reshape(1, -1).copy()
    except AdapterError:
        raise
    except (binascii.Error, wave.Error, EOFError, ValueError, OverflowError) as exc:
        raise AdapterError("AI 음성 WAV 형식을 읽을 수 없습니다.") from exc


class TeamAdapter:
    def __init__(self, config: Config):
        self.config = config

    async def _post(self, encoded: bytes, headers: dict[str, str]) -> bytearray:
        async with httpx.AsyncClient(timeout=self.config.ai_timeout, follow_redirects=False) as client:
            async with client.stream("POST", self.config.ai_url, content=encoded, headers=headers) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise AdapterError("AI 응답이 허용 크기를 초과했습니다.")
        return raw

    async def analyze(self, payload: dict) -> AnalysisResult:
        if not self.config.ai_configured:
            raise AdapterError("AI가 연결되지 않았습니다. 카메라·음성 연결만 시험할 수 있습니다.")
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise AdapterError("질문 데이터가 허용 크기를 초과했습니다. 질문을 짧게 해 주세요.")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.ai_key:
            headers["Authorization"] = f"Bearer {self.config.ai_key}"
        try:
            # httpx read timeouts are per chunk; wait_for also caps the whole
            # request when a server keeps dribbling response bytes indefinitely.
            raw = await asyncio.wait_for(self._post(encoded, headers), timeout=self.config.ai_timeout)
            parsed = TeamResponse.model_validate_json(raw)
            if not parsed.text.strip():
                raise AdapterError("AI 응답 텍스트가 비어 있습니다.")
            audio = decode_wav(parsed.audio_wav_base64) if parsed.audio_wav_base64 else None
            return AnalysisResult(parsed.text.strip(), parsed.kind, parsed.direction, audio,
                                  "team-api" if audio is not None else None)
        except AdapterError:
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            raise AdapterError("AI 응답 시간이 초과되었습니다. 잠시 뒤 다시 시도해 주세요.") from exc
        except httpx.HTTPStatusError as exc:
            raise AdapterError(f"AI 서버가 요청을 처리하지 못했습니다 (HTTP {exc.response.status_code}).") from exc
        except httpx.RequestError as exc:
            raise AdapterError("AI 서버에 연결할 수 없습니다.") from exc
        except (ValueError, TypeError) as exc:
            raise AdapterError("AI 응답 형식이 연결 규격과 맞지 않습니다.") from exc


class LocalAdapter:
    """Question PCM → Korean transcript → local vision → streamed reply audio."""

    def __init__(self, config: Config):
        from .speech import create_speech_providers
        from .vision import OllamaVision

        self.config = config
        self.vision = OllamaVision(config.ollama_model, config.ai_timeout)
        self.stt, self.tts = create_speech_providers(config.stt_model_path, config.tts_voice)

    async def analyze(self, payload: dict) -> AnalysisResult:
        from .speech import SpeechError
        from .vision import VisionError

        try:
            async with asyncio.timeout(self.config.ai_timeout):
                question = None
                if payload.get("audio_wav_base64"):
                    if self.stt is None:
                        raise AdapterError("PC의 음성 인식 모델이 설정되지 않았습니다.")
                    question = await self.stt.transcribe(read_question_pcm(payload["audio_wav_base64"]))
                result = await self.vision.analyze(
                    jpeg_base64=payload["jpeg_base64"], mode=payload["mode"], question=question,
                )
                audio = None
                if self.tts is not None:
                    try:
                        # Keep a valid text result usable even if local audio is unavailable.
                        audio = await asyncio.wait_for(self.tts.synthesize(result.text), timeout=4)
                    except (SpeechError, TimeoutError):
                        pass
                return AnalysisResult(result.text, result.kind, result.direction, audio,
                                      "windows-tts" if audio is not None else None)
        except (SpeechError, VisionError) as exc:
            raise AdapterError(str(exc)) from exc
        except TimeoutError as exc:
            raise AdapterError("PC에서 답변을 처리하는 시간이 초과되었습니다. 다시 시도해 주세요.") from exc
