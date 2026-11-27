"""Opt-in offline speech recognition and synthesis for SENSE+."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Protocol

import numpy as np


INPUT_RATE = 16000
MAX_INPUT_SAMPLES = 20 * INPUT_RATE
MODEL_FILES = ('model.bin', 'config.json', 'tokenizer.json')


class SpeechError(Exception):
    """Speech error safe to show without including input text or audio."""


class SpeechToText(Protocol):
    async def transcribe(self, pcm: np.ndarray) -> str:
        """Transcribe a mono 16 kHz int16 question."""


class TextToSpeech(Protocol):
    async def synthesize(self, text: str) -> np.ndarray:
        """Synthesize a mono 24 kHz int16 playback buffer."""


class FasterWhisperSTT:
    def __init__(self, model_path: str):
        try:
            if model_path.startswith(('\\\\', '//')):
                raise ValueError('Network path')
            self.model_path = Path(model_path).expanduser().resolve()
            if str(self.model_path).startswith(('\\\\', '//')) or not self.model_path.is_dir():
                raise ValueError('Not a local directory')
            complete = all((self.model_path / name).is_file() for name in MODEL_FILES)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SpeechError('음성 인식 모델의 로컬 폴더를 확인해 주세요.') from exc
        if not complete:
            raise SpeechError('음성 인식 모델 폴더에 model.bin, config.json, tokenizer.json 파일이 필요합니다.')
        self._model = None
        self._lock = threading.Lock()

    async def transcribe(self, pcm: np.ndarray) -> str:
        data = np.asarray(pcm)
        if data.ndim == 2 and 1 in data.shape:
            data = data.reshape(-1)
        if (data.ndim != 1 or data.dtype.kind != 'i' or data.dtype.itemsize != 2
                or not 0 < data.size <= MAX_INPUT_SAMPLES):
            raise SpeechError('16kHz 모노 16비트 음성 데이터를 확인해 주세요.')
        if not np.any(data):
            raise SpeechError('질문 음성을 듣지 못했습니다. 마이크를 확인해 주세요.')

        waveform = data.astype(np.float32) / 32768.0
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(self._transcribe, waveform, cancelled)
        except asyncio.CancelledError:
            # A native inference call may finish before the thread can stop.
            # Discard its output, and keep access to the cached model serialized.
            cancelled.set()
            raise

    def _transcribe(self, waveform: np.ndarray, cancelled: threading.Event) -> str:
        with self._lock:
            if cancelled.is_set():
                return ''
            try:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    # A deployed model can be removed after configuration is read.
                    # Do not let a missing tokenizer trigger an upstream lookup.
                    if not (self.model_path / 'tokenizer.json').is_file():
                        raise SpeechError('로컬 음성 인식 모델에 tokenizer.json 파일이 없습니다.')
                    model = WhisperModel(str(self.model_path), device='cpu', compute_type='int8', local_files_only=True)
                    if not model.model.is_multilingual or model.feature_extractor.sampling_rate != INPUT_RATE:
                        raise SpeechError('한국어를 지원하는 16kHz Whisper 모델을 설정해 주세요.')
                    self._model = model
                if cancelled.is_set():
                    return ''
                segments, _ = self._model.transcribe(waveform, language='ko', task='transcribe', beam_size=1,
                                                     vad_filter=True, condition_on_previous_text=False)
                parts = []
                for segment in segments:
                    if cancelled.is_set():
                        return ''
                    parts.append(segment.text)
            except SpeechError:
                raise
            except ImportError as exc:
                raise SpeechError('로컬 음성 인식 런타임을 불러오지 못했습니다.') from exc
            except Exception as exc:
                raise SpeechError('로컬 음성 인식에 실패했습니다. 모델을 확인해 주세요.') from exc

        transcript = ' '.join(' '.join(parts).split())
        if not transcript:
            raise SpeechError('질문 음성을 알아듣지 못했습니다. 다시 말씀해 주세요.')
        return transcript


_WINDOWS_SPEECH_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Speech
$inputStream = [IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
$request = ConvertFrom-Json -InputObject $inputStream.ReadToEnd()
$synthesizer = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$memory = [IO.MemoryStream]::new()
try {
    $synthesizer.SelectVoice($request.voice)
    $synthesizer.SetOutputToWaveStream($memory)
    $synthesizer.Speak([string]$request.text)
    $synthesizer.SetOutputToNull()
    $wave = $memory.ToArray()
    [Console]::OpenStandardOutput().Write($wave, 0, $wave.Length)
} finally {
    $memory.Dispose()
    $synthesizer.Dispose()
    $inputStream.Dispose()
}
'''
_WINDOWS_SPEECH_COMMAND = base64.b64encode(_WINDOWS_SPEECH_SCRIPT.encode('utf-16-le')).decode('ascii')


class WindowsSpeechTTS:
    def __init__(self, voice: str):
        self.voice = voice.strip()
        self._powershell = (Path(os.environ.get('SystemRoot', 'C:/Windows'))
                            / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
        if os.name != 'nt' or not self._powershell.is_file() or not self.voice:
            raise SpeechError('Windows 로컬 음성 합성에 사용할 음성을 확인해 주세요.')

    async def synthesize(self, text: str) -> np.ndarray:
        from .adapter import AdapterError, MAX_WAV_BYTES, decode_wav

        if not text.strip():
            raise SpeechError('읽을 응답이 없습니다.')
        request = json.dumps({'voice': self.voice, 'text': text}, ensure_ascii=False).encode('utf-8')
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
                '-EncodedCommand', _WINDOWS_SPEECH_COMMAND,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                wav, _ = await process.communicate(request)
            except asyncio.CancelledError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
                raise
        except (OSError, NotImplementedError) as exc:
            raise SpeechError('Windows 음성 합성 프로세스를 실행하지 못했습니다.') from exc
        if process.returncode or not wav or len(wav) > MAX_WAV_BYTES:
            raise SpeechError('Windows 음성 합성이 실패했습니다. 선택한 음성과 응답 길이를 확인해 주세요.')
        try:
            return decode_wav(base64.b64encode(wav).decode('ascii'))
        except AdapterError as exc:
            raise SpeechError('합성한 음성을 재생 규격으로 변환하지 못했습니다.') from exc


def create_speech_providers(stt_model_path: str, tts_voice: str) -> tuple[SpeechToText | None, TextToSpeech | None]:
    stt = FasterWhisperSTT(stt_model_path.strip()) if stt_model_path.strip() else None
    tts = WindowsSpeechTTS(tts_voice) if tts_voice.strip() else None
    return stt, tts
