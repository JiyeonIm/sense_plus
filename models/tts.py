import base64
import time
import io
import wave

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI()


def generate_speech(text):

    start = time.perf_counter()

    # 1. Raw PCM 데이터 생성
    response = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text,
        response_format="pcm"
    )

    pcm_bytes = response.content

    # 2. 정상적인 WAV 헤더 생성
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm_bytes)

    audio_bytes = buffer.getvalue()

    # 3. 생성된 WAV 검증
    with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
        rate = wav.getframerate()
        frames = wav.getnframes()
        duration = frames / rate

    print(f"[TTS] Sample rate: {rate}")
    print(f"[TTS] Frames: {frames}")
    print(f"[TTS] Duration: {duration:.2f} sec")
    print(f"[TTS] Bytes: {len(audio_bytes)}")

    # 4. Base64 변환
    audio_base64 = base64.b64encode(
        audio_bytes
    ).decode("utf-8")

    elapsed = time.perf_counter() - start

    print(f"[TTS] 응답 시간: {elapsed:.2f}초")

    return audio_base64