import unittest

import numpy as np

from senseplus.wake import SAMPLE_RATE, WINDOW_SAMPLES, VoiceActivity, split_wake_phrase


def quiet(windows, offset=0):
    return np.full(windows * WINDOW_SAMPLES, offset, dtype=np.int16)


def voice(windows, amplitude=900):
    period = np.array([amplitude, -amplitude], dtype=np.int16)
    return np.tile(period, windows * WINDOW_SAMPLES // len(period))


class WakePhraseTests(unittest.TestCase):
    def test_english_korean_and_narrow_transcription_aliases(self):
        for text, expected in (
            ("Hey, SENSE", ""),
            ("헤이 센스.", ""),
            ("헤이센스, 문이 어디 있어?", "문이 어디 있어?"),
            ("헤이 쎈스: 앞을 읽어 줘.", "앞을 읽어 줘."),
            ("헤이 센쓰 앞에 뭐 있어?", "앞에 뭐 있어?"),
            ("Ｈｅｙ　Ｓｅｎｓｅ！ What’s ahead?", "What’s ahead?"),
            ('"Hey Sense"', ""),
        ):
            with self.subTest(text=text):
                self.assertEqual(split_wake_phrase(text), expected)

    def test_requires_start_and_exact_word_boundaries(self):
        for text in ("", "센스", "좀 헤이 센스", "I said hey sense", "Hey Senseless", "헤이 센스야", "Hey sensor"):
            with self.subTest(text=text):
                self.assertIsNone(split_wake_phrase(text))


class VoiceActivityTests(unittest.TestCase):
    def test_chunks_preserve_preroll_question_and_trim_trailing_quiet(self):
        vad = VoiceActivity()
        history = np.concatenate([quiet(1, offset) for offset in range(20)])
        spoken = voice(15)
        audio = np.concatenate((history, spoken, quiet(30)))
        results = []
        for offset in range(0, audio.size, 173):
            utterance = vad.push(audio[offset:offset + 173])
            if utterance is not None:
                results.append(utterance)
        self.assertEqual(len(results), 1)
        expected = np.concatenate((history[-12 * WINDOW_SAMPLES:], spoken, quiet(6)))
        np.testing.assert_array_equal(results[0], expected)
        self.assertEqual(results[0].dtype, np.int16)

    def test_pulse_low_energy_and_constant_offset_do_not_emit(self):
        vad = VoiceActivity()
        for chunk in (quiet(10, 3000), voice(20, 250), quiet(33), voice(1), quiet(30), voice(4), quiet(30)):
            self.assertIsNone(vad.push(chunk))

    def test_sustained_signal_is_bounded_and_requires_quiet_before_rearming(self):
        vad = VoiceActivity(max_seconds=0.5, end_silence_ms=200)
        utterance = vad.push(np.concatenate((quiet(12), voice(200))))
        self.assertIsNotNone(utterance)
        self.assertLessEqual(utterance.size, SAMPLE_RATE // 2)
        self.assertIsNone(vad.push(voice(200)))
        self.assertIsNone(vad.push(quiet(10)))
        self.assertIsNotNone(vad.push(np.concatenate((voice(10), quiet(10)))))

    def test_reset_releases_pending_audio_and_accepts_single_channel_arrays(self):
        vad = VoiceActivity(end_silence_ms=200)
        self.assertIsNone(vad.push(voice(10)))
        vad.reset()
        self.assertIsNone(vad.push(quiet(30)))
        self.assertIsNotNone(vad.push(np.concatenate((voice(10), quiet(10))).reshape(1, -1)))
        with self.assertRaises(ValueError):
            vad.push(np.zeros((2, WINDOW_SAMPLES), dtype=np.int16))
        with self.assertRaises(ValueError):
            vad.push(np.zeros(WINDOW_SAMPLES, dtype=np.float32))
