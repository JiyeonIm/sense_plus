"""In-memory authenticated answer playback, including safe expiry."""

import io
import unittest
import wave

import httpx
import numpy as np

from senseplus.adapter import AnalysisResult, OUTPUT_RATE
from senseplus.config import Config
from senseplus.core import SessionManager
from senseplus.server import create_app


class AnswerAudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = SessionManager(Config())
        app = create_app(manager=self.manager, enable_rtc=False)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.manager.close_all)

    async def connect(self, *, http_audio=False):
        headers = {"X-Sense-Answer-Audio": "http"} if http_audio else {}
        response = await self.client.post("/api/sessions", headers=headers)
        response.raise_for_status()
        created = response.json()
        session = self.manager.sessions[created["session_id"]]
        self.manager.media_video(session, np.zeros((96, 128, 3), dtype=np.uint8))
        return session, {"Authorization": f"Bearer {created['token']}"}

    def publish(self, session, *, with_audio=True):
        samples = np.array([[1500, -2000, 500, 3]], dtype=np.int16)
        self.manager._publish(session, session.generation, "turn", AnalysisResult(
            "합성 시험 답변", audio=samples if with_audio else None,
        ))
        path = f"/api/sessions/{session.session_id}/audio/{session.latest_result['id']}.wav"
        return path, samples

    async def test_http_wav_requires_own_bearer_and_never_enqueues_webrtc(self):
        health = await self.client.get("/api/health")
        self.assertTrue(health.json()["answer_audio_http"])
        session, headers = await self.connect(http_audio=True)
        _, wrong_headers = await self.connect(http_audio=True)
        path, pcm = self.publish(session)

        state = await self.client.get(f"/api/sessions/{session.session_id}/state", headers=headers)
        self.assertEqual(state.json()["latest_result"]["audio_source"], "http-wav")
        self.assertTrue(state.json()["latest_result"]["audio_available"])
        self.assertNotIn("latest_answer_wav", state.text)
        self.assertTrue(session.output_audio.empty())
        self.assertEqual((await self.client.get(path)).status_code, 401)
        self.assertEqual((await self.client.get(path, headers=wrong_headers)).status_code, 401)

        response = await self.client.get(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.headers["cache-control"], "no-store")
        with wave.open(io.BytesIO(response.content), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, OUTPUT_RATE))
            self.assertEqual(wav.readframes(wav.getnframes()), pcm.astype("<i2").tobytes())

    async def test_only_latest_fresh_answer_is_available(self):
        session, headers = await self.connect(http_audio=True)
        previous, _ = self.publish(session)
        current, _ = self.publish(session)
        self.assertEqual((await self.client.get(previous, headers=headers)).status_code, 404)
        session.last_video_at -= 10
        self.assertEqual((await self.client.get(current, headers=headers)).status_code, 409)
        self.assertIsNone(session.latest_answer_wav)

        self.manager.media_video(session, np.zeros((96, 128, 3), dtype=np.uint8))
        self.assertEqual((await self.client.get(current, headers=headers)).status_code, 404)
        current, _ = self.publish(session)
        await self.manager.control(session, "guide.stop")
        self.assertEqual((await self.client.get(current, headers=headers)).status_code, 404)

        current, _ = self.publish(session)
        await self.manager.close(session.session_id)
        self.assertIsNone(session.latest_answer_wav)
        self.assertEqual((await self.client.get(current, headers=headers)).status_code, 404)

    async def test_legacy_rtc_text_only_and_test_tone_keep_their_routes(self):
        legacy, headers = await self.connect()
        path, _ = self.publish(legacy)
        self.assertEqual(legacy.latest_result["audio_source"], "webrtc")
        self.assertFalse(legacy.output_audio.empty())
        self.assertEqual((await self.client.get(path, headers=headers)).status_code, 404)

        opted_in, headers = await self.connect(http_audio=True)
        path, _ = self.publish(opted_in, with_audio=False)
        self.assertEqual(opted_in.latest_result["audio_source"], "browser-tts")
        self.assertEqual((await self.client.get(path, headers=headers)).status_code, 404)
        await self.manager.control(opted_in, "audio.test")
        self.assertEqual(opted_in.latest_result["audio_source"], "webrtc")
        self.assertFalse(opted_in.output_audio.empty())
