"""Meaningful regression tests for consent, freshness, isolation and turn order."""

import asyncio
import base64
import io
import time
import unittest
import wave
from unittest.mock import patch

import httpx
import numpy as np
from PIL import Image

from senseplus.adapter import AdapterError, AnalysisResult, TeamAdapter, decode_wav, encode_wav
from senseplus.config import Config
from senseplus.core import INPUT_RATE, SessionError, SessionManager
from senseplus.server import create_app


class CaptureAdapter:
    def __init__(self):
        self.payloads = []

    async def analyze(self, payload):
        self.payloads.append(payload)
        return AnalysisResult("테스트 서버 응답", "info", "none")


class BackendStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = CaptureAdapter()
        self.manager = SessionManager(Config(audio_tail_ms=30), self.adapter)
        self.session = self.manager.create()

    async def asyncTearDown(self):
        await self.manager.close_all()

    def frame(self, color=(255, 0, 0)):
        value = np.empty((120, 160, 3), dtype=np.uint8)
        value[:] = color
        value[25:95, 45:110] = (250, 250, 250)
        self.manager.media_video(self.session, value)

    async def start_question(self, mode="guide", turn_id=None, color=(255, 0, 0)):
        self.frame(color)
        request = asyncio.create_task(self.manager.control(self.session, "question.start", mode, turn_id))
        await asyncio.sleep(0)
        for _ in range(5):
            self.frame(color)
            await asyncio.sleep(.07)
        return await request

    async def test_session_tokens_are_isolated_and_not_returned_in_state(self):
        other = self.manager.create()
        with self.assertRaises(SessionError) as raised:
            self.manager.authorize(self.session.session_id, other.token)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("token", self.session.public_state(True))
        self.assertIs(self.manager.authorize(self.session.session_id, self.session.token), self.session)

    async def test_question_rejects_missing_and_stale_frames(self):
        with self.assertRaises(SessionError):
            await self.manager.control(self.session, "question.start")
        self.frame()
        self.session.last_video_at -= 5
        with self.assertRaises(SessionError) as raised:
            await self.manager.control(self.session, "question.start")
        self.assertIn("오래되어", raised.exception.detail)
        self.assertFalse(self.session.recording)

    async def test_question_uses_start_frame_and_captures_late_audio_tail(self):
        started = await self.start_question("scene")
        self.manager.media_audio(self.session, INPUT_RATE, np.ones((1, 2000), dtype=np.int16))
        self.frame((0, 0, 255))
        await self.manager.control(self.session, "question.end", "scene", started["current_turn_id"])
        await asyncio.sleep(.005)
        self.manager.media_audio(self.session, INPUT_RATE, np.ones((1, 500), dtype=np.int16))
        await self.session.task
        payload = self.adapter.payloads[0]
        decoded = np.array(Image.open(io.BytesIO(base64.b64decode(payload["jpeg_base64"]))))
        self.assertGreater(int(decoded[0, 0, 0]), 240)
        self.assertLess(int(decoded[0, 0, 2]), 10)
        with wave.open(io.BytesIO(base64.b64decode(payload["audio_wav_base64"])), "rb") as wav:
            self.assertEqual(wav.getnframes(), 2500)
        self.assertEqual(payload["turn_id"], started["current_turn_id"])
        self.assertEqual(self.session.latest_result["source"], "ai")
        self.assertFalse(self.session.busy)

    async def test_old_question_end_cannot_finish_a_new_turn(self):
        await self.start_question(turn_id="first")
        await self.start_question(turn_id="second")
        with self.assertRaises(SessionError):
            await self.manager.control(self.session, "question.end", turn_id="first")
        self.assertTrue(self.session.recording)
        self.assertEqual(self.session.current_turn_id, "second")

    async def test_ai_is_never_fabricated_when_unconfigured(self):
        manager = SessionManager(Config())
        session = manager.create()
        state = await manager.control(session, "guide.start")
        self.assertEqual(state["latest_result"]["source"], "system")
        self.assertEqual(state["latest_result"]["direction"], "none")
        self.assertIn("연결되지", state["latest_result"]["text"])
        self.assertFalse(state["latest_result"]["audio_available"])
        self.assertEqual(state["video_frames"], 0)
        await manager.close_all()

    async def test_cancelled_old_result_cannot_replace_new_turn(self):
        class SlowAdapter:
            async def analyze(inner, payload):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    return AnalysisResult("폐기해야 하는 이전 안내", "direction", "left")

        self.manager.adapter = SlowAdapter()
        self.session.task = asyncio.create_task(self.manager._analyze(self.session, self.session.generation, "old", {}))
        old_task = self.session.task
        await asyncio.sleep(0)
        await self.manager.control(self.session, "guide.stop")
        await old_task
        self.assertIsNone(self.session.latest_result)
        self.assertTrue(self.session.output_audio.empty())

    async def test_old_guide_frame_is_discarded_even_if_request_returns(self):
        await self.manager._analyze(self.session, self.session.generation, "late", {}, frame_monotonic=time.monotonic() - 5)
        self.assertEqual(self.session.latest_result["source"], "system")
        self.assertEqual(self.session.latest_result["direction"], "none")
        self.assertIn("보류", self.session.latest_result["text"])

    async def test_question_rejects_ai_reply_if_camera_stalls_during_inference(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        class SlowAdapter:
            async def analyze(inner, payload):
                entered.set()
                await finish.wait()
                return AnalysisResult("새 영상이 필요합니다.", audio=np.ones((1, 800), dtype=np.int16))

        self.manager.adapter = SlowAdapter()
        started = await self.start_question("scene")
        self.manager.media_audio(self.session, INPUT_RATE, np.ones((1, 2000), dtype=np.int16))
        await self.manager.control(self.session, "question.end", "scene", started["current_turn_id"])
        await asyncio.wait_for(entered.wait(), 1)
        self.session.last_video_at -= 10
        finish.set()
        await self.session.task
        self.assertEqual(self.session.latest_result["source"], "system")
        self.assertIn("보류", self.session.latest_result["text"])
        self.assertTrue(self.session.output_audio.empty())

    async def test_audio_memory_is_bounded_to_twenty_seconds(self):
        await self.start_question()
        for _ in range(25):
            self.manager.media_audio(self.session, INPUT_RATE, np.ones((1, INPUT_RATE), dtype=np.int16))
        self.assertEqual(self.session.question_samples, 20 * INPUT_RATE)
        self.assertEqual(sum(part.size for part in self.session.question_audio), 20 * INPUT_RATE)
        self.assertTrue(self.session.audio_truncated)

    async def test_audio_test_requires_connection_and_explicit_action(self):
        self.assertTrue(self.session.output_audio.empty())
        with self.assertRaises(SessionError):
            await self.manager.control(self.session, "audio.test")
        self.frame()
        self.assertTrue(self.session.output_audio.empty())
        await self.manager.control(self.session, "audio.test")
        self.assertFalse(self.session.output_audio.empty())
        self.assertEqual(self.session.latest_result["source"], "transport-test")

    async def test_delete_releases_media_and_invalidates_auth(self):
        self.frame()
        await self.manager.close(self.session.session_id)
        self.assertIsNone(self.session.latest_video)
        with self.assertRaises(SessionError):
            self.manager.authorize(self.session.session_id, self.session.token)


class ApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_auth_health_and_payload_limits(self):
        config = Config(ai_key="DO-NOT-EXPOSE")
        app = create_app(config=config, enable_rtc=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            health = await client.get("/api/health")
            self.assertNotIn("DO-NOT-EXPOSE", health.text)
            self.assertFalse(health.json()["ai_configured"])
            self.assertFalse(health.json()["rtc_available"])
            created = (await client.post("/api/sessions")).json()
            sid = created["session_id"]
            headers = {"Authorization": f"Bearer {created['token']}"}
            self.assertEqual((await client.get(f"/api/sessions/{sid}/state")).status_code, 401)
            self.assertEqual((await client.get(f"/api/sessions/{sid}/state", headers=headers)).status_code, 200)
            self.assertEqual((await client.post(f"/api/sessions/{sid}/control", headers=headers, content="x" * 5000)).status_code, 413)
            self.assertEqual((await client.post("/webrtc/offer", json={"type":"offer", "webrtc_id":sid, "sdp":"test"})).status_code, 401)
            self.assertEqual((await client.post(f"/api/sessions/{sid}/control", headers=headers, json={"action":"unsafe.command"})).status_code, 422)
            self.assertEqual((await client.delete(f"/api/sessions/{sid}", headers=headers)).status_code, 200)
            self.assertEqual((await client.get(f"/api/sessions/{sid}/state", headers=headers)).status_code, 404)


class WavTests(unittest.TestCase):
    def test_valid_pcm_is_decoded_and_invalid_payload_is_rejected(self):
        source = np.arange(1600, dtype=np.int16)
        output = decode_wav(encode_wav(source, 16000))
        self.assertEqual(output.shape, (1, 2400))
        with self.assertRaises(AdapterError):
            decode_wav("not-valid-base64!")


class TeamHttpAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_contract_auth_and_optional_wav_are_processed(self):
        observed = []
        def respond(request):
            observed.append(request)
            return httpx.Response(200, json={
                "text": "테스트 응답", "kind": "info", "direction": "none",
                "audio_wav_base64": encode_wav(np.ones(1600, dtype=np.int16), 16000),
            })
        real_client = httpx.AsyncClient
        def local_client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)
        adapter = TeamAdapter(Config(ai_url="https://team.invalid/analyze", ai_key="test-only-key"))
        with patch("senseplus.adapter.httpx.AsyncClient", side_effect=local_client):
            result = await adapter.analyze({"session_id":"test", "turn_id":"turn", "jpeg_base64":"test-image"})
        self.assertEqual(observed[0].headers["authorization"], "Bearer test-only-key")
        self.assertEqual(result.text, "테스트 응답")
        self.assertEqual(result.audio.shape, (1, 2400))

    async def test_invalid_team_response_never_becomes_guidance(self):
        real_client = httpx.AsyncClient
        def respond(request):
            return httpx.Response(200, json={"text":"테스트", "kind":"unverified", "direction":"up"})
        def local_client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)
        with patch("senseplus.adapter.httpx.AsyncClient", side_effect=local_client):
            with self.assertRaises(AdapterError):
                await TeamAdapter(Config(ai_url="https://team.invalid/analyze")).analyze({})


if __name__ == "__main__":
    unittest.main()
