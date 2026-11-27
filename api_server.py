import base64
import binascii
import os
import threading
import time
import cv2
import numpy as np

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from models.vlm import analyze_scene, client as openai_client
from models.tts import generate_speech
from models.stt import transcribe_audio
from models.depth import DepthEstimator
from core.depth_guide import (
    summarize_depth,
    get_center_depth
)

from concurrent.futures import ThreadPoolExecutor


# 서버 TTS 사용 여부
# 1(기본): OpenAI TTS로 음성을 만들어 WAV로 돌려줌
# 0      : 텍스트만 돌려주고 휴대폰 브라우저 TTS가 바로 읽음 → TTS 대기 시간 제거
USE_SERVER_TTS = os.getenv("SENSE_SERVER_TTS", "1") != "0"


def _warm_up():
    # 첫 요청에서 TLS 연결을 맺느라 늦어지지 않도록 미리 연결해 둔다.
    try:
        openai_client.models.list()
        print("[API] OpenAI 연결 준비 완료")
    except Exception as exc:
        print(f"[API] OpenAI 사전 연결 실패(무시): {type(exc).__name__}")


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_warm_up, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)

depth_estimator = DepthEstimator(depth_scale=1.0)

depth_executor = ThreadPoolExecutor(max_workers=1)


@app.get("/health")
def health():
    return {"status": "ok", "voice_wake_available": True}


@app.post("/stt")
def stt(payload: dict):

    audio_base64 = payload.get("audio_wav_base64")

    if not audio_base64:
        raise HTTPException(
            status_code=400,
            detail="음성 데이터가 없습니다."
        )

    text = transcribe_audio(audio_base64)

    return {"text": text}


@app.post("/tts")
def tts(payload: dict):

    text = payload.get("text", "").strip()

    if not text:
        raise HTTPException(
            status_code=400,
            detail="텍스트가 없습니다."
        )

    audio_base64 = generate_speech(text)

    return {"audio_wav_base64": audio_base64}

def analyze_depth(frame):
    start = time.perf_counter()

    depth_map = depth_estimator.predict(frame)

    summary = summarize_depth(depth_map)
    center_depth = get_center_depth(depth_map)

    print(f"[Depth] Center ROI: {center_depth} m")
    
    elapsed = time.perf_counter() - start

    print(f"[Depth] 추론 시간: {elapsed:.3f}초")
    print(f"[Depth] Summary: {summary}")

    return summary, round(elapsed * 1000)


@app.post("/analyze")
def analyze(payload: dict):

    t0 = time.perf_counter()
    timings = {}

    # 1. 입력 데이터 확인
    mode = payload.get("mode", "scene")
    image_base64 = payload.get("jpeg_base64")
    input_audio_base64 = payload.get("audio_wav_base64")
    question_text = (payload.get("question_text") or "").strip()

    if not image_base64:
        raise HTTPException(
            status_code=400,
            detail="이미지가 없습니다."
        )

    # 2. JPEG 데이터 확인
    try:
        jpeg = base64.b64decode(
            image_base64,
            validate=True
        )
    except binascii.Error:
        raise HTTPException(
            status_code=400,
            detail="이미지 디코딩 실패"
        )

    if not jpeg.startswith(b"\xff\xd8"):
        raise HTTPException(
            status_code=400,
            detail="JPEG 형식이 아닙니다."
        )

    # Depth Anything용 이미지 디코딩
    frame = cv2.imdecode(
        np.frombuffer(jpeg, dtype=np.uint8),
        cv2.IMREAD_COLOR
    )

    if frame is None:
        raise HTTPException(
            status_code=400,
            detail="이미지 디코딩 실패"
        )

    # Depth 비동기 추론 시작
    depth_future = depth_executor.submit(
        analyze_depth,
        frame
    )

    print(
        f"[API] Mode: {mode} / "
        f"JPEG {len(jpeg) // 1024}KB"
    )

    # 3. 사용자 질문 처리
    if question_text:

        question = f"사용자 음성 질문: {question_text}"

    elif input_audio_base64:

        t = time.perf_counter()

        question = transcribe_audio(input_audio_base64)

        timings["stt_ms"] = round(
            (time.perf_counter() - t) * 1000
        )

        if question:
            question = f"사용자 음성 질문: {question}"

    else:

        if mode == "read":
            question = "이미지에서 확인할 수 있는 글자를 읽어 줘."

        elif mode == "find":
            question = "주요 물체와 위치를 알려줘."

        else:
            question = "자동 주변 안내를 수행해 줘."


    # 4. Depth 결과 수신
    try:
        t = time.perf_counter()

        depth_summary, depth_ms = depth_future.result()

        timings["depth_ms"] = depth_ms

        timings["depth_wait_ms"] = round(
            (time.perf_counter() - t) * 1000
        )

    except Exception as exc:
        print(f"[Depth] Error: {exc}")
        depth_summary = None

    # 5. VLM 호출 (Depth 정보 포함)
    t = time.perf_counter()

    if question:

        detail = "high" if mode == "read" else None

        result = analyze_scene(
            image_base64,
            question,
            detail=detail,
            depth_summary=depth_summary
        )

    else:

        result = {
            "hazard": "unknown",
            "speak": "질문을 알아듣지 못했습니다."
        }

    timings["vlm_ms"] = round(
        (time.perf_counter() - t) * 1000
    )

    answer = result["speak"]
    hazard = result["hazard"]

    kind = (
        "obstacle"
        if hazard in ("observed", "possible")
        else "info"
    )

    print(f"[VLM] 응답 유형: {kind}")

    # 6. TTS 호출
    output_audio_base64 = None

    if USE_SERVER_TTS:

        t = time.perf_counter()

        output_audio_base64 = generate_speech(answer)

        timings["tts_ms"] = round(
            (time.perf_counter() - t) * 1000
        )

    timings["total_ms"] = round(
        (time.perf_counter() - t0) * 1000
    )

    print(f"[API] 단계별 시간(ms): {timings}")

    # 7. 응답 반환
    return {
        "text": answer,
        "kind": kind,
        "direction": "none",
        "audio_wav_base64": output_audio_base64,
        "hazard": hazard,
        "timings_ms": timings,
        "depth": depth_summary
    }
