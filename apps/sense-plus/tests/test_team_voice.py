"""Team voice handshake against a mock server. No external API calls."""

import asyncio
from contextlib import suppress
import json
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from senseplus.adapter import AdapterError, OUTPUT_RATE, TeamAdapter, encode_wav, read_question_pcm, wav_bytes
from senseplus.config import Config
from senseplus.core import INPUT_RATE, SessionManager
from senseplus.server import create_app


TEAM_ORIGIN = 'https://team.invalid'
TRANSCRIPT = '헤이 센스, 의자가 어디에 있나요?'
QUESTION = '의자가 어디에 있나요?'
ANSWER = '화면 중앙에 의자가 보입니다.'
STARTUP = '센서 플러스가 실행되었습니다.'
TEST_KEY = 'mock-team-key'
PAIRING = 'A1B2C3D4E5F6'
REAL_CLIENT = httpx.AsyncClient
SPEECH_PATHS = {'/stt': {'post': {'operationId': 'stt_post'}}, '/tts': {'post': {'operationId': 'tts_post'}}}


def team_config():
    return Config(ai_url=TEAM_ORIGIN + '/analyze', ai_key=TEST_KEY, guide_interval=60)


class TeamVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.speech = np.full((1, 240), 1100, dtype=np.int16)
        self.encoded_speech = encode_wav(self.speech, OUTPUT_RATE)
        self.team_health = {'status': 'ok', 'voice_wake_available': True}
        self.team_routes = {}
        self.override = {}
        patcher = patch('senseplus.adapter.httpx.AsyncClient', side_effect=self.mock_client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mock_client(self, **kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(self.respond), **kwargs)

    def respond(self, request):
        self.assertEqual(str(request.url.copy_with(path='/')), TEAM_ORIGIN + '/')
        self.assertEqual(request.headers.get('authorization'), 'Bearer ' + TEST_KEY)
        path = request.url.path
        if request.method == 'GET':
            self.calls.append((path, None))
            if path == '/health':
                return httpx.Response(200, json=self.team_health)
            if path == '/openapi.json':
                return httpx.Response(200, json={'paths': self.team_routes})
            self.fail(f'Unexpected mock team GET path: {path}')

        self.assertEqual(request.method, 'POST')
        payload = json.loads(request.content)
        self.calls.append((path, payload))
        if path in self.override:
            return self.override[path]
        if path == '/stt':
            self.assertEqual(set(payload), {'audio_wav_base64'})
            return httpx.Response(200, json={'text': f' {TRANSCRIPT} '})
        if path == '/tts':
            self.assertEqual(set(payload), {'text'})
            return httpx.Response(200, json={'audio_wav_base64': self.encoded_speech})
        if path == '/analyze':
            return httpx.Response(200, json={'text': ANSWER, 'audio_wav_base64': self.encoded_speech})
        self.fail(f'Unexpected mock team path: {path}')

    def posts(self):
        return [(path, payload) for path, payload in self.calls if payload is not None]

    async def test_stt_and_tts_use_sibling_routes_and_pcm_contract(self):
        adapter = TeamAdapter(team_config())
        self.assertFalse(adapter.voice_wake_available)
        await adapter.refresh_voice_wake_available()
        self.assertTrue(adapter.voice_wake_available)
        pcm = np.tile(np.array([1000, -2000, 3000, -4000], dtype=np.int16), 400)

        self.assertEqual(await adapter.transcribe_voice(pcm), TRANSCRIPT)
        np.testing.assert_array_equal(await adapter.synthesize_voice(STARTUP), self.speech)

        speech_calls = self.posts()
        self.assertEqual(self.calls[0], ('/health', None))
        self.assertEqual([path for path, _ in speech_calls], ['/stt', '/tts'])
        np.testing.assert_array_equal(read_question_pcm(speech_calls[0][1]['audio_wav_base64']), pcm)
        self.assertEqual(speech_calls[1][1]['text'], STARTUP)

    async def test_legacy_health_uses_openapi_to_confirm_both_post_routes(self):
        self.team_health = {'status': 'ok'}
        self.team_routes = SPEECH_PATHS
        adapter = TeamAdapter(team_config())
        await adapter.refresh_voice_wake_available()
        self.assertTrue(adapter.voice_wake_available)
        self.assertEqual(self.calls, [('/health', None), ('/openapi.json', None)])

    async def test_explicitly_disabled_team_voice_does_not_inspect_or_call_speech(self):
        self.team_health = {'status': 'ok', 'voice_wake_available': False}
        self.team_routes = SPEECH_PATHS
        adapter = TeamAdapter(team_config())
        await adapter.refresh_voice_wake_available()
        self.assertFalse(adapter.voice_wake_available)
        self.assertEqual(self.calls, [('/health', None)])

    async def test_old_analyze_configuration_keeps_wake_disabled(self):
        self.team_health = {'status': 'ok'}
        self.team_routes = {'/stt': SPEECH_PATHS['/stt'], '/tts': {'get': {'operationId': 'tts_get'}}}
        adapter = TeamAdapter(team_config())
        await adapter.refresh_voice_wake_available()
        self.assertFalse(adapter.voice_wake_available)
        with self.assertRaises(AdapterError):
            await adapter.transcribe_voice(np.ones(INPUT_RATE, dtype=np.int16))
        with self.assertRaises(AdapterError):
            await adapter.synthesize_voice(STARTUP)
        self.assertEqual(self.posts(), [])
        self.assertEqual((await adapter.analyze({'jpeg_base64': 'mock-still'})).text, ANSWER)
        self.assertEqual([path for path, _ in self.posts()], ['/analyze'])

    async def test_rejects_bad_speech_response_and_does_not_follow_redirect(self):
        adapter = TeamAdapter(team_config())
        await adapter.refresh_voice_wake_available()
        self.override['/stt'] = httpx.Response(307, headers={'Location': 'https://other.invalid/stt'})
        self.override['/tts'] = httpx.Response(200, json={'audio_wav_base64': 'invalid WAV'})
        with self.assertRaises(AdapterError):
            await adapter.transcribe_voice(np.ones(INPUT_RATE, dtype=np.int16))
        with self.assertRaises(AdapterError):
            await adapter.synthesize_voice(STARTUP)
        self.assertEqual([path for path, _ in self.posts()], ['/stt', '/tts'])

    async def test_paired_voice_start_and_wake_question_use_mock_team_models(self):
        manager = SessionManager(team_config())
        manager.pairing_code = PAIRING
        self.addAsyncCleanup(manager.close_all)
        app = create_app(manager=manager)
        async with REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url='https://sense.test') as client:
            paired = await client.post('/api/sessions', headers={'X-Sense-Pairing': PAIRING, 'X-Sense-Answer-Audio': 'http'})
            paired.raise_for_status()
            created = paired.json()
            session = manager.sessions[created['session_id']]
            url = f"/api/sessions/{created['session_id']}"
            headers = {'Authorization': f"Bearer {created['token']}"}

            async def control(action, turn_id=None):
                result = await client.post(url + '/control', headers=headers, json={
                    'action': action, 'mode': 'scene', 'turn_id': turn_id,
                })
                result.raise_for_status()
                return result.json()

            async def await_state(predicate):
                async with asyncio.timeout(3):
                    while True:
                        result = await client.get(url + '/state', headers=headers)
                        result.raise_for_status()
                        state = result.json()
                        if predicate(state):
                            return state
                        await asyncio.sleep(.025)

            frame = np.zeros((96, 128, 3), dtype=np.uint8)
            frame[:, 30:60] = 235
            manager.media_video(session, frame)

            async def keep_video_live():
                while True:
                    await asyncio.sleep(.07)
                    manager.media_video(session, frame)

            video = asyncio.create_task(keep_video_live())
            try:
                self.assertEqual(self.calls, [])
                self.assertTrue((await control('voice.enable'))['voice_wake_enabled'])
                startup = (await await_state(lambda state: (state['latest_result'] or {}).get('source') == 'startup'))['latest_result']
                self.assertEqual(self.calls[0], ('/health', None))
                self.assertEqual(startup['text'], STARTUP)
                self.assertTrue(startup['audio_available'])
                self.assertEqual(startup['audio_source'], 'http-wav')
                self.assertEqual(startup['audio_provider'], 'team-api')
                self.assertEqual(self.posts(), [('/tts', {'text': STARTUP})])
                health = (await client.get('/api/health')).json()
                self.assertEqual(health['voice_wake_available'], health['rtc_available'])
                self.assertEqual(health['voice_wake_provider'], 'team-api' if health['rtc_available'] else None)
                audio = await client.get(url + f"/audio/{startup['id']}.wav", headers=headers)
                audio.raise_for_status()
                self.assertEqual(audio.content, wav_bytes(self.speech, OUTPUT_RATE))

                await control('voice.playback.start', startup['id'])
                await control('voice.playback.end', startup['id'])
                await await_state(lambda item: item['voice_wake_phase'] == 'waiting')
                sound = np.tile(np.array([1400, -1400], dtype=np.int16), 160)
                silence = np.zeros_like(sound)
                for samples in [sound] * 38 + [silence] * 60:
                    manager.media_audio(session, INPUT_RATE, samples)

                voice_reply = await await_state(lambda state: (state['latest_result'] or {}).get('text') == ANSWER
                                                and not state['latest_result'].get('scene_observation'))
                self.assertEqual(voice_reply['latest_result']['audio_provider'], 'team-api')
                question_calls = [payload for path, payload in self.calls if path == '/analyze' and payload.get('question_text')]
                self.assertEqual(len(question_calls), 1)
                self.assertEqual(question_calls[0]['question_text'], QUESTION)
                self.assertIsNone(question_calls[0].get('audio_wav_base64'))
                self.assertNotIn(QUESTION, str(manager.events))
                self.assertEqual(sum(path == '/stt' for path, _ in self.calls), 1)
            finally:
                video.cancel()
                with suppress(asyncio.CancelledError):
                    await video
