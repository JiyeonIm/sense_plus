import base64
import cv2
import numpy as np

from fastapi import FastAPI, HTTPException
from models.vlm import analyze_image
from models.tts import generate_speech
from models.stt import transcribe_audio


app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
def analyze(payload: dict):

    # 1. 입력 데이터 확인
    mode = payload.get("mode", "scene")
    image_base64 = payload.get("jpeg_base64")
    input_audio_base64 = payload.get("audio_wav_base64")

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

    # 3. 음성 질문 여부에 따른 분기
    if input_audio_base64:

        # Reactive: 사용자 음성 질문
        print("[API] Reactive mode")

        question = transcribe_audio(input_audio_base64)

    else:

        # Proactive: 일반 주변 환경 설명
        print("[API] Proactive mode")

        question = (
            "화면에서 가장 중요한 물체와 위치를 알려줘. "
            "한 문장으로 25자 내외로 답변해 줘."
        )

    # 4. VLM 호출
    if question:
        answer = analyze_image(frame, question)
    else:
        answer = "질문을 알아듣지 못했습니다."

    print(f"[VLM] {answer}")

    # 5. TTS 호출
    output_audio_base64 = generate_speech(answer)

    # 6. 응답 반환
    return {
        "text": answer,
        "kind": "info",
        "direction": "none",
        "audio_wav_base64": output_audio_base64
    }