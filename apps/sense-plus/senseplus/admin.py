"""Read-only local operator dashboard. No content, tokens or media on disk."""
from collections.abc import Awaitable, Callable
from pathlib import Path
import secrets
import time
from urllib.parse import quote, urlsplit
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, FileResponse
from .adapter import jpeg_bytes
from .core import AUDIO_LEVEL_FRESH_MS, AUDIO_WAVEFORM_BUCKETS, INPUT_RATE
from .qr import render_qr_png


VIDEO_PREVIEW_FRESH_MS = 2500


def create_admin(
    manager, *, user_url: str, port: int, rtc_available: bool,
    inbound_video_stats: Callable[[str], Awaitable[tuple[int | None, int | None, str | None]]] | None = None,
):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    assets = Path(__file__).resolve().parent.parent / 'admin'
    admin_host = f'127.0.0.1:{port}'
    pairing_qr_version = secrets.token_urlsafe(8)
    user_origin = urlsplit(user_url)
    supports_phone_pairing = user_origin.scheme == 'https' and user_origin.hostname not in {None, '127.0.0.1', 'localhost', '::1'}

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        # Separate loopback listener and exact Host check (including DNS rebinding).
        # No forwarded addresses are trusted.
        if (not request.client or request.client.host not in {'127.0.0.1', '::1'}
            or request.headers.get('host') != admin_host
            or request.headers.get('sec-fetch-site') == 'cross-site'):
            return JSONResponse({'detail': 'Local operator only'}, status_code=403)
        response = await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.get('/')
    async def home(): return FileResponse(assets / 'index.html')
    @app.get('/admin.css')
    async def css(): return FileResponse(assets / 'admin.css', media_type='text/css')
    @app.get('/admin.js')
    async def js(): return FileResponse(assets / 'admin.js', media_type='application/javascript')
    @app.get('/api/health')
    async def health(): return {'ok':True, 'rtc_available':rtc_available}

    @app.get('/api/pairing-qr.png')
    async def pairing_qr():
        pairing_code = manager.pairing_code
        if not pairing_code or not supports_phone_pairing:
            return Response(status_code=404)
        # The phone consumes the code from the fragment. Browsers omit it from HTTP URLs.
        pairing_url = f"{user_url.rstrip('/')}/#pair={quote(pairing_code, safe='')}"
        return Response(render_qr_png(pairing_url), media_type='image/png')

    @app.get('/api/monitor')
    async def monitor():
        sessions = []
        for s in list(manager.sessions.values()):
            if s.closed: continue
            video_packets = video_bytes = video_codec = None
            if inbound_video_stats is not None:
                video_packets, video_bytes, video_codec = await inbound_video_stats(s.session_id)
            if s.closed: continue
            state = s.public_state(manager.ai_configured)
            result = s.latest_result or {}
            video_age = state['last_video_age_ms']
            audio_age = state['last_audio_age_ms']
            has_question_audio = bool(s.audio_peaks or s.audio_peak_samples)
            audio_level_active = (s.recording and s.transport == 'connected' and has_question_audio
                                  and audio_age is not None and audio_age < AUDIO_LEVEL_FRESH_MS)
            waveform = list(s.audio_peaks) if audio_level_active else []
            if audio_level_active and s.audio_peak_samples:
                waveform.append(s.audio_peak_in_progress)

            arrival_gap = None
            if s.transport in {'disconnected', 'failed'} or (video_age is not None and video_age > VIDEO_PREVIEW_FRESH_MS):
                arrival_state = 'stale'
            elif video_age is None:
                arrival_state = 'waiting'
            elif not s.recording:
                arrival_state = 'audio_muted'
            elif audio_level_active:
                arrival_state = 'observed'
                # Only callback arrival times share this clock. Neither camera
                # capture time nor microphone capture time is measured here.
                arrival_gap = round(abs(s.last_audio_at - s.last_video_at) * 1000)
            elif has_question_audio or (s.question_started_at and (time.monotonic() - s.question_started_at) * 1000 >= AUDIO_LEVEL_FRESH_MS):
                arrival_state = 'stale'
            else:
                arrival_state = 'waiting'

            sessions.append({
                'id': s.session_id, 'created_at':s.created_at, 'transport':s.transport,
                'mode':s.mode, 'video_frames':s.video_frames, 'video_fps':s.video_fps(), 'audio_frames':s.audio_frames,
                'video_packets_received': video_packets, 'video_bytes_received': video_bytes, 'video_codec': video_codec,
                'audio_samples':s.audio_samples, 'audio_seconds':round(s.audio_samples / INPUT_RATE, 2),
                'video_age_ms':video_age, 'audio_age_ms':audio_age,
                'video_size':s.video_size if s.latest_video is not None else None,
                'preview_size':s.latest_video.shape[:2][::-1] if s.latest_video is not None else None,
                'audio_level_active':audio_level_active,
                'audio_rms':round(s.audio_rms, 5) if audio_level_active else 0,
                'audio_waveform':[round(peak, 5) for peak in waveform[-AUDIO_WAVEFORM_BUCKETS:]],
                'av_arrival_state':arrival_state, 'av_arrival_gap_ms':arrival_gap,
                'recording':s.recording, 'draining':s.draining,'busy':s.busy, 'guide_active':s.guide_active,
                'paused':s.paused, 'error':bool(s.last_error), 'turn_id':s.current_turn_id,
                'result_id':result.get('id'), 'result_source':result.get('source'),
                'result_kind':result.get('kind'), 'analysis_ms':s.analysis_ms,
                'audio_provider':result.get('audio_provider'),
                'last_ai_frame_number': state.get('last_ai_frame_number'),
                'last_ai_frame_received_at': state.get('last_ai_frame_received_at'),
            })
        pairing_required = supports_phone_pairing and bool(manager.pairing_code)
        config = manager.config
        local_models = { 'vision': config.ollama_model or None,
                         'stt': 'faster-whisper' if config.ollama_model and config.stt_model_path else None,
                         'tts': 'windows-tts' if config.ollama_model and config.tts_voice else None }
        return {'user_url':user_url, 'pairing_required':pairing_required, 'local_models':local_models,
                'pairing_qr_version':pairing_qr_version if pairing_required else None, 'rtc_available':rtc_available,
                'ai_configured':manager.ai_configured,'sessions':sessions,'events':list(manager.events)}
    @app.get('/api/frame/{sid}')
    async def frame(sid: str):
        s = manager.get_active(sid)
        if not s or s.latest_video is None or (s.video_age_ms() or 0) > VIDEO_PREVIEW_FRESH_MS:
            return Response(status_code=404)
        return Response(jpeg_bytes(s.latest_video), media_type='image/jpeg')

    return app
