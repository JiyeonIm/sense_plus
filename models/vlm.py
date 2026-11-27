import cv2
import base64
import time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI()

MODEL_NAME = "gpt-4o-mini"

SYSTEM_PROMPT = """
당신은 시각장애인의 일상생활을 지원하는
AI 시각 보조 어시스턴트 SENSE+입니다.

사용자가 제공한 이미지와 질문을 기반으로 답변하세요.

[응답 원칙]
- 한국어로 간결하고 명확하게 답변하세요.
- 사용자 질문과 직접 관련된 정보를 우선 전달하세요.
- 물체의 위치는 화면 기준 왼쪽, 오른쪽, 중앙으로 표현하세요.
- 이미지에서 확인할 수 없는 정보는 추측하지 마세요.
- 정확한 거리나 안전 여부를 단정하지 마세요.
- 물체나 문자를 인식하기 어렵다면 솔직하게 알리세요.
- 불필요한 서론이나 마크다운을 사용하지 마세요.

[주요 기능]
1. 주변 환경 설명
2. 물건 찾기 및 위치 안내
3. 이미지 속 문자 읽기
"""


def analyze_image(image, question):

    if image is None:
        raise ValueError("입력 이미지가 없습니다.")

    start_time = time.perf_counter()

    # 1. OpenCV 이미지를 JPEG로 인코딩
    success, buffer = cv2.imencode(".jpg", image)

    if not success:
        raise ValueError("이미지 인코딩에 실패했습니다.")

    # 2. Base64 문자열로 변환
    image_base64 = base64.b64encode(buffer).decode("utf-8")

    # 3. VLM 호출
    response = client.responses.create(
        model=MODEL_NAME,
        instructions=SYSTEM_PROMPT,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": question
                    },
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:image/jpeg;base64,{image_base64}"
                        )
                    }
                ]
            }
        ],
        max_output_tokens=300,
        store=False
    )

    answer = response.output_text

    elapsed = time.perf_counter() - start_time

    print(f"[VLM] 응답 시간: {elapsed:.2f}초")

    return answer