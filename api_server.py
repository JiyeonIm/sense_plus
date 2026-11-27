import base64
import binascii
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from models.vlm import analyze_scene, client as openai_client
from models.tts import generate_speech
from models.stt import transcribe_audio


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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
def analyze(payload: dict):

    t0 = time.perf_counter()
    timings = {}

    # 1. 입력 데이터 확인
    mode = payload.get("mode", "scene")
    image_base64 = payload.get("jpeg_base64")
    input_audio_base64 = payload.get("audio_wav_base64")

    if not image_base64:
        raise HTTPException(
            status_code=400,
            detail="이미지가 없습니다."
        )

    # 2. 이미지 확인
    # 앱이 이미 640x480 JPEG로 보내므로 디코딩/재인코딩 없이 그대로 VLM에 전달한다.
    try:
        jpeg = base64.b64decode(image_base64, validate=True)
    except binascii.Error:
        raise HTTPException(status_code=400, detail="이미지 디코딩 실패")

    if not jpeg.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=400, detail="JPEG 형식이 아닙니다.")

    print(f"[API] Mode: {mode} / JPEG {len(jpeg) // 1024}KB")

    # 3. 음성 질문 여부에 따른 분기
    if input_audio_base64:

        # Reactive: 사용자 음성 질문
        t = time.perf_counter()
        question = transcribe_audio(input_audio_base64)
        timings["stt_ms"] = round((time.perf_counter() - t) * 1000)

        if question:
            question = f"사용자 음성 질문: {question}"

    else:
        if mode == "read":
            question = "이미지에서 확인할 수 있는 글자를 읽어 줘."

        elif mode == "find":
            question = "주요 물체와 위치를 알려줘."

        else:
            question = "자동 주변 안내를 수행해 줘."

    # 4. VLM 호출
    t = time.perf_counter()
    if question:
        # 글자 읽기는 작은 글씨 판독이 필요하므로 고해상도로 처리
        detail = "high" if mode == "read" else None
        result = analyze_scene(image_base64, question, detail=detail)
    else:
        result = {"hazard": "unknown", "speak": "질문을 알아듣지 못했습니다."}
    timings["vlm_ms"] = round((time.perf_counter() - t) * 1000)

    answer = result["speak"]
    hazard = result["hazard"]

    # 위험 단서가 있으면 앱에 obstacle로 알림 → 앱이 우선 재생할 수 있음
    kind = "obstacle" if hazard in ("observed", "possible") else "info"

    print(f"[VLM] ({kind}) {answer}")

    # 5. TTS 호출
    output_audio_base64 = None
    if USE_SERVER_TTS:
        t = time.perf_counter()
        output_audio_base64 = generate_speech(answer)
        timings["tts_ms"] = round((time.perf_counter() - t) * 1000)

    timings["total_ms"] = round((time.perf_counter() - t0) * 1000)
    print(f"[API] 단계별 시간(ms): {timings}")

    # 6. 응답 반환
    # hazard, timings_ms는 앱이 무시하는 추가 필드 (관제 로그·디버깅용)
    return {
        "text": answer,
        "kind": kind,
        "direction": "none",
        "audio_wav_base64": output_audio_base64,
        "hazard": hazard,
        "timings_ms": timings
    }