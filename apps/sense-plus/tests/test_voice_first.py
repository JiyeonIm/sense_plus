"""Paired voice control, synthetic microphone packets and authenticated answer WAVs."""

import asyncio
from collections import deque
from contextlib import suppress
import io
import unittest
import wave

import httpx
import numpy as np

from senseplus.adapter import AnalysisResult, LocalAdapter, OUTPUT_RATE
from senseplus.config import Config
from senseplus.core import INPUT_RATE, SessionManager
from senseplus.server import create_app


PAIRING = 'voice-first-test-pairing'
WAKE = '헤이 센스'
QUESTION = '빨간 원이 어디에 있나요?'
REPLY = '화면 왼쪽에 빨간 원이 보입니다.'


class VoiceAdapter(LocalAdapter):
    def __init__(self):
        self.transcripts = deque()
        self.transcribed = []
        self.spoken = []
        self.analyzed = []
        self.transcribing = asyncio.Event()
        self.block_transcription = None
        self.late_transcription = asyncio.Event()

    @property
    def voice_wake_available(self):
        return True

    async def transcribe_voice(self, pcm):
        self.transcribed.append(pcm.size)
        self.transcribing.set()
        result = self.transcripts.popleft() if self.transcripts else '오늘은 여기에 의자가 있습니다.'
        if self.block_transcription is not None:
            try:
                await self.block_transcription.wait()
            except asyncio.CancelledError:
                # A native recognizer can finish late even after cancellation.
                self.late_transcription.set()
        return result

    async def synthesize_voice(self, text):
        self.spoken.append(text)
        return np.full((1, 240), 1600, dtype=np.int16)

    async def analyze(self, payload):
        self.analyzed.append(payload)
        if 'question_text' in payload:
            audio = await self.synthesize_voice(REPLY)
            return AnalysisResult(REPLY, audio=audio, audio_provider='test-tts')
        return AnalysisResult('카메라에 주변이 보입니다.')


class VoiceFirstTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = VoiceAdapter()
        self.manager = SessionManager(Config(guide_interval=60, audio_tail_ms=0), self.adapter)
        self.manager.pairing_code = PAIRING
        app = create_app(manager=self.manager)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://sense.test')
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.manager.close_all)

        response = await self.client.post('/api/sessions', headers={
            'X-Sense-Pairing': PAIRING,
            'X-Sense-Answer-Audio': 'http',
        })
        self.assertEqual(response.status_code, 201, response.text)
        session = response.json()
        self.session = self.manager.sessions[session['session_id']]
        self.url = f"/api/sessions/{session['session_id']}"
        self.headers = {'Authorization': f"Bearer {session['token']}"}

        frame = np.full((96, 128, 3), 28, dtype=np.uint8)
        frame[:, 30:50] = 230
        frame[20:50, 60:90] = (230, 30, 30)
        self.manager.media_video(self.session, frame)
        self.video = asyncio.create_task(self.feed_video(frame))
        self.addAsyncCleanup(self.stop_video)

    async def feed_video(self, frame):
        while True:
            await asyncio.sleep(.07)
            self.manager.media_video(self.session, frame)

    async def stop_video(self):
        self.video.cancel()
        with suppress(asyncio.CancelledError):
            await self.video

    async def state(self):
        response = await self.client.get(self.url + '/state', headers=self.headers)
        response.raise_for_status()
        return response.json()

    async def control(self, action, turn_id=None):
        payload = {'action': action, 'mode': 'scene'}
        if turn_id is not None:
            payload['turn_id'] = turn_id
        response = await self.client.post(self.url + '/control', headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def until_state(self, predicate, timeout=3):
        async with asyncio.timeout(timeout):
            while True:
                state = await self.state()
                if predicate(state):
                    return state
                await asyncio.sleep(.02)

    def send_segment(self):
        """A short voiced waveform then silence at 20 ms RTC packet size."""
        count = INPUT_RATE // 50
        wave_values = (4500 * np.sin(np.arange(count) * 2 * np.pi * 200 / INPUT_RATE)).astype(np.int16)
        silence = np.zeros(count, dtype=np.int16)
        for _ in range(38):
            self.manager.media_audio(self.session, INPUT_RATE, wave_values)
        for _ in range(60):
            self.manager.media_audio(self.session, INPUT_RATE, silence)

    async def enable_and_wait_for_announcement(self):
        state = await self.control('voice.enable')
        self.assertTrue(state['voice_wake_enabled'])
        self.assertEqual(state['mode'], 'scene')
        announcement = await self.until_state(lambda item: (item['latest_result'] or {}).get('source') == 'startup')
        result = announcement['latest_result']
        self.assertTrue(result['audio_available'])
        self.assertIn(result['text'], self.adapter.spoken)
        return result

    async def finish_playback(self, result):
        await self.control('voice.playback.start', result['id'])
        return await self.end_playback(result)

    async def end_playback(self, result):
        state = await self.control('voice.playback.end', result['id'])
        await asyncio.sleep(.30)  # Allow audible output to clear the short echo guard.
        return state

    def voice_analysis(self):
        return [payload for payload in self.adapter.analyzed if 'question_text' in payload]

    async def test_pairing_and_opt_in_keep_unsolicited_background_audio_off(self):
        health = await self.client.get('/api/health')
        self.assertEqual(health.json()['voice_wake_available'], health.json()['rtc_available'])
        self.assertEqual((await self.client.post('/api/sessions')).status_code, 403)
        unauthed = await self.client.post(self.url + '/control', json={'action': 'voice.enable'})
        self.assertEqual(unauthed.status_code, 401)
        state = await self.state()
        self.assertFalse(state['voice_wake_enabled'])
        self.assertEqual(state['voice_wake_phase'], 'off')
        self.adapter.transcripts.append(f'{WAKE} {QUESTION}')
        self.send_segment()
        await asyncio.sleep(.05)
        self.assertEqual(self.adapter.transcribed, [])
        self.assertEqual(self.voice_analysis(), [])

    async def test_remote_analysis_configuration_does_not_offer_wake_recognition(self):
        app = create_app(config=Config(ai_url='https://external.example.test/analyze'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://sense.test') as client:
            response = await client.get('/api/health')
        self.assertTrue(response.json()['ai_configured'])
        self.assertFalse(response.json()['voice_wake_available'])

    async def test_ordinary_speech_with_wake_words_mid_sentence_does_not_trigger(self):
        announcement = await self.enable_and_wait_for_announcement()
        await self.finish_playback(announcement)
        self.adapter.transcripts.append(f'팀원에게 {WAKE}라고 말하는 법을 설명했습니다.')
        self.send_segment()
        await asyncio.wait_for(self.adapter.transcribing.wait(), 2)
        await self.until_state(lambda item: item['voice_wake_phase'] == 'waiting')
        self.assertEqual(self.voice_analysis(), [])
        self.assertEqual(len(self.adapter.transcribed), 1)

    async def test_wake_with_question_produces_authenticated_answer_wav(self):
        announcement = await self.enable_and_wait_for_announcement()
        await self.finish_playback(announcement)
        await self.until_state(lambda item: item['guide_active'] and item['voice_wake_phase'] == 'waiting')
        self.adapter.transcripts.append(f'{WAKE}, {QUESTION}')
        self.send_segment()

        state = await self.until_state(lambda item: (item['latest_result'] or {}).get('text') == REPLY)
        result = state['latest_result']
        self.assertEqual(len(self.voice_analysis()), 1)
        self.assertEqual(self.voice_analysis()[0]['question_text'], QUESTION)
        self.assertIsNone(self.voice_analysis()[0].get('audio_wav_base64'))
        self.assertEqual(result['audio_source'], 'http-wav')
        self.assertNotIn(QUESTION, str(self.manager.events))
        path = self.url + f"/audio/{result['id']}.wav"
        self.assertEqual((await self.client.get(path)).status_code, 401)
        wav_response = await self.client.get(path, headers=self.headers)
        self.assertEqual(wav_response.status_code, 200, wav_response.text)
        with wave.open(io.BytesIO(wav_response.content), 'rb') as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, OUTPUT_RATE))
            self.assertGreater(audio.getnframes(), 0)
        await self.finish_playback(result)
        await self.until_state(lambda item: item['guide_active'] and item['voice_wake_phase'] == 'waiting')

    async def test_wake_alone_arms_followup_question(self):
        announcement = await self.enable_and_wait_for_announcement()
        await self.finish_playback(announcement)
        self.adapter.transcripts.append(WAKE)
        self.send_segment()

        await self.until_state(lambda item: item['voice_wake_phase'] == 'question')
        self.assertEqual(self.voice_analysis(), [])
        self.adapter.transcripts.append(QUESTION)
        self.send_segment()
        await self.until_state(lambda item: (item['latest_result'] or {}).get('text') == REPLY)
        self.assertEqual(self.voice_analysis()[0]['question_text'], QUESTION)
        self.assertEqual(len(self.adapter.transcribed), 2)

    async def test_server_ignores_microphone_feedback_during_playback(self):
        announcement = await self.enable_and_wait_for_announcement()
        await self.control('voice.playback.start', announcement['id'])
        await self.control('voice.playback.end', 'previous-result')
        self.adapter.transcripts.append(f'{WAKE} {QUESTION}')
        self.send_segment()
        await asyncio.sleep(.05)
        self.assertEqual(self.adapter.transcribed, [])
        self.assertEqual((await self.state())['voice_wake_phase'], 'playing')

        await self.end_playback(announcement)
        self.send_segment()
        await self.until_state(lambda item: (item['latest_result'] or {}).get('text') == REPLY)
        self.assertEqual(len(self.adapter.transcribed), 1)

    async def test_disabling_cancels_a_late_recognizer_result(self):
        announcement = await self.enable_and_wait_for_announcement()
        await self.finish_playback(announcement)
        self.adapter.block_transcription = asyncio.Event()
        self.adapter.transcripts.append(f'{WAKE} {QUESTION}')
        self.send_segment()
        await asyncio.wait_for(self.adapter.transcribing.wait(), 2)
        after_disable = await self.control('voice.disable')
        self.assertFalse(after_disable['voice_wake_enabled'])
        self.assertEqual(after_disable['voice_wake_phase'], 'off')
        await asyncio.wait_for(self.adapter.late_transcription.wait(), 2)
        self.send_segment()
        await asyncio.sleep(.05)
        self.assertEqual(len(self.adapter.transcribed), 1)
        self.assertEqual(self.voice_analysis(), [])

    async def test_rtc_shutdown_clears_voice_recognition(self):
        try:
            from senseplus.rtc import SenseHandler
        except ImportError:
            self.skipTest('FastRTC is unavailable')
        handler = SenseHandler(self.manager)
        handler.bind(self.session.session_id)
        announcement = await self.enable_and_wait_for_announcement()
        await self.finish_playback(announcement)
        self.adapter.block_transcription = asyncio.Event()
        self.adapter.transcripts.append(f'{WAKE} {QUESTION}')
        self.send_segment()
        await asyncio.wait_for(self.adapter.transcribing.wait(), 2)

        await self.stop_video()
        await handler.shutdown()
        await asyncio.wait_for(self.adapter.late_transcription.wait(), 2)
        state = await self.state()
        self.assertEqual(state['transport'], 'disconnected')
        self.assertFalse(state['voice_wake_enabled'])
        self.assertEqual(state['voice_wake_phase'], 'off')
        await handler.receive((INPUT_RATE, np.full(320, 4000, dtype=np.int16)))
        self.assertEqual(len(self.adapter.transcribed), 1)
        self.assertEqual(self.voice_analysis(), [])
