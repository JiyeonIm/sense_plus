import base64
import cv2
import numpy as np

from fastapi import FastAPI, HTTPException
from models.vlm import analyze_image
from models.tts import generate_speech


app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
def analyze(payload: dict):

    mode = payload.get("mode", "scene")

    # 1. 이미지 데이터 확인
    image_base64 = payload.get("jpeg_base64")

    if not image_base64:
        raise HTTPException(
            status_code=400,
            detail="이미지가 없습니다."
        )

    # 2. Base64 → OpenCV Image (BGR)
    image_bytes = base64.b64decode(image_base64)

    image_array = np.frombuffer(
        image_bytes,
        dtype=np.uint8
    )

    frame = cv2.imdecode(
        image_array,
        cv2.IMREAD_COLOR
    )

    if frame is None:
        raise HTTPException(
            status_code=400,
            detail="이미지 디코딩 실패"
        )

    print(f"[API] Mode: {mode}")
    print(f"[API] Image shape: {frame.shape}")

    # 3. VLM 질문 설정
    question = (
        "현재 화면에서 확인할 수 있는 주요 물체와 "
        "위치를 설명해 줘. "
        "가장 중요한 정보만 전달하고, "
        "반드시 50자 이내의 한 문장으로 답변해 줘."
    )

    # 4. VLM 호출
    answer = analyze_image(frame, question)

    print(f"[VLM] {answer}")

    # 5. TTS 호출
    audio_base64 = generate_speech(answer)

    # 6. 응답 반환
    return {
        "text": answer,
        "kind": "info",
        "direction": "none",
        "audio_wav_base64": audio_base64
    }