import io
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from worker import voice_trim
from worker import comfy_client


def timed_words():
    return [SimpleNamespace(word=word, start=i * .4, end=i * .4 + .3)
            for i, word in enumerate("Hi I'm Avery It's really nice to meet you Soft morning light moved across the quiet room".split())]


class VoiceTrimTests(unittest.TestCase):
    def test_boundary_preserves_passage_and_removes_whole_greeting(self):
        self.assertAlmostEqual(voice_trim.reference_start(timed_words()), 3.56)

    def test_uncertain_boundary_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "confidently"):
            voice_trim.reference_start(timed_words()[:9])
        with self.assertRaisesRegex(RuntimeError, "confidently"):
            voice_trim.reference_start(timed_words() + timed_words())

    def test_overlap_is_rejected(self):
        words = timed_words()
        words[8].end = 4
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            voice_trim.reference_start(words)

    def test_promotion_uses_chosen_output_and_never_synthesizes(self):
        response = Mock(content=b"chosen recording")
        with patch.object(comfy_client.requests, "get", return_value=response) as fetch, \
             patch.object(voice_trim, "trim_candidate", return_value=b"trimmed recording") as trim, \
             patch.object(comfy_client, "upload_bytes_to_input", return_value="reference.wav") as upload, \
             patch.object(comfy_client, "submit_prompt") as submit:
            self.assertEqual(comfy_client.promote_selected_voice("chosen.flac", "audio"), "reference.wav")
        self.assertEqual(fetch.call_args.kwargs["params"], {
            "filename": "chosen.flac", "subfolder": "audio", "type": "output"})
        trim.assert_called_once_with(b"chosen recording")
        self.assertEqual(upload.call_args.args[0], b"trimmed recording")
        submit.assert_not_called()

    def test_failed_trim_does_not_upload_a_replacement(self):
        with patch.object(comfy_client.requests, "get", return_value=Mock(content=b"audio")), \
             patch.object(voice_trim, "trim_candidate", side_effect=RuntimeError("uncertain")), \
             patch.object(comfy_client, "upload_bytes_to_input") as upload:
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                comfy_client.promote_selected_voice("chosen.flac")
        upload.assert_not_called()

    def test_real_ffmpeg_retains_exact_pcm_samples(self):
        try:
            voice_trim.resolve_ffmpeg()
        except RuntimeError:
            self.skipTest("FFmpeg unavailable")
        samples = [i % 30000 for i in range(8000 * 9)]
        source = io.BytesIO()
        with wave.open(source, "wb") as audio:
            audio.setparams((1, 4, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(struct.pack("<" + "i" * len(samples), *samples))
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(words=timed_words())], None)
        module = SimpleNamespace(WhisperModel=Mock(return_value=model))
        with patch.dict(sys.modules, {"faster_whisper": module}):
            result = voice_trim.trim_candidate(source.getvalue())
        with wave.open(io.BytesIO(result), "rb") as audio:
            retained = audio.readframes(8000 * 9)
        self.assertEqual(retained, struct.pack("<" + "i" * (len(samples) - 28480), *samples[28480:]))

    def test_configured_ffmpeg_is_preserved(self):
        with patch.object(voice_trim.shutil, "which", return_value="/existing/ffmpeg"):
            self.assertEqual(voice_trim.resolve_ffmpeg(), "/existing/ffmpeg")

    def test_bundled_ffmpeg_fallback(self):
        module = SimpleNamespace(get_ffmpeg_exe=Mock(return_value="/bundled/ffmpeg"))
        with patch.object(voice_trim.shutil, "which", return_value=None), \
             patch.dict(sys.modules, {"imageio_ffmpeg": module}), \
             patch.object(voice_trim.Path, "is_file", return_value=True):
            self.assertEqual(voice_trim.resolve_ffmpeg(), "/bundled/ffmpeg")

    def test_prepare_checks_binary_and_loads_model(self):
        with patch.object(voice_trim, "resolve_ffmpeg", return_value="/bundled/ffmpeg"), \
             patch.object(voice_trim.subprocess, "run") as run, \
             patch.object(voice_trim, "load_recognizer") as load:
            voice_trim.prepare()
        self.assertEqual(run.call_args.args[0], ["/bundled/ffmpeg", "-version"])
        load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
