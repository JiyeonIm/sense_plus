import asyncio
import base64
import io
import json
import unittest

import httpx
from PIL import Image

from senseplus.vision import TEAM_VISION_MODEL, OllamaVision, VisionError


def jpeg_fixture():
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "blue").save(output, "JPEG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def describe(content):
    return httpx.Response(200, json={
        "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)},
        "done": True,
        "done_reason": "stop",
    })


class OllamaVisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.jpg = jpeg_fixture()
        self.metadata = {"capabilities": ["completion", "vision"], "model_info": {"general.architecture": "qwen3vl"}}

    async def test_modes_send_only_local_jpeg_and_text_after_capability_check(self):
        calls = []

        def handle(request):
            self.assertEqual(request.url.host, "127.0.0.1")
            path = request.url.path
            payload = json.loads(request.content)
            calls.append((path, payload))
            if path == "/api/show":
                return httpx.Response(200, json=self.metadata)
            self.assertEqual(path, "/api/chat")
            return describe({"text": "화면 오른쪽에 문이 보입니다.", "kind": "direction", "direction": "right"})

        vision = OllamaVision(TEAM_VISION_MODEL, 1, transport=httpx.MockTransport(handle))
        question = "어디에 있나요?"
        for mode, prompt_key in {"guide": "주변 구조", "scene": "한두 문장", "find": "물건", "read": "글자"}.items():
            with self.subTest(mode=mode):
                result = await vision.analyze(jpeg_base64=self.jpg, mode=mode, question=question)
                self.assertEqual(result.direction, "right")
                self.assertEqual(calls[-2], ("/api/show", {"model": TEAM_VISION_MODEL}))
                chat = calls[-1][1]
                self.assertEqual(chat["messages"][1]["images"], [self.jpg])
                self.assertIn(prompt_key, chat["messages"][1]["content"])
                self.assertIn(question, chat["messages"][1]["content"])
                self.assertNotIn("think", chat)
                self.assertFalse(chat["stream"])
                self.assertEqual(set(chat["format"]["required"]), {"text", "kind", "direction"})

    async def test_find_without_a_target_does_not_start_model(self):
        def fail(_request):
            self.fail("an unspecified target should not invoke the model")

        vision = OllamaVision(TEAM_VISION_MODEL, 1, transport=httpx.MockTransport(fail))
        result = await vision.analyze(jpeg_base64=self.jpg, mode="find", question="  ")
        self.assertEqual(result.kind, "uncertain")
        self.assertIn("어떤 물건", result.text)

    async def test_only_installed_vision_models_receive_media(self):
        cases = [
            ({**self.metadata, "remote_host": "cloud.invalid"}, "PC에 설치된"),
            ({"capabilities": ["vision"]}, "PC에 설치된"),
            ({**self.metadata, "capabilities": ["completion"]}, "영상 입력"),
            (None, "설치되어 있지"),
        ]
        for metadata, expected in cases:
            with self.subTest(expected=expected):
                def handle(request):
                    self.assertEqual(request.url.path, "/api/show")
                    self.assertNotIn(self.jpg.encode(), request.content)
                    return httpx.Response(200, json=metadata) if metadata else httpx.Response(404)

                vision = OllamaVision(TEAM_VISION_MODEL, 1, transport=httpx.MockTransport(handle))
                with self.assertRaisesRegex(VisionError, expected):
                    await vision.analyze(jpeg_base64=self.jpg, mode="scene")

    async def test_safety_guarantee_question_is_answered_without_model(self):
        def fail(_request):
            self.fail("a walking safety guarantee should not invoke the model")

        vision = OllamaVision(TEAM_VISION_MODEL, 1, transport=httpx.MockTransport(fail))
        result = await vision.analyze(
            jpeg_base64=self.jpg, mode="guide", question="오른쪽으로 걸어가도 안전하다고 보장해 줄 수 있나요?"
        )
        self.assertEqual((result.kind, result.direction), ("uncertain", "none"))
        self.assertIn("이동 가능 여부를 확인할 수 없어요", result.text)

    async def test_blocks_navigation_and_unqualified_direction(self):
        self.metadata["capabilities"].append("thinking")
        contents = iter([
            {"text": "오른쪽으로 걸어가면 출입구 표시가 있는 방향으로 이동합니다.", "kind": "direction", "direction": "ahead"},
            {"text": "책이 문 옆에 보입니다.", "kind": "info", "direction": "left"},
        ])

        def handle(request):
            if request.url.path == "/api/show":
                return httpx.Response(200, json=self.metadata)
            self.assertIs(json.loads(request.content)["think"], False)
            return describe(next(contents))

        vision = OllamaVision(TEAM_VISION_MODEL, 1, transport=httpx.MockTransport(handle))
        unsafe = await vision.analyze(jpeg_base64=self.jpg, mode="guide")
        self.assertEqual((unsafe.kind, unsafe.direction), ("uncertain", "none"))
        self.assertNotIn("걸어가면", unsafe.text)
        relation = await vision.analyze(jpeg_base64=self.jpg, mode="scene")
        self.assertEqual((relation.kind, relation.direction), ("info", "none"))

    async def test_invalid_model_output_and_timeout_have_safe_errors(self):
        def invalid(request):
            if request.url.path == "/api/show":
                return httpx.Response(200, json=self.metadata)
            return describe({"text": "malformed output", "kind": "unknown", "direction": "none"})

        async def slow(_request):
            await asyncio.sleep(0.2)
            return httpx.Response(200, json=self.metadata)

        invalid_vision = OllamaVision(TEAM_VISION_MODEL, 1, transport=httpx.MockTransport(invalid))
        with self.assertRaisesRegex(VisionError, "응답 형식"):
            await invalid_vision.analyze(jpeg_base64=self.jpg, mode="scene")
        timed = OllamaVision(TEAM_VISION_MODEL, 0.01, transport=httpx.MockTransport(slow))
        with self.assertRaisesRegex(VisionError, "시간이 초과"):
            await timed.analyze(jpeg_base64=self.jpg, mode="scene")

    async def test_external_urls_cloud_models_and_invalid_images_are_rejected(self):
        for url in ["http://localhost:11434", "http://192.168.1.1:11434", "https://127.0.0.1", "http://127.0.0.1@external.invalid"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaVision(TEAM_VISION_MODEL, 1, base_url=url)
        with self.assertRaises(ValueError):
            OllamaVision("qwen3-vl:235b-cloud", 1)

        def fail(_request):
            self.fail("invalid input should not reach Ollama")

        vision = OllamaVision(TEAM_VISION_MODEL, 1, transport=httpx.MockTransport(fail))
        with self.assertRaisesRegex(VisionError, "JPEG 형식"):
            await vision.analyze(jpeg_base64=base64.b64encode(b"not an image").decode(), mode="scene")
        with self.assertRaisesRegex(VisionError, "분석 모드"):
            await vision.analyze(jpeg_base64=self.jpg, mode="unsupported")
