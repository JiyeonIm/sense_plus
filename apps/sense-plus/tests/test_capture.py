import asyncio
import base64
from datetime import datetime
from io import BytesIO
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageFilter

from senseplus.adapter import AnalysisResult
from senseplus.capture import FrameStability
from senseplus.config import Config
from senseplus.core import INPUT_RATE, SessionError, SessionManager


def edge_pattern(offset: int = 0) -> Image.Image:
    frame = np.full((96, 128, 3), 30, dtype=np.uint8)
    frame[:, 30 + offset:60 + offset] = 230
    return Image.fromarray(frame)


class FrameStabilityTests(unittest.TestCase):
    def test_selects_a_sharp_frame_after_a_continuous_run(self):
        stability = FrameStability()
        frame = edge_pattern()
        results = [stability.accepts(frame, sample / 100) for sample in (0, 7, 14, 21, 28)]
        self.assertEqual(results, [False, False, False, False, True])

    def test_rejects_blur_slow_drift_and_gapped_frames(self):
        blurred = edge_pattern().filter(ImageFilter.GaussianBlur(radius=5))
        stability = FrameStability()
        self.assertFalse(any(stability.accepts(blurred, sample * 0.07) for sample in range(10)))

        stability = FrameStability()
        self.assertFalse(any(stability.accepts(edge_pattern(sample), sample * 0.07) for sample in range(10)))

        stability = FrameStability()
        frame = edge_pattern()
        self.assertFalse(any(stability.accepts(frame, sample / 100) for sample in (0, 10, 20, 50, 60, 70)))


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_captures_only_new_frames_from_the_requested_session(self):
        manager = SessionManager(Config())
        session = manager.create()
        other = manager.create()
        frame = np.asarray(edge_pattern())
        manager.media_video(session, frame)
        task = asyncio.create_task(manager.capture_stable_frame(session))
        await asyncio.sleep(0)

        with self.assertRaises(SessionError) as conflict:
            await manager.capture_stable_frame(session)
        self.assertEqual(conflict.exception.status_code, 409)

        for _ in range(5):
            manager.media_video(other, frame)
            await asyncio.sleep(0.07)
        self.assertFalse(task.done())

        await manager.control(session, 'question.start')
        self.assertTrue(session.recording)
        for _ in range(5):
            manager.media_video(session, frame)
            await asyncio.sleep(0.07)
        captured = await asyncio.wait_for(task, 0.5)
        self.assertGreater(captured.frame_number, 1)
        self.assertIsNot(captured.rgb, session.latest_video)
        np.testing.assert_array_equal(captured.rgb, frame)
        self.assertIsNotNone(datetime.fromisoformat(captured.received_at).tzinfo)
        self.assertIsNone(session.pending_capture)

    async def test_timeout_cancel_and_close_release_the_capture(self):
        manager = SessionManager(Config())
        session = manager.create()
        with self.assertRaises(SessionError) as invalid:
            await manager.capture_stable_frame(session)
        self.assertEqual(invalid.exception.status_code, 409)

        manager.media_video(session, np.asarray(edge_pattern()))
        with patch('senseplus.core.CAPTURE_TIMEOUT', 0.04), self.assertRaises(SessionError) as timeout:
            await manager.capture_stable_frame(session)
        self.assertEqual(timeout.exception.status_code, 408)
        self.assertIsNone(session.pending_capture)

        task = asyncio.create_task(manager.capture_stable_frame(session))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(session.pending_capture)

        task = asyncio.create_task(manager.capture_stable_frame(session))
        await asyncio.sleep(0)
        await manager.close(session.session_id)
        with self.assertRaises(SessionError) as closed:
            await asyncio.wait_for(task, 0.5)
        self.assertEqual(closed.exception.status_code, 409)
        self.assertIsNone(session.pending_capture)


class VideoFpsTests(unittest.TestCase):
    def test_receipt_rate_and_stale_value_are_independent_of_polling(self):
        manager = SessionManager(Config())
        session = manager.create()
        frame = np.asarray(edge_pattern())
        self.assertIsNone(session.video_fps())

        with patch('senseplus.core.time.monotonic', return_value=50.0) as clock:
            manager.media_video(session, frame)
            self.assertIsNone(session.video_fps())
            for index in range(1, 41):
                clock.return_value = 50.0 + index / 20
                manager.media_video(session, frame)
            self.assertEqual(session.video_fps(), 20.0)
            clock.return_value += 2.0
            self.assertEqual(session.video_fps(), 0.0)

            # A new stream of frames drops the entire older window.
            manager.media_video(session, frame)
            self.assertIsNone(session.video_fps())
            clock.return_value += 0.1
            manager.media_video(session, frame)
            self.assertEqual(session.video_fps(), 10.0)


class InferenceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class RecordingAdapter:
            def __init__(self):
                self.payloads = []

            async def analyze(self, payload):
                self.payloads.append(payload)
                return AnalysisResult(f'테스트 응답 {len(self.payloads)}', audio=np.ones((1, 240), dtype=np.int16))

        self.adapter = RecordingAdapter()
        self.manager = SessionManager(Config(audio_tail_ms=0, guide_interval=.02), self.adapter)
        self.session = self.manager.create()
        self.manager.media_video(self.session, np.asarray(edge_pattern()))

    async def asyncTearDown(self):
        await self.manager.close_all()

    async def feed_stable_video(self, offset=15):
        frame = np.asarray(edge_pattern(offset))
        for _ in range(5):
            self.manager.media_video(self.session, frame)
            await asyncio.sleep(.07)

    async def feed_until(self, frame, ready, timeout=2.0):
        async with asyncio.timeout(timeout):
            while not ready():
                self.manager.media_video(self.session, frame)
                await asyncio.sleep(.065)

    async def test_scene_repeats_for_changed_frames_and_stops_on_request(self):
        await self.manager.control(self.session, 'guide.start', 'scene')
        task = self.session.task
        await asyncio.sleep(0)
        self.assertTrue(self.session.busy)
        self.assertEqual(self.adapter.payloads, [])
        self.assertIsNone(self.session.last_ai_frame_number)

        await self.feed_stable_video()
        self.assertEqual(len(self.adapter.payloads), 1)
        payload = self.adapter.payloads[0]
        with Image.open(BytesIO(base64.b64decode(payload['jpeg_base64']))) as image:
            self.assertLess(image.getpixel((35, 48))[0], 45)
            self.assertGreater(image.getpixel((52, 48))[0], 215)
        self.assertEqual(payload['mode'], 'scene')
        first_frame_number = self.session.last_ai_frame_number
        self.assertGreater(first_frame_number, 1)
        self.assertLessEqual(first_frame_number, self.session.video_frames)
        self.assertEqual(self.session.last_ai_frame_received_at, payload['image_captured_at'])
        first_result = self.session.latest_result['id']
        self.assertTrue(self.session.guide_active)

        same = np.asarray(edge_pattern(offset=15))
        await self.feed_until(same, lambda: self.session.pending_capture is not None)
        next_capture = self.session.pending_capture
        await self.feed_until(same, lambda: self.session.pending_capture is not next_capture)
        self.assertEqual(len(self.adapter.payloads), 1)
        self.assertEqual(self.session.latest_result['id'], first_result)

        changed = np.asarray(edge_pattern(offset=37))
        await self.feed_until(changed, lambda: len(self.adapter.payloads) >= 2)
        self.assertEqual(len(self.adapter.payloads), 2)
        self.assertGreater(self.session.last_ai_frame_number, first_frame_number)
        self.assertNotEqual(self.session.latest_result['id'], first_result)
        self.assertNotEqual(self.adapter.payloads[1]['turn_id'], payload['turn_id'])

        await self.manager.control(self.session, 'guide.stop', 'scene')
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), .5)
        self.assertFalse(self.session.guide_active)
        self.assertIsNone(self.session.pending_capture)
        await self.feed_stable_video()
        self.assertEqual(len(self.adapter.payloads), 2)

    async def test_scene_recovers_when_camera_becomes_steady(self):
        blurry = np.asarray(edge_pattern().filter(ImageFilter.GaussianBlur(radius=5)))
        with patch('senseplus.core.CAPTURE_TIMEOUT', .5):
            await self.manager.control(self.session, 'guide.start', 'scene')

            def waiting():
                return self.session.public_state(True)['scene_waiting_for_video']

            await self.feed_until(blurry, waiting)
            self.assertTrue(self.session.guide_active)
            self.assertFalse(self.session.paused)
            self.assertEqual(self.adapter.payloads, [])

            await self.feed_until(np.asarray(edge_pattern()), lambda: bool(self.adapter.payloads))
            self.assertTrue(self.session.guide_active)
            self.assertFalse(waiting())
            self.assertEqual(self.session.latest_result['source'], 'ai')

    async def test_scene_stops_after_video_receipts_go_stale(self):
        with patch('senseplus.core.CAPTURE_TIMEOUT', .08):
            await self.manager.control(self.session, 'guide.start', 'scene')
            self.session.last_video_at -= self.manager.config.frame_max_age + 1
            await asyncio.wait_for(self.session.task, 1)
        self.assertFalse(self.session.guide_active)
        self.assertTrue(self.session.paused)
        self.assertTrue(self.session.last_error)
        self.assertFalse(self.session.public_state(True)['scene_waiting_for_video'])
        self.assertEqual(self.adapter.payloads, [])

    async def test_find_and_read_keep_one_shot_behavior(self):
        for mode in ('find', 'read'):
            with self.subTest(mode=mode):
                await self.manager.control(self.session, 'guide.start', mode)
                await asyncio.sleep(0)
                await self.feed_stable_video()
                await asyncio.wait_for(self.session.task, .5)
                self.assertFalse(self.session.guide_active)
        self.assertEqual([payload['mode'] for payload in self.adapter.payloads], ['find', 'read'])

    async def test_scene_resumes_after_voice_question_without_another_start(self):
        frame = np.asarray(edge_pattern(offset=15))
        await self.manager.control(self.session, 'guide.start', 'scene')
        await self.feed_until(frame, lambda: bool(self.adapter.payloads))

        start_question = asyncio.create_task(self.manager.control(self.session, 'question.start', 'scene'))
        await self.feed_until(frame, start_question.done)
        question = await start_question
        self.assertTrue(question['question_recording'])
        self.assertFalse(question['guide_active'])
        self.manager.media_audio(self.session, INPUT_RATE, np.full(2400, 1000, dtype=np.int16))
        await self.manager.control(self.session, 'question.end', 'scene', question['current_turn_id'])
        await self.session.task
        self.assertTrue(self.session.guide_active)
        self.assertEqual(self.adapter.payloads[1]['turn_id'], question['current_turn_id'])
        self.assertIsNotNone(self.adapter.payloads[1]['audio_wav_base64'])

        changed = np.asarray(edge_pattern(offset=37))
        await self.feed_until(changed, lambda: len(self.adapter.payloads) == 3)
        self.assertIsNone(self.adapter.payloads[2]['audio_wav_base64'])

    async def test_guide_pauses_on_quality_timeout_without_sending_a_frame(self):
        with patch('senseplus.core.CAPTURE_TIMEOUT', 0.06):
            await self.manager.control(self.session, 'guide.start')
            await self.session.task
        self.assertEqual(self.adapter.payloads, [])
        self.assertIsNone(self.session.last_ai_frame_number)
        self.assertTrue(self.session.paused)
        self.assertFalse(self.session.guide_active)
        self.assertIn('선명한 영상', self.session.last_error)

    async def test_question_waits_to_start_recording_and_submits_the_frozen_frame(self):
        request = asyncio.create_task(self.manager.control(self.session, 'question.start', 'scene'))
        await asyncio.sleep(0)
        self.assertFalse(self.session.recording)
        self.assertIsNone(self.session.current_turn_id)
        self.manager.media_audio(self.session, INPUT_RATE, np.full(2400, 100, dtype=np.int16))
        self.assertEqual(self.session.question_samples, 0)
        await self.feed_stable_video()
        state = await request
        self.assertTrue(state['question_recording'])
        self.assertIsNone(state['last_ai_frame_number'])
        frozen_frame = self.session.question_frame_number
        self.assertGreater(frozen_frame, 1)

        self.manager.media_video(self.session, np.asarray(edge_pattern(offset=35)))
        self.manager.media_audio(self.session, INPUT_RATE, np.full(2400, 1000, dtype=np.int16))
        await self.manager.control(self.session, 'question.end', 'scene', state['current_turn_id'])
        await self.session.task
        self.assertEqual(len(self.adapter.payloads), 1)
        self.assertEqual(self.session.last_ai_frame_number, frozen_frame)
        self.assertEqual(self.session.question_samples, 2400)
        self.assertIsNone(self.session.question_jpeg)

    async def test_stop_invalidates_an_in_progress_question_selection(self):
        request = asyncio.create_task(self.manager.control(self.session, 'question.start', 'scene'))
        await asyncio.sleep(0)
        self.assertIsNotNone(self.session.pending_capture)
        await self.manager.control(self.session, 'guide.stop', 'scene')
        with self.assertRaises(SessionError) as cancelled:
            await asyncio.wait_for(request, .5)
        self.assertEqual(cancelled.exception.status_code, 409)
        self.assertIsNone(self.session.pending_capture)
        self.assertFalse(self.session.recording)
        self.assertIsNone(self.session.last_error)
