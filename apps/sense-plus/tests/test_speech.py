import asyncio
import base64
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from senseplus.adapter import encode_wav
from senseplus.speech import FasterWhisperSTT, SpeechError, WindowsSpeechTTS, create_speech_providers


class WhisperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.model_path = Path(directory.name)
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (self.model_path / name).touch()

    async def test_offline_model_is_reused_and_receives_normalized_korean_audio(self):
        model = Mock()
        model.model.is_multilingual = True
        model.feature_extractor.sampling_rate = 16000
        model.transcribe.return_value = ([SimpleNamespace(text='  불을\n '), SimpleNamespace(text=' 켜 줘 ')], None)
        runtime = SimpleNamespace(WhisperModel=Mock(return_value=model))
        pcm = np.array([[-32768, 0, 16384]], dtype=np.int16)

        with patch.dict(sys.modules, {'faster_whisper': runtime}):
            speech, _ = create_speech_providers(str(self.model_path), '')
            self.assertEqual(await speech.transcribe(pcm), '불을 켜 줘')
            self.assertEqual(await speech.transcribe(pcm), '불을 켜 줘')

        runtime.WhisperModel.assert_called_once_with(
            str(self.model_path.resolve()), device='cpu', compute_type='int8', local_files_only=True,
        )
        self.assertEqual(model.transcribe.call_count, 2)
        values, options = model.transcribe.call_args
        self.assertEqual(values[0].dtype, np.float32)
        np.testing.assert_array_equal(values[0], [-1.0, 0.0, 0.5])
        self.assertEqual(options['language'], 'ko')
        self.assertTrue(options['vad_filter'])
        self.assertEqual(pcm[0, -1], 16384)

    async def test_model_files_and_audio_are_checked_before_inference(self):
        speech = FasterWhisperSTT(str(self.model_path))
        with self.assertRaises(SpeechError):
            await speech.transcribe(np.ones((2, 200), dtype=np.int16))
        with self.assertRaises(SpeechError):
            await speech.transcribe(np.zeros(1600, dtype=np.int16))
        (self.model_path / 'tokenizer.json').unlink()
        with self.assertRaisesRegex(SpeechError, 'tokenizer.json'):
            FasterWhisperSTT(str(self.model_path))
        runtime = SimpleNamespace(WhisperModel=Mock())
        with patch.dict(sys.modules, {'faster_whisper': runtime}):
            with self.assertRaisesRegex(SpeechError, 'tokenizer.json'):
                await speech.transcribe(np.ones(1600, dtype=np.int16))
        runtime.WhisperModel.assert_not_called()
        self.assertEqual(create_speech_providers('', ''), (None, None))

    async def test_cancelling_native_inference_discards_transcript(self):
        started = threading.Event()
        release = threading.Event()

        def delayed_transcribe(*args, **kwargs):
            started.set()
            if not release.wait(2):
                raise AssertionError('Test did not release inference')
            return [SimpleNamespace(text='늦은 전사')], None

        model = SimpleNamespace(model=SimpleNamespace(is_multilingual=True),
                                feature_extractor=SimpleNamespace(sampling_rate=16000),
                                transcribe=delayed_transcribe)
        runtime = SimpleNamespace(WhisperModel=Mock(return_value=model))
        speech = FasterWhisperSTT(str(self.model_path))
        with patch.dict(sys.modules, {'faster_whisper': runtime}):
            task = asyncio.create_task(speech.transcribe(np.ones(1600, dtype=np.int16)))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()


class WindowsSpeechTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Test the subprocess protocol on all platforms with an injected process.
        self.speech = WindowsSpeechTTS.__new__(WindowsSpeechTTS)
        self.speech.voice = 'Microsoft Heami Desktop'
        self.speech._powershell = Path('powershell-placeholder')

    async def test_sends_text_via_utf8_stdin_and_normalizes_pcm_output(self):
        wav = base64.b64decode(encode_wav(np.full(1600, 4096, dtype=np.int16), 16000))
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(wav, None)))
        sentence = '“안녕하세요.”\n책을 읽어 주세요.'

        with patch('senseplus.speech.subprocess.CREATE_NO_WINDOW', 0, create=True), \
             patch('senseplus.speech.asyncio.create_subprocess_exec', return_value=process) as launch:
            output = await self.speech.synthesize(sentence)

        self.assertNotIn(sentence, ' '.join(launch.call_args.args))
        request = json.loads(process.communicate.call_args.args[0])
        self.assertEqual(request, {'voice': self.speech.voice, 'text': sentence})
        self.assertEqual(output.shape, (1, 2400))
        self.assertEqual(output.dtype, np.int16)

    async def test_terminates_synthesis_process_on_cancel(self):
        started = asyncio.Event()

        async def communicate(_):
            started.set()
            await asyncio.Future()

        process = SimpleNamespace(returncode=None, communicate=communicate,
                                  kill=Mock(), wait=AsyncMock())
        with patch('senseplus.speech.subprocess.CREATE_NO_WINDOW', 0, create=True), \
             patch('senseplus.speech.asyncio.create_subprocess_exec', return_value=process):
            task = asyncio.create_task(self.speech.synthesize('테스트 음성'))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
