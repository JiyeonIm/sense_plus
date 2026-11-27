import base64
import io
import time

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI()

MODEL_NAME = "gpt-4o-mini-transcribe"


def transcribe_audio(audio_base64):

    start = time.perf_counter()

    # 1. Base64 → WAV bytes
    audio_bytes = base64.b64decode(
        audio_base64,
        validate=True
    )

    # 2. 메모리상에 WAV 파일 생성
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "question.wav"

    # 3. STT 호출
    response = client.audio.transcriptions.create(
        model=MODEL_NAME,
        file=audio_file,
        language="ko"
    )

    # 4. 질문 텍스트 추출
    question = response.text.strip()

    elapsed = time.perf_counter() - start

    print(f"[STT] 응답 시간: {elapsed:.2f}초")
    print(f"[STT] 사용자 질문: {question}")

    return question