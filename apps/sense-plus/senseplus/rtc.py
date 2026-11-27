"""FastRTC 0.0.34 integration, verified against its installed source.

FastRTC supplies BGR video arrays; internal application images are RGB. Audio is
16kHz mono input / 24kHz mono output. The text data channel must be negotiated by
the browser because FastRTC waits for it before reading the media tracks.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import numpy as np

# The transport does not need Gradio usage analytics or update checks.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from fastrtc import AsyncAudioVideoStreamHandler, Stream, get_current_context
from fastrtc.stream import Body

from .core import INPUT_RATE, OUTPUT_RATE, Session, SessionManager


class SenseHandler(AsyncAudioVideoStreamHandler):
    def __init__(self, manager: SessionManager):
        # fps paces video returned to legacy peers. Incoming video has its own
        # receive task and is processed at the browser's negotiated rate.
        super().__init__(expected_layout="mono", input_sample_rate=INPUT_RATE, output_sample_rate=OUTPUT_RATE, fps=10)
        self.manager = manager
        self.session_id: str | None = None
        self._closed = False
        self._video_ready = asyncio.Event()
        self._pcm: np.ndarray | None = None
        self._pcm_generation = -1
        self._pcm_offset = 0
        self._output_epoch = 0

    def bind(self, session_id: str) -> None:
        if self.session_id and self.session_id != session_id:
            raise RuntimeError("미디어 세션이 일치하지 않습니다.")
        self.session_id = session_id
        session = self.manager.get_active(session_id)
        if session:
            session.flush_audio = self.flush_output

    def _session(self) -> Session | None:
        if self._closed:
            return None
        if not self.session_id:
            # Audio start_up receives the verified FastRTC Context. The offer
            # wrapper also binds explicitly, so video cannot attach by guessing.
            with contextlib.suppress(RuntimeError):
                self.bind(get_current_context().webrtc_id)
        return self.manager.get_active(self.session_id) if self.session_id else None

    def copy(self) -> "SenseHandler":
        return SenseHandler(self.manager)

    async def start_up(self) -> None:
        self.bind(get_current_context().webrtc_id)

    async def receive(self, frame: tuple[int, np.ndarray]) -> None:
        if session := self._session():
            self.manager.media_audio(session, frame[0], frame[1])

    async def video_receive(self, frame: np.ndarray) -> None:
        if session := self._session():
            self.manager.media_video(session, np.asarray(frame)[:, :, ::-1])
            self._video_ready.set()

    async def video_emit(self) -> np.ndarray:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._video_ready.wait(), timeout=.5)
        self._video_ready.clear()
        session = self._session()
        if session and session.latest_video is not None:
            return session.latest_video[:, :, ::-1].copy()
        return np.zeros((240, 320, 3), dtype=np.uint8)

    def flush_output(self) -> None:
        self._output_epoch += 1
        self._pcm = None
        self._pcm_offset = 0
        clear = self.clear_queue
        if callable(clear):
            with contextlib.suppress(Exception):
                clear()

    async def emit(self):
        session = self._session()
        if session is None:
            await asyncio.sleep(.05)
            return None
        if self._pcm is not None and self._pcm_generation != session.generation:
            self.flush_output()
        if self._pcm is None:
            try:
                generation, _, pcm = await asyncio.wait_for(session.output_audio.get(), timeout=.1)
            except asyncio.TimeoutError:
                return None
            if generation != session.generation:
                return None
            self._pcm, self._pcm_generation, self._pcm_offset = pcm, generation, 0
        # Yield only 100ms at a time, so stop/pause cannot leave a 20s response
        # queued inside FastRTC. This complements its supported clear_queue hook.
        chunk = self._pcm[:, self._pcm_offset:self._pcm_offset + 2400].copy()
        epoch = self._output_epoch
        self._pcm_offset += chunk.shape[1]
        if self._pcm_offset >= self._pcm.shape[1]:
            self._pcm = None
        await asyncio.sleep(max(.005, chunk.shape[1] / OUTPUT_RATE * .85))
        if self._closed or self._pcm_generation != session.generation or epoch != self._output_epoch:
            return None
        return OUTPUT_RATE, chunk

    async def shutdown(self) -> None:
        if self._closed:
            return
        session = self._session()
        self._closed = True
        self.flush_output()
        self._video_ready.set()
        if session:
            session.transport = "disconnected"
            session.cancel_pending_capture()
            self.manager.event("rtc.disconnected", session)
            session.guide_active = False
            self.manager._invalidate(session)
            session.flush_audio = None
            session.latest_video = None
            session.last_error = "실시간 연결이 종료되었습니다. 다시 시작해 주세요."


class RTCService:
    def __init__(self, manager: SessionManager):
        self.manager = manager
        ice = {"iceServers": manager.config.ice_servers}
        self.stream = Stream(
            handler=SenseHandler(manager),
            modality="audio-video",
            mode="send-receive",
            rtc_configuration=ice,
            server_rtc_configuration=ice,
            concurrency_limit=manager.config.max_sessions,
            verbose=False,
        )
        manager.close_transport = self.close

    async def inbound_video_stats(self, session_id: str) -> tuple[int | None, int | None, str | None]:
        pc = self.stream.pcs.get(session_id)
        if pc is None or pc.connectionState != "connected":
            return None, None, None

        report = await pc.getStats()
        video_streams = [stat for stat in report.values() if stat.type == "inbound-rtp" and stat.kind == "video"]
        if not video_streams:
            return None, None, None

        packets = sum(stat.packetsReceived for stat in video_streams)
        payload_counts = [stat.bytesReceived for stat in video_streams if hasattr(stat, "bytesReceived")]
        payload_bytes = sum(payload_counts) if len(payload_counts) == len(video_streams) else None
        codecs = {
            mime_type for stat in video_streams
            if (mime_type := getattr(report.get(getattr(stat, "codecId", None)), "mimeType", None))
        }
        return packets, payload_bytes, next(iter(codecs)) if len(codecs) == 1 else None

    async def offer(self, payload: dict):
        session_id = payload["webrtc_id"]
        try:
            response = await self.stream.offer(Body.model_validate(payload))
            if handler := self.stream.handlers.get(session_id):
                handler.bind(session_id)
            if payload["type"] == "offer":
                # FastRTC normally starts reading video on the first outgoing
                # video frame. A sendonly browser never pulls that frame.
                for callback in self.stream.connections.get(session_id, ()):
                    if callback.kind == "video":
                        await callback.start()
            return response
        except Exception:
            await self.close(session_id)
            raise

    async def close(self, session_id: str) -> None:
        pc = self.stream.pcs.get(session_id)
        if pc:
            await pc.close()
        # Idempotent cleanup also covers a failed SDP offer before media arrives.
        self.stream.clean_up(session_id)
