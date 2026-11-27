"""Bounded per-session state and explicit question/frame pairing.

Button questions select a frame before enabling the microphone and accept late audio
at question.end. Voice questions select a fresh frame after recognizing the request.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
from PIL import Image

from .adapter import AIAdapter, AdapterError, AnalysisResult, OUTPUT_RATE, create_adapter, encode_jpeg, encode_wav, wav_bytes
from .capture import CAPTURE_TIMEOUT, FrameStability, StableFrame, scene_changed, scene_thumbnail
from .config import Config
from .wake import VoiceActivity, split_wake_phrase

INPUT_RATE = 16000
MAX_QUESTION_SECONDS = 20
AUDIO_WAVEFORM_BUCKETS = 40
AUDIO_WAVEFORM_BUCKET_SAMPLES = INPUT_RATE // AUDIO_WAVEFORM_BUCKETS
AUDIO_LEVEL_FRESH_MS = 1000
VIDEO_FPS_WINDOW = 1.5
AI_UNCONNECTED = "AI가 연결되지 않았습니다. 카메라·음성 연결만 시험할 수 있습니다."
VOICE_STARTUP = "센서 플러스가 실행되었습니다."
VOICE_QUESTION_TIMEOUT = 12.0
VOICE_PLAYBACK_GRACE = 0.25
VOICE_PLAYBACK_FETCH_MARGIN = 2.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


@dataclass
class PendingCapture:
    result: asyncio.Future[StableFrame | None]
    deadline: float
    generation: int | None = None
    stability: FrameStability = field(default_factory=FrameStability)


@dataclass
class Session:
    session_id: str
    token: str
    last_seen: float = field(default_factory=time.monotonic)
    created_at: str = field(default_factory=utc_now)
    transport: str = "new"
    mode: str = "guide"
    video_frames: int = 0
    audio_samples: int = 0
    audio_frames: int = 0
    last_audio_at: float = 0.0
    audio_rms: float = 0.0
    audio_peaks: deque[float] = field(default_factory=lambda: deque(maxlen=AUDIO_WAVEFORM_BUCKETS))
    audio_peak_in_progress: float = 0.0
    audio_peak_samples: int = 0
    analysis_ms: int | None = None
    last_ai_frame_number: int | None = None
    last_ai_frame_received_at: str | None = None
    latest_video: np.ndarray | None = None
    video_size: tuple[int, int] | None = None
    last_video_at: float = 0.0
    last_video_wall: str | None = None
    _video_receipts: deque[float] = field(default_factory=lambda: deque(maxlen=240))
    pending_capture: PendingCapture | None = None
    busy: bool = False
    last_error: str | None = None
    latest_result: dict | None = None
    guide_active: bool = False
    paused: bool = False
    scene_waiting_for_video: bool = False
    resume_scene_after_question: bool = False
    recording: bool = False
    draining: bool = False
    current_turn_id: str | None = None
    generation: int = 0
    question_jpeg: str | None = None
    question_frame_number: int | None = None
    question_frame_at: str | None = None
    question_started_at: float = 0.0
    question_audio: list[np.ndarray] = field(default_factory=list)
    question_samples: int = 0
    audio_truncated: bool = False
    task: asyncio.Task | None = None
    output_audio: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2))
    flush_audio: Any = None
    answer_audio_http: bool = False
    latest_answer_wav: bytes | None = field(default=None, repr=False)
    voice_wake_enabled: bool = False
    voice_vad: VoiceActivity = field(default_factory=VoiceActivity, repr=False)
    voice_task: asyncio.Task | None = field(default=None, repr=False)
    voice_question_timer: asyncio.Task | None = field(default=None, repr=False)
    voice_waiting_for_question: bool = False
    voice_processing: bool = False
    voice_playback_id: str | None = None
    voice_playback_seconds: float = 0
    voice_playback_until: float = 0
    closed: bool = False

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def clear_audio_level(self) -> None:
        self.audio_rms = 0.0
        self.audio_peaks.clear()
        self.audio_peak_in_progress = 0.0
        self.audio_peak_samples = 0

    def update_audio_level(self, data: np.ndarray) -> None:
        # Each bucket retains just one peak magnitude from 25 ms of PCM.
        # Packet boundaries can differ from bucket boundaries.
        amplitudes = np.abs(data.astype(np.float32)) / 32768.0
        self.audio_rms = float(np.sqrt(np.mean(amplitudes ** 2)))
        offset = 0
        while offset < amplitudes.size:
            count = min(AUDIO_WAVEFORM_BUCKET_SAMPLES - self.audio_peak_samples, amplitudes.size - offset)
            self.audio_peak_in_progress = max(self.audio_peak_in_progress, float(amplitudes[offset:offset + count].max()))
            self.audio_peak_samples += count
            offset += count
            if self.audio_peak_samples == AUDIO_WAVEFORM_BUCKET_SAMPLES:
                self.audio_peaks.append(self.audio_peak_in_progress)
                self.audio_peak_in_progress = 0.0
                self.audio_peak_samples = 0

    def video_age_ms(self) -> int | None:
        return round((time.monotonic() - self.last_video_at) * 1000) if self.latest_video is not None else None

    def count_video_receipt(self, received_at: float) -> None:
        window_start = received_at - VIDEO_FPS_WINDOW
        while self._video_receipts and self._video_receipts[0] < window_start:
            self._video_receipts.popleft()
        self._video_receipts.append(received_at)

    def video_fps(self) -> float | None:
        if not self._video_receipts:
            return None
        if self.latest_video is None or time.monotonic() - self.last_video_at > VIDEO_FPS_WINDOW:
            return 0.0
        elapsed = self._video_receipts[-1] - self._video_receipts[0]
        return round((len(self._video_receipts) - 1) / elapsed, 1) if elapsed > 0 else None

    def cancel_pending_capture(self) -> None:
        capture, self.pending_capture = self.pending_capture, None
        if capture is not None and not capture.result.done():
            capture.result.set_result(None)

    def voice_wake_phase(self) -> str:
        if not self.voice_wake_enabled or self.paused or self.transport != "connected":
            return "off"
        if self.voice_playback_until > time.monotonic():
            return "playing"
        if self.voice_waiting_for_question or self.recording:
            return "question"
        if self.voice_processing or self.draining:
            return "processing"
        return "waiting"

    def public_state(self, ai_configured: bool) -> dict:
        counters = {"video_frames": self.video_frames, "audio_frames": self.audio_frames, "audio_samples": self.audio_samples}
        return {
            "session_id": self.session_id,
            "generation": self.generation,
            "created_at": self.created_at,
            "transport": self.transport,
            "mode": self.mode,
            **counters,
            "counters": counters,
            "last_video_age_ms": self.video_age_ms(),
            "last_audio_age_ms": round((time.monotonic() - self.last_audio_at) * 1000) if self.last_audio_at else None,
            "last_ai_frame_number": self.last_ai_frame_number,
            "last_ai_frame_received_at": self.last_ai_frame_received_at,
            "latest_result": self.latest_result,
            "busy": self.busy,
            "last_error": self.last_error,
            "guide_active": self.guide_active,
            "paused": self.paused,
            "scene_waiting_for_video": self.scene_waiting_for_video,
            "question_recording": self.recording,
            "question_draining": self.draining,
            "current_turn_id": self.current_turn_id,
            "question_audio_seconds": round(self.question_samples / INPUT_RATE, 2),
            "audio_truncated": self.audio_truncated,
            "ai_configured": ai_configured,
            "voice_wake_enabled": self.voice_wake_enabled,
            "voice_wake_phase": self.voice_wake_phase(),
            "frame_policy": "AI 요청 시 안정되고 선명한 최신 영상을 선택합니다. 질문은 영상 선택 뒤 듣기 시작합니다.",
        }


class SessionManager:
    def __init__(self, config: Config, adapter: AIAdapter | None = None):
        self.config = config
        self.adapter = adapter if adapter is not None else create_adapter(config)
        self.ai_configured = adapter is not None or config.ai_configured
        self.sessions: dict[str, Session] = {}
        self.close_transport = None
        self.pairing_code: str | None = None
        self.events = deque(maxlen=400)
        self.event_seq = 0

    @property
    def voice_wake_available(self) -> bool:
        return bool(getattr(self.adapter, "voice_wake_available", False))

    @property
    def voice_audio_provider(self) -> str:
        return getattr(self.adapter, "voice_audio_provider", "windows-tts")

    async def refresh_voice_wake_available(self) -> bool:
        refresh = getattr(self.adapter, "refresh_voice_wake_available", None)
        if refresh is not None:
            return await refresh()
        return self.voice_wake_available

    def event(self, kind: str, session: Session, **fields) -> None:
        # Metadata only. Never accept arbitrary request content or exception text.
        allowed = {k: v for k, v in fields.items() if k in {"action", "turn_id", "result_id", "duration_ms", "source", "status", "mode"}}
        self.event_seq += 1
        self.events.append({"seq": self.event_seq, "at": utc_now(), "event": kind, "session_id": session.session_id, "mode": session.mode, **allowed})


    def create(self, *, answer_audio_http: bool = False) -> Session:
        if len(self.sessions) >= self.config.max_sessions:
            raise SessionError("현재 연결 수가 많습니다. 사용하지 않는 창을 종료해 주세요.", 429)
        item = Session(session_id=uuid.uuid4().hex, token=secrets.token_urlsafe(32), answer_audio_http=answer_audio_http)
        self.sessions[item.session_id] = item
        self.event("session.created", item)
        return item

    def authorize(self, session_id: str, token: str | None) -> Session:
        item = self.sessions.get(session_id)
        if item is None or item.closed:
            raise SessionError("연결 세션이 없거나 만료되었습니다. 다시 시작해 주세요.", 404)
        if not token or not secrets.compare_digest(item.token, token):
            raise SessionError("이 연결을 사용할 권한이 없습니다.", 401)
        if time.monotonic() - item.last_seen > self.config.session_ttl:
            raise SessionError("연결 시간이 만료되었습니다. 다시 시작해 주세요.", 410)
        item.touch()
        return item

    def get_active(self, session_id: str) -> Session | None:
        item = self.sessions.get(session_id)
        return item if item and not item.closed else None

    async def close(self, session_id: str) -> None:
        item = self.sessions.pop(session_id, None)
        if not item:
            return
        self.event("session.closed", item)
        item.closed = True
        item.voice_wake_enabled = False
        item.cancel_pending_capture()
        self._invalidate(item)
        item.transport = "closed"
        item.latest_video = None
        item.question_jpeg = None
        item.question_audio.clear()
        if self.close_transport:
            with contextlib.suppress(Exception):
                await self.close_transport(session_id)

    async def close_all(self) -> None:
        for session_id in list(self.sessions):
            await self.close(session_id)

    async def cleanup(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for sid, session in list(self.sessions.items()):
                if now - session.last_seen > self.config.session_ttl:
                    await self.close(sid)

    def _invalidate(self, session: Session) -> None:
        session.generation += 1
        if session.pending_capture is not None and session.pending_capture.generation is not None:
            session.cancel_pending_capture()
        if session.task and session.task is not asyncio.current_task():
            session.task.cancel()
        session.task = None
        session.busy = False
        session.scene_waiting_for_video = False
        session.resume_scene_after_question = False
        session.recording = False
        session.draining = False
        session.clear_audio_level()
        session.question_audio.clear()
        session.question_jpeg = None
        session.question_frame_number = None
        session.question_frame_at = None
        session.current_turn_id = None
        session.voice_vad.reset()
        for name in ("voice_task", "voice_question_timer"):
            task = getattr(session, name)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                setattr(session, name, None)
        session.voice_waiting_for_question = False
        session.voice_processing = False
        session.voice_playback_id = None
        session.voice_playback_seconds = 0
        session.voice_playback_until = 0
        self._clear_output_audio(session)

    def _clear_output_audio(self, session: Session) -> None:
        session.latest_answer_wav = None
        if session.flush_audio:
            session.flush_audio()
        while not session.output_audio.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                session.output_audio.get_nowait()

    def media_video(self, session: Session, rgb: np.ndarray) -> None:
        if session.closed:
            return
        arr = np.asarray(rgb)
        if arr.ndim != 3 or arr.shape[2] < 3 or arr.shape[0] < 1 or arr.shape[1] < 1:
            return
        if arr.shape[0] * arr.shape[1] > 4096 * 4096:
            session.last_error = "카메라 해상도가 너무 높습니다. 해상도를 낮춰 주세요."
            return
        received_at = time.monotonic()
        arr = arr[:, :, :3].astype(np.uint8, copy=False)
        image = Image.fromarray(arr, mode="RGB")
        session.video_size = image.size
        image.thumbnail((960, 720))
        session.latest_video = np.array(image, dtype=np.uint8, copy=True)
        session.video_frames += 1
        session.count_video_receipt(received_at)
        if session.video_frames == 1:
            self.event("video.first", session)
        session.last_video_at = received_at
        session.last_video_wall = utc_now()
        session.transport = "connected"
        capture = session.pending_capture
        if (capture is not None and not capture.result.done() and received_at <= capture.deadline
                and capture.stability.accepts(image, received_at)):
            capture.result.set_result(StableFrame(
                session.latest_video.copy(), session.video_frames, session.last_video_wall, received_at,
            ))
        # Media does not renew the lease: a lost browser/control connection must
        # expire even if a peer continues sending abandoned media packets.

    async def capture_stable_frame(self, session: Session, *, generation: int | None = None) -> StableFrame:
        if self.get_active(session.session_id) is not session or session.transport != "connected":
            raise SessionError("카메라 연결을 먼저 확인해 주세요.", 409)
        if session.pending_capture is not None:
            raise SessionError("이미 안정된 영상을 기다리고 있습니다.", 409)

        capture = PendingCapture(asyncio.get_running_loop().create_future(), time.monotonic() + CAPTURE_TIMEOUT, generation)
        session.pending_capture = capture
        try:
            frame = await asyncio.wait_for(capture.result, timeout=CAPTURE_TIMEOUT)
            if session.closed or session.transport != "connected":
                raise SessionError("캡처 중 카메라 연결이 종료되었습니다.", 409)
            if generation is not None and generation != session.generation:
                raise SessionError("영상 선택이 취소되었습니다. 다시 시작해 주세요.", 409)
            if frame is None:
                raise SessionError("캡처 중 카메라 연결이 종료되었습니다.", 409)
            if time.monotonic() - frame.received_monotonic > self.config.frame_max_age:
                raise SessionError("캡처한 영상이 오래되었습니다. 다시 시도해 주세요.", 408)
            return frame
        except asyncio.TimeoutError as exc:
            raise SessionError("안정되고 선명한 영상을 받지 못했습니다. 카메라를 잠시 고정한 뒤 다시 시도해 주세요.", 408) from exc
        finally:
            if session.pending_capture is capture:
                session.pending_capture = None

    def media_audio(self, session: Session, sample_rate: int, audio: np.ndarray) -> None:
        if session.closed:
            return
        received_at = time.monotonic()
        data = np.asarray(audio)
        if data.size == 0:
            return
        if data.ndim > 1:
            data = data.mean(axis=0) if data.shape[0] <= 2 else data.reshape(-1)
        data = data.reshape(-1)
        if sample_rate != INPUT_RATE:
            if sample_rate < 8000 or sample_rate > 96000:
                return
            count = max(1, round(data.size * INPUT_RATE / sample_rate))
            data = np.interp(np.arange(count) * sample_rate / INPUT_RATE, np.arange(data.size), data)
        data = np.clip(data, -32768, 32767).astype(np.int16)
        session.audio_samples += int(data.size)
        session.audio_frames += 1
        if session.recording:
            if (received_at - session.last_audio_at) * 1000 >= AUDIO_LEVEL_FRESH_MS:
                session.clear_audio_level()
            session.update_audio_level(data)
        if session.audio_frames == 1:
            self.event("audio.first", session)
        session.last_audio_at = received_at
        session.transport = "connected"
        if session.recording or session.draining:
            remaining = MAX_QUESTION_SECONDS * INPUT_RATE - session.question_samples
            if remaining > 0:
                kept = data[:remaining].copy()
                session.question_audio.append(kept)
                session.question_samples += kept.size
            if data.size > remaining:
                session.audio_truncated = True
                session.last_error = "질문은 20초까지만 전송됩니다. 질문 종료를 눌러 주세요."
        elif (session.voice_wake_phase() in {"waiting", "question"} and session.voice_task is None
              and session.latest_video is not None and received_at - session.last_video_at <= self.config.frame_max_age):
            if (utterance := session.voice_vad.push(data)) is not None:
                session.voice_task = asyncio.create_task(self._handle_voice_utterance(session, session.generation, utterance))

    def _require_recent_video(self, session: Session) -> None:
        if session.latest_video is None:
            raise SessionError("서버에 카메라 영상이 아직 도착하지 않았습니다. 연결을 확인해 주세요.", 409)
        if time.monotonic() - session.last_video_at > self.config.frame_max_age:
            raise SessionError("카메라 영상이 오래되어 분석을 보류했습니다. 화면 연결을 확인해 주세요.", 409)

    def _system_result(self, session: Session, text: str, source: str = "system") -> None:
        session.latest_answer_wav = None
        session.latest_result = {
            "id": uuid.uuid4().hex,
            "turn_id": session.current_turn_id,
            "text": text,
            "kind": "info",
            "direction": "none",
            "created_at": utc_now(),
            "mode": session.mode,
            "source": source,
            "audio_available": source == "transport-test",
            "audio_source": "webrtc" if source == "transport-test" else "none",
        }
        self.event("result.system", session, source=source, result_id=session.latest_result["id"], turn_id=session.current_turn_id)

    def _publish(
        self, session: Session, generation: int, turn_id: str, result: AnalysisResult, *, source: str = "ai",
    ) -> None:
        if session.closed or generation != session.generation:
            return
        session.latest_answer_wav = None
        audio = result.audio
        audio_source = "browser-tts"
        if audio is not None:
            if session.answer_audio_http:
                if 0 < audio.size <= 20 * OUTPUT_RATE:
                    session.latest_answer_wav = wav_bytes(audio, OUTPUT_RATE)
                    audio_source = "http-wav"
                else:
                    audio = None
            else:
                audio_source = "webrtc"
        session.latest_result = {
            "id": uuid.uuid4().hex,
            "turn_id": turn_id,
            "text": result.text,
            "kind": result.kind,
            "direction": result.direction,
            "created_at": utc_now(),
            "mode": session.mode,
            "source": source,
            "scene_observation": source == "ai" and session.mode == "scene" and session.guide_active,
            "audio_available": audio is not None,
            "audio_source": audio_source,
            "audio_provider": "browser-tts" if audio is None else result.audio_provider,
        }
        session.last_error = None
        if session.voice_wake_enabled and audio is not None:
            session.voice_vad.reset()
            session.voice_playback_id = session.latest_result["id"]
            session.voice_playback_seconds = audio.size / OUTPUT_RATE
            session.voice_playback_until = time.monotonic() + session.voice_playback_seconds + VOICE_PLAYBACK_FETCH_MARGIN
        self.event("result.ai" if source == "ai" else "result.system", session, source=source,
                   result_id=session.latest_result["id"], turn_id=turn_id, status=result.kind)
        if audio_source == "webrtc":
            self._queue_audio(session, generation, audio)

    def answer_audio(self, session: Session, result_id: str) -> bytes:
        audio = session.latest_answer_wav
        if audio is None or (session.latest_result or {}).get("id") != result_id:
            raise SessionError("재생할 답변 음성을 찾을 수 없습니다.", 404)
        if session.transport != "connected" or session.paused or session.scene_waiting_for_video or session.last_error:
            session.latest_answer_wav = None
            raise SessionError("현재 답변 음성을 재생할 수 없습니다.", 409)
        try:
            self._require_recent_video(session)
        except SessionError:
            session.latest_answer_wav = None
            raise
        return audio

    def _queue_audio(self, session: Session, generation: int, pcm: np.ndarray) -> None:
        self._clear_output_audio(session)
        session.output_audio.put_nowait((generation, OUTPUT_RATE, pcm))

    def _payload(self, session: Session, turn_id: str, image: str, image_at: str | None, audio: np.ndarray | None = None) -> dict:
        return {
            "session_id": session.session_id,
            "turn_id": turn_id,
            "mode": session.mode,
            "jpeg_base64": image,
            "image_captured_at": image_at,
            "audio_wav_base64": encode_wav(audio, INPUT_RATE) if audio is not None and audio.size else None,
            "sample_rate": INPUT_RATE,
            "audio_truncated": session.audio_truncated if audio is not None else False,
            "requested_at": utc_now(),
        }

    async def _start_voice_guide(self, session: Session, generation: int) -> None:
        session.current_turn_id = uuid.uuid4().hex
        try:
            audio = await asyncio.wait_for(self.adapter.synthesize_voice(VOICE_STARTUP), timeout=min(10, self.config.ai_timeout))
            if session.closed or generation != session.generation:
                return
            self._require_recent_video(session)
            self._publish(session, generation, session.current_turn_id,
                          AnalysisResult(VOICE_STARTUP, "info", "none", audio, self.voice_audio_provider), source="startup")
        except (AdapterError, TimeoutError):
            if not session.closed and generation == session.generation:
                self._system_result(session, VOICE_STARTUP, "startup")
                self.event("voice.audio_unavailable", session, status="startup")
        except SessionError as exc:
            self._pause_voice_guide(session, exc.detail)
            return
        finally:
            if generation == session.generation:
                session.voice_processing = False
        await self._wait_for_voice_playback(session, generation)
        if not session.closed and generation == session.generation and session.guide_active:
            await self._guide_loop(session, generation)

    async def _wait_for_voice_playback(self, session: Session, generation: int) -> None:
        while not session.closed and generation == session.generation:
            remaining = session.voice_playback_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(.1, remaining))

    def _pause_voice_guide(self, session: Session, message: str) -> None:
        session.guide_active = False
        session.voice_wake_enabled = False
        session.paused = True
        session.voice_vad.reset()
        session.last_error = message
        self._system_result(session, message)
        self.event("guide.paused", session, status="video_interrupted")

    def _restore_voice_guide(self, session: Session, generation: int) -> None:
        if session.closed or generation != session.generation or not session.voice_wake_enabled:
            return
        try:
            if session.transport != "connected":
                raise SessionError("카메라 연결이 끊겨 주변 확인을 멈췄습니다.", 409)
            self._require_recent_video(session)
        except SessionError as exc:
            self._pause_voice_guide(session, exc.detail)
            return
        session.guide_active = True
        session.voice_vad.reset()
        self.event("voice.guide_resumed", session)
        session.task = asyncio.create_task(self._guide_loop(session, generation, initial_delay=self.config.guide_interval))

    async def _expire_voice_question(self, session: Session, generation: int) -> None:
        try:
            await asyncio.sleep(VOICE_QUESTION_TIMEOUT)
            if session.closed or generation != session.generation or not session.voice_waiting_for_question:
                return
            self._invalidate(session)
            self._system_result(session, "질문이 들리지 않아 주변 확인을 계속합니다.", "voice-timeout")
            self._restore_voice_guide(session, session.generation)
        finally:
            if session.voice_question_timer is asyncio.current_task():
                session.voice_question_timer = None

    async def _handle_voice_utterance(self, session: Session, generation: int, utterance: np.ndarray) -> None:
        try:
            async with asyncio.timeout(self.config.ai_timeout):
                transcript = await self.adapter.transcribe_voice(utterance)
            del utterance
            if session.closed or generation != session.generation or not session.voice_wake_enabled or session.paused:
                return
            question = split_wake_phrase(transcript)
            if session.voice_waiting_for_question:
                question = transcript.strip() if question is None else question
                if not question:
                    return
            elif question is not None:
                self.event("voice.wake_detected", session)
            else:
                return
            del transcript

            # Cancel in-flight scene analysis and its audio before using a new frame for the spoken request.
            self._invalidate(session)
            generation = session.generation
            session.guide_active = False
            session.latest_result = None
            session.last_error = None
            if not question:
                session.voice_waiting_for_question = True
                session.voice_question_timer = asyncio.create_task(self._expire_voice_question(session, generation))
                return
            await self._answer_voice_question(session, generation, question)
        except (AdapterError, TimeoutError):
            # Unrecognized background conversation and noise are discarded.
            pass
        finally:
            if session.voice_task is asyncio.current_task():
                session.voice_task = None

    async def _answer_voice_question(self, session: Session, generation: int, question: str) -> None:
        turn_id = session.current_turn_id = uuid.uuid4().hex
        session.voice_processing = session.busy = True
        self.event("question.started", session, turn_id=turn_id, source="voice")
        try:
            frame = await self.capture_stable_frame(session, generation=generation)
            payload = self._payload(session, turn_id, encode_jpeg(frame.rgb), frame.received_at)
            payload["question_text"] = question
            await self._analyze(session, generation, turn_id, payload, frame.received_monotonic, frame.frame_number)
        except SessionError as exc:
            if not session.closed and generation == session.generation:
                session.last_error = exc.detail
                self._system_result(session, exc.detail)
        finally:
            if generation == session.generation:
                session.voice_processing = session.busy = False
        await self._wait_for_voice_playback(session, generation)
        self._restore_voice_guide(session, generation)

    def _voice_playback_control(self, session: Session, action: str, result_id: str | None) -> None:
        if not session.voice_wake_enabled or session.paused or not result_id or result_id != (session.latest_result or {}).get("id"):
            return
        if action == "voice.playback.start":
            duration = session.voice_playback_seconds if result_id == session.voice_playback_id else 0
            session.voice_playback_id = result_id
            session.voice_playback_until = time.monotonic() + (duration or 20) + VOICE_PLAYBACK_GRACE
            session.voice_vad.reset()
        elif result_id == session.voice_playback_id:
            session.voice_playback_until = time.monotonic() + VOICE_PLAYBACK_GRACE
            session.voice_vad.reset()

    async def _analyze(
        self, session: Session, generation: int, turn_id: str, payload: dict,
        frame_monotonic: float | None = None, frame_number: int | None = None,
    ) -> float:
        if session.closed or generation != session.generation:
            return 0
        if not self.ai_configured:
            self._system_result(session, AI_UNCONNECTED)
            session.last_error = AI_UNCONNECTED
            return 0
        if frame_monotonic is not None and time.monotonic() - frame_monotonic > self.config.frame_max_age:
            session.last_error = "카메라 영상이 오래되어 분석을 보류했습니다. 다시 시도해 주세요."
            self._system_result(session, session.last_error)
            return 0
        if frame_number is not None:
            session.last_ai_frame_number = frame_number
            session.last_ai_frame_received_at = payload['image_captured_at']
        started = time.monotonic()
        self.event("analysis.start", session, turn_id=turn_id)
        try:
            result = await self.adapter.analyze(payload)
            if session.closed or generation != session.generation:
                return 0
            session.analysis_ms = round((time.monotonic() - started) * 1000)
            self.event("analysis.response", session, turn_id=turn_id, duration_ms=session.analysis_ms)
            if session.mode == "guide" and frame_monotonic is not None and time.monotonic() - frame_monotonic > self.config.frame_max_age:
                session.last_error = "영상 분석 응답이 늦어 이동 안내를 보류했습니다. 최신 화면으로 다시 확인합니다."
                self._system_result(session, session.last_error)
                return 0
            if frame_number is not None:
                self._require_recent_video(session)
            previous = session.latest_result
            if (session.mode == "scene" and session.guide_active and previous
                    and previous.get("source") == "ai" and previous.get("kind") == result.kind
                    and previous.get("direction") == result.direction
                    and previous["text"].rstrip(".!?… ") == result.text.rstrip(".!?… ")):
                return 0
            self._publish(session, generation, turn_id, result)
            if result.audio is not None:
                return result.audio.size / OUTPUT_RATE
            return min(20, len(result.text) / 6)
        except (AdapterError, SessionError) as exc:
            self.event("analysis.error", session, turn_id=turn_id, status="adapter_error")
            if generation == session.generation and not session.closed:
                session.last_error = exc.detail if isinstance(exc, SessionError) else str(exc)
                self._system_result(session, session.last_error)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.event("analysis.error", session, turn_id=turn_id, status="internal_error")
            # Never serialize an exception that may contain a URL/API token.
            if generation == session.generation and not session.closed:
                session.last_error = "AI 처리 중 오류가 발생했습니다. 서버 연결을 확인해 주세요."
                self._system_result(session, session.last_error)
        return 0

    async def _finish_question(self, session: Session, generation: int, turn_id: str) -> None:
        speech_seconds = 0.0
        try:
            await asyncio.sleep(self.config.audio_tail_ms / 1000)
            if session.closed or generation != session.generation:
                return
            session.draining = False
            samples = np.concatenate(session.question_audio) if session.question_audio else np.empty(0, dtype=np.int16)
            session.question_audio.clear()
            if samples.size < INPUT_RATE // 10:
                raise SessionError("서버에 질문 음성이 도착하지 않았습니다. 마이크 연결을 확인해 주세요.", 409)
            if self.ai_configured:
                self._require_recent_video(session)
                if session.question_jpeg is None:
                    raise SessionError("질문에 사용할 영상이 없습니다. 다시 시도해 주세요.", 409)
            payload = self._payload(session, turn_id, session.question_jpeg or "", session.question_frame_at, samples)
            speech_seconds = await self._analyze(session, generation, turn_id, payload, frame_number=session.question_frame_number)
        except SessionError as exc:
            if generation == session.generation:
                session.last_error = exc.detail
                self._system_result(session, exc.detail)
        except asyncio.CancelledError:
            raise
        finally:
            if generation == session.generation:
                session.busy = False
                session.draining = False
                session.question_jpeg = None
                session.question_frame_number = None
                session.question_frame_at = None
                session.question_audio.clear()
                if session.resume_scene_after_question and session.transport == "connected":
                    session.resume_scene_after_question = False
                    if time.monotonic() - session.last_video_at <= self.config.frame_max_age:
                        session.guide_active = True
                        session.task = asyncio.create_task(self._guide_loop(
                            session, generation, initial_delay=max(self.config.guide_interval, speech_seconds + 1),
                        ))

    async def _wait_for_next_scene(self, session: Session, delay: float) -> None:
        deadline = time.monotonic() + delay
        while (remaining := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(remaining, 0.5))
            if session.transport != "connected":
                raise SessionError("카메라 연결이 끊겨 주변 확인을 멈췄습니다.", 409)
            self._require_recent_video(session)

    async def _guide_loop(self, session: Session, generation: int, *, initial_delay: float = 0) -> None:
        scene = session.mode == "scene"
        once = session.mode in {"find", "read"}
        previous_frame = None
        try:
            if initial_delay:
                await self._wait_for_next_scene(session, initial_delay)
            while session.guide_active and not session.closed and generation == session.generation:
                if session.voice_wake_enabled:
                    await self._wait_for_voice_playback(session, generation)
                    if session.closed or generation != session.generation:
                        return
                turn_id = uuid.uuid4().hex
                session.busy = True
                try:
                    capture_start = time.perf_counter()

                    frame = await self.capture_stable_frame(session, generation=generation)

                    print(
                        f"[Capture] 안정 프레임 확보 시간: "
                        f"{time.perf_counter() - capture_start:.3f}초"
                    )
                    print(
                        f"[Capture] 프레임 경과 시간: "
                        f"{time.monotonic() - frame.received_monotonic:.3f}초"
                    )
                    session.scene_waiting_for_video = False
                    session.last_error = None
                    thumbnail = scene_thumbnail(frame.rgb) if scene else None
                    if thumbnail is None or previous_frame is None or scene_changed(previous_frame, thumbnail):
                        previous_frame = thumbnail
                        session.current_turn_id = turn_id
                        payload = self._payload(session, turn_id, encode_jpeg(frame.rgb), frame.received_at)
                        speech_seconds = await self._analyze(
                            session, generation, turn_id, payload, frame.received_monotonic, frame.frame_number,
                        )
                        del payload
                    else:
                        speech_seconds = 0
                    del frame
                except SessionError as exc:
                    if session.closed or generation != session.generation:
                        return
                    speech_seconds = 0
                    if (scene and exc.status_code == 408 and session.transport == "connected"
                            and time.monotonic() - session.last_video_at <= self.config.frame_max_age):
                        if not session.scene_waiting_for_video:
                            session.scene_waiting_for_video = True
                            session.last_error = None
                            session.latest_result = None
                            self._clear_output_audio(session)
                            self.event("guide.waiting", session, status="video_quality")
                        previous_frame = None
                    else:
                        session.last_error = exc.detail
                        self._system_result(session, exc.detail)
                finally:
                    if generation == session.generation:
                        session.busy = False
                if not once and (session.last_error or (not scene and (session.latest_result or {}).get("kind") in {"obstacle", "uncertain"})):
                    session.guide_active = False
                    session.paused = True
                    session.voice_wake_enabled = False
                    session.voice_vad.reset()
                    self.event("guide.paused", session, status="attention_required")
                    return
                if once or not self.ai_configured:
                    return
                if scene:
                    await self._wait_for_next_scene(session, max(self.config.guide_interval, speech_seconds + 1))
                else:
                    await asyncio.sleep(self.config.guide_interval)
        except SessionError as exc:
            if not session.closed and generation == session.generation:
                self._clear_output_audio(session)
                session.last_error = exc.detail
                self._system_result(session, exc.detail)
                session.guide_active = False
                session.paused = True
                session.voice_wake_enabled = False
                session.voice_vad.reset()
                self.event("guide.paused", session, status="video_interrupted")
        except asyncio.CancelledError:
            raise
        finally:
            if generation == session.generation:
                session.busy = False
                session.scene_waiting_for_video = False
                if once:
                    session.guide_active = False

    async def control(self, session: Session, action: str, mode: str = "guide", turn_id: str | None = None) -> dict:
        session.touch()
        if action in {"voice.playback.start", "voice.playback.end"}:
            self._voice_playback_control(session, action, turn_id)
            return session.public_state(self.ai_configured)
        self.event("control.request", session, action=action, mode=mode, turn_id=turn_id)
        if action == "voice.enable":
            generation = session.generation
            if not await self.refresh_voice_wake_available():
                raise SessionError("음성 인식과 음성 합성 서버를 먼저 연결해 주세요.", 409)
            if session.closed or generation != session.generation:
                raise SessionError("연결 상태가 바뀌었어요. 다시 시작해 주세요.", 409)
            self._require_recent_video(session)
            if not session.voice_wake_enabled or session.paused:
                self._invalidate(session)
                session.voice_wake_enabled = True
                session.mode = "scene"
                session.guide_active = True
                session.paused = False
                session.last_error = None
                session.latest_result = None
                session.voice_processing = True
                session.task = asyncio.create_task(self._start_voice_guide(session, session.generation))
        elif action == "voice.disable":
            self._invalidate(session)
            session.voice_wake_enabled = False
            session.guide_active = False
            session.paused = True
            session.latest_result = None
        elif action in {"guide.start", "guide.resume"}:
            self._invalidate(session)
            session.mode = mode
            session.guide_active = True
            session.paused = False
            session.last_error = None
            session.latest_result = None
            if not self.ai_configured:
                self._system_result(session, AI_UNCONNECTED)
                session.last_error = AI_UNCONNECTED
            else:
                session.task = asyncio.create_task(self._guide_loop(session, session.generation))
        elif action in {"guide.pause", "guide.stop"}:
            self._invalidate(session)
            session.voice_wake_enabled = False
            session.guide_active = False
            session.paused = action == "guide.pause"
        elif action == "question.start":
            self._require_recent_video(session)
            resume_scene = self.ai_configured and session.mode == mode == "scene" and session.guide_active
            self._invalidate(session)
            session.mode = mode
            session.guide_active = False
            session.paused = False
            session.last_error = None
            session.latest_result = None
            session.question_samples = 0
            session.audio_truncated = False
            if self.ai_configured:
                generation = session.generation
                session.busy = True
                try:
                    frame = await self.capture_stable_frame(session, generation=generation)
                    session.question_jpeg = encode_jpeg(frame.rgb)
                    session.question_frame_number = frame.frame_number
                    session.question_frame_at = frame.received_at
                except SessionError as exc:
                    if not session.closed and generation == session.generation:
                        session.last_error = exc.detail
                        self._system_result(session, exc.detail)
                        if (resume_scene and session.transport == "connected"
                                and time.monotonic() - session.last_video_at <= self.config.frame_max_age):
                            session.guide_active = True
                            session.task = asyncio.create_task(self._guide_loop(
                                session, generation, initial_delay=self.config.guide_interval,
                            ))
                    raise
                finally:
                    if generation == session.generation:
                        session.busy = False
            session.recording = True
            session.resume_scene_after_question = resume_scene
            session.current_turn_id = turn_id or uuid.uuid4().hex
            session.question_started_at = time.monotonic()
            self.event("question.started", session, turn_id=session.current_turn_id)
        elif action == "question.end":
            if not session.recording or not session.current_turn_id:
                raise SessionError("진행 중인 질문이 없습니다.", 409)
            if turn_id is not None and turn_id != session.current_turn_id:
                raise SessionError("이미 끝난 질문입니다. 현재 질문 상태를 확인해 주세요.", 409)
            session.recording = False
            session.draining = True
            session.clear_audio_level()
            session.busy = True
            session.task = asyncio.create_task(self._finish_question(session, session.generation, session.current_turn_id))
        elif action == "audio.test":
            if session.transport != "connected":
                raise SessionError("먼저 카메라·마이크의 실시간 연결을 시작해 주세요.", 409)
            # Explicit opt-in only. This is a generated transport tone, not AI.
            t = np.arange(int(OUTPUT_RATE * .35)) / OUTPUT_RATE
            envelope = np.minimum(1, t / .02) * np.minimum(1, (.35 - t) / .04)
            pcm = (np.sin(2 * np.pi * 660 * t) * envelope * 4500).astype(np.int16).reshape(1, -1)
            self._queue_audio(session, session.generation, pcm)
            self._system_result(session, "서버 확인음을 보냈습니다. 짧은 소리가 들리면 음성 수신 연결이 작동합니다.", "transport-test")
        else:
            raise SessionError("지원하지 않는 조작입니다.")
        self.event("control.accepted", session, action=action, turn_id=session.current_turn_id)
        return session.public_state(self.ai_configured)
