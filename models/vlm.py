import os
import cv2
import json
import base64
import time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI()

MODEL_NAME = os.getenv("SENSE_VLM_MODEL", "gpt-4o-mini")

# 이미지 해상도 처리 방식
# low  : 512px 저해상도 1장으로 처리 → 이미지 토큰이 고정·소량이라 응답이 빠름
# high : 타일 단위 고해상도 처리 → 작은 글자 판독(READ)에 유리하지만 느림
IMAGE_DETAIL = os.getenv("SENSE_VLM_DETAIL", "low")

# 음성 안내는 1~3문장이면 충분 → 출력 토큰을 줄이면 생성 시간이 줄어듦
MAX_OUTPUT_TOKENS = int(os.getenv("SENSE_VLM_MAX_TOKENS", "120"))

# 추론(reasoning) 모델용 설정. 예: gpt-6-luna, gpt-6.1-terra는 "none" 가능
# 비워 두면 보내지 않음 (gpt-4o-mini처럼 추론 설정이 없는 모델용)
# none : 생각 단계를 건너뛰고 바로 답함 → 가장 빠름
REASONING_EFFORT = os.getenv("SENSE_VLM_REASONING", "").strip()

HAZARD_PREFIX = "주의하세요."

SYSTEM_PROMPT = """
당신은 시각장애인의 일상생활을 지원하는
AI 시각 보조 어시스턴트 SENSE+입니다.

제공된 이미지와 사용자 질문을 기반으로 답변하세요.

[공통 응답 원칙]
- 한국어로 간결하고 자연스럽게 답변하세요.
- 물체의 위치는 카메라 화면 기준 왼쪽, 중앙, 오른쪽으로 표현하세요.
- 이미지에서 확인할 수 없는 정보는 추측하지 마세요.
- 정확한 거리, 이동 가능 여부, 안전 여부를 단정하지 마세요.
- 물체나 문자를 인식하기 어렵다면 솔직하게 알리세요.
- 불필요한 서론, 마크다운, 반복 표현을 사용하지 마세요.

[자동 주변 안내]
- 중요한 물체, 위치, 주변 환경을 우선 설명하세요.
- 모든 물체를 나열하지 마세요.
- 위험 단서가 없다면 20자 내외의 한 문장으로 답변하세요.
- "화면에는", "가장 중요한 물체는" 등의 서론을 생략하세요.

[사용자 질문]
- 사용자 질문과 직접 관련된 정보를 우선 전달하세요.
- 물건 찾기, 위치 안내, 문자 읽기 등의 요청에 답변하세요.
- 필요한 정보를 생략하지 않는 범위에서 간결하게 답변하세요.
- 자동 주변 안내의 글자 수 제한을 적용하지 마세요.

[위험 판단]
- 계단, 단차, 열린 구멍, 차량, 차도 경계,
  돌출물, 바닥의 전선, 날카로운 물건 등
  시각적으로 확인되는 위험 단서를 살펴보세요.
- 물체가 존재한다는 이유만으로 위험하다고 판단하지 마세요.
- hazard 분류 기준:
  observed: 위험 단서가 이미지에서 명확하게 관찰됨.
  possible: 시각적 근거는 있으나 위험 단서가 불명확함.
  unknown: 위험 단서를 확인하지 못함.
- observed 또는 possible이면 speak를
  반드시 "주의하세요."로 시작하세요.
- 위험 단서와 위치를 먼저 전달한 뒤 질문에 답변하세요.
- 불확실한 위험 단서는 가능성으로 표현하세요.
- 위험 안내에는 자동 안내의 글자 수 제한을 적용하지 마세요.
- unknown은 안전하다는 의미가 아닙니다.
- "안전합니다", "장애물이 없습니다"라고 단정하지 마세요.

[응답 예시]
- 중앙에 노트북이 있습니다.
- 오른쪽에 사람이 있습니다.
- 왼쪽에 출입문이 있습니다.
- 주의하세요. 왼쪽 바닥에 전선이 있습니다.
- 주의하세요. 중앙에 단차가 있는 것으로 보입니다.
- 글자가 흐려 읽기 어렵습니다.

[출력]
- hazard에는 observed, possible, unknown 중 하나를 넣으세요.
- speak에는 실제 음성으로 전달할 한국어 문장만 넣으세요.
"""


# 모델이 항상 이 형식의 JSON만 내도록 강제 (Structured Outputs)
RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "sense_answer",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "hazard": {
                "type": "string",
                "enum": ["observed", "possible", "unknown"]
            },
            "speak": {"type": "string"}
        },
        "required": ["hazard", "speak"],
        "additionalProperties": False
    }
}


def _to_jpeg_base64(image):
    """
    입력이 이미 JPEG base64 문자열이면 그대로 사용하고,
    OpenCV 이미지(ndarray)면 그때만 JPEG로 인코딩한다.
    (앱이 보낸 640x480 JPEG를 다시 디코딩·인코딩하지 않기 위함)
    """
    if isinstance(image, str):
        return image

    success, buffer = cv2.imencode(
        ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80]
    )

    if not success:
        raise ValueError("이미지 인코딩에 실패했습니다.")

    return base64.b64encode(buffer).decode("utf-8")


def _parse(raw):
    """
    모델 출력(JSON)을 {"hazard", "speak"}로 정리한다.
    JSON이 깨졌으면 원문을 그대로 읽어 주고 hazard는 unknown으로 둔다.
    """
    try:
        data = json.loads(raw)
        hazard = data.get("hazard", "unknown")
        speak = str(data.get("speak", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        hazard = "unknown"
        speak = "화면을 확인하기 어렵습니다."

    if hazard not in ("observed", "possible", "unknown"):
        hazard = "unknown"

    if not speak:
        speak = "화면을 확인하지 못했습니다. 다시 시도해 주세요."

    # 위험이면 첫 문장이 반드시 "주의하세요."로 시작하도록 보장 (명세 C7)
    if hazard != "unknown" and not speak.startswith(HAZARD_PREFIX):
        speak = f"{HAZARD_PREFIX} {speak}"

    return {"hazard": hazard, "speak": speak}


def analyze_scene(image, question, detail=None):
    """이미지와 질문을 분석해 {"hazard": ..., "speak": ...}를 돌려준다."""

    if image is None:
        raise ValueError("입력 이미지가 없습니다.")

    start_time = time.perf_counter()

    image_base64 = _to_jpeg_base64(image)

    extra = {}
    if REASONING_EFFORT:
        extra["reasoning"] = {"effort": REASONING_EFFORT}

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
                        ),
                        "detail": detail or IMAGE_DETAIL
                    }
                ]
            }
        ],
        text={"format": RESPONSE_FORMAT},
        max_output_tokens=MAX_OUTPUT_TOKENS,
        store=False,
        **extra
    )

    result = _parse(response.output_text)

    elapsed = time.perf_counter() - start_time

    print(
        f"[VLM] 응답 시간: {elapsed:.2f}초 "
        f"({MODEL_NAME}, detail={detail or IMAGE_DETAIL}, hazard={result['hazard']})"
    )

    return result


def analyze_image(image, question, detail=None):
    """기존 테스트 스크립트(test_vlm.py 등)용: 읽을 문장만 돌려준다."""
    return analyze_scene(image, question, detail)["speak"]