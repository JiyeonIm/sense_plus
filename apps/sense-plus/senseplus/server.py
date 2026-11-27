"""Authenticated session API and the real FastRTC offer endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .config import Config
from .core import SessionError, SessionManager

logger = logging.getLogger(__name__)


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "guide.start", "guide.pause", "guide.resume", "guide.stop",
        "question.start", "question.end", "audio.test",
        "voice.enable", "voice.disable", "voice.playback.start", "voice.playback.end",
    ]
    mode: Literal["guide", "scene", "find", "read"] = "guide"
    turn_id: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sdp: str | None = Field(default=None, max_length=110_000)
    candidate: dict | None = None
    type: Literal["offer", "ice-candidate"]
    webrtc_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")


def bearer(request: Request) -> str | None:
    value = request.headers.get("Authorization", "")
    return value[7:] if value.startswith("Bearer ") else None


async def read_json(request: Request, maximum: int = 131072) -> dict:
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > maximum:
            raise SessionError("요청 크기가 너무 큽니다.", 413)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionError("올바른 JSON 요청이 필요합니다.") from exc
    if not isinstance(result, dict):
        raise SessionError("JSON 객체가 필요합니다.")
    return result


def create_app(config: Config | None = None, manager: SessionManager | None = None, enable_rtc: bool = True) -> FastAPI:
    config = config or (manager.config if manager is not None else Config.from_env())
    manager = manager or SessionManager(config)
    rtc = None
    rtc_error = "FastRTC가 꺼져 있습니다. 미리보기에서는 실시간 통신을 사용할 수 없습니다."
    if enable_rtc:
        try:
            from .rtc import RTCService
            rtc = RTCService(manager)
            rtc_error = ""
        except (ImportError, RuntimeError, ValueError) as exc:
            logger.warning("FastRTC initialization failed (%s); /api/health reports unavailable", type(exc).__name__)
            rtc_error = "FastRTC를 시작하지 못했습니다. 설치 상태와 서버 실행 로그를 확인해 주세요."

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup_task = asyncio.create_task(manager.cleanup())
        yield
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        await manager.close_all()

    app = FastAPI(title="SENSE+", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.manager, app.state.rtc, app.state.config = manager, rtc, config

    @app.exception_handler(SessionError)
    async def session_error(request: Request, exc: SessionError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(self), geolocation=()"
        return response

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "version": __version__,
            "rtc_available": rtc is not None,
            "video_uplink_only": rtc is not None,
            "answer_audio_http": True,
            "voice_wake_available": rtc is not None and manager.voice_wake_available,
            "ai_configured": manager.ai_configured,
            "answer_wait_ms": max(30_000, round(config.ai_timeout * 1000) + config.audio_tail_ms + 5_000),
            "mode": "live",
            "pairing_required": manager.pairing_code is not None,
            "detail": rtc_error or ("실시간 통신 준비 완료. AI 분석이 설정되었습니다." if manager.ai_configured else "실시간 통신 준비 완료. AI 분석은 연결되지 않았습니다."),
            "ice_servers": config.ice_servers,
            "rtc_configuration": {"iceServers": config.ice_servers},
        }

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request):
        pairing = request.headers.get("X-Sense-Pairing", "")
        if manager.pairing_code and not secrets.compare_digest(pairing.encode(), manager.pairing_code.encode()):
            raise SessionError("PC 관제 화면에서 새 접속 QR을 스캔해 주세요.", 403)
        session = manager.create(answer_audio_http=request.headers.get("X-Sense-Answer-Audio") == "http")
        return {"session_id": session.session_id, "token": session.token}

    @app.get("/api/sessions/{session_id}/state")
    async def session_state(session_id: str, request: Request):
        session = manager.authorize(session_id, bearer(request))
        return session.public_state(manager.ai_configured)

    @app.get("/api/sessions/{session_id}/audio/{result_id}.wav")
    async def answer_audio(session_id: str, result_id: str, request: Request):
        session = manager.authorize(session_id, bearer(request))
        return Response(manager.answer_audio(session, result_id), media_type="audio/wav")

    @app.post("/api/sessions/{session_id}/control")
    async def control(session_id: str, request: Request):
        session = manager.authorize(session_id, bearer(request))
        try:
            command = Control.model_validate(await read_json(request, 4096))
        except ValidationError as exc:
            raise SessionError("지원하지 않는 조작 또는 모드입니다.", 422) from exc
        return await manager.control(session, command.action, command.mode, command.turn_id)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        manager.authorize(session_id, bearer(request))
        await manager.close(session_id)
        return {"ok": True}

    @app.post("/webrtc/offer")
    async def webrtc_offer(request: Request):
        try:
            offer = Offer.model_validate(await read_json(request))
        except ValidationError as exc:
            raise SessionError("실시간 연결 요청 형식이 맞지 않습니다.", 422) from exc
        session = manager.authorize(offer.webrtc_id, bearer(request))
        if rtc is None:
            raise SessionError(rtc_error, 503)
        if offer.type == "offer" and not offer.sdp:
            raise SessionError("SDP 연결 정보가 없습니다.", 422)
        if offer.type == "ice-candidate" and not offer.candidate:
            raise SessionError("ICE 연결 정보가 없습니다.", 422)
        if offer.type == "offer":
            if offer.webrtc_id in rtc.stream.pcs:
                raise SessionError("이미 연결된 세션입니다. 새로 시작해 주세요.", 409)
            session.transport = "connecting"
            manager.event("rtc.offer", session)
        try:
            return await rtc.offer(offer.model_dump())
        except Exception as exc:
            session.transport = "failed"
            manager.event("rtc.error", session, status="negotiation_failed")
            session.last_error = "실시간 연결을 시작하지 못했습니다. 네트워크 및 ICE 설정을 확인해 주세요."
            logger.warning("WebRTC negotiation failed (%s)", type(exc).__name__)
            raise SessionError(session.last_error, 502) from exc

    # Explicit API routes above take priority. No project root, .env, logs,
    # provider credentials, or media buffers are exposed through StaticFiles.
    if config.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(config.frontend_dir), html=True), name="frontend")
    return app


app = create_app()
