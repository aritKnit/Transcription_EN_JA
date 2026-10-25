import json
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from whisper_live.backend import sensevoice_backend as sv
from whisper_live.backend.sensevoice_backend import SenseVoiceSegment, ServeClientSenseVoice
from whisper_live.server import BackendType


def make_client(language="ja", model=None, **kwargs):
    """A client whose shared model is a mock and whose transcription thread is not started."""
    ServeClientSenseVoice.SINGLE_MODEL = model or MagicMock()
    websocket = MagicMock()
    with patch.object(sv.threading, "Thread"):
        client = ServeClientSenseVoice(websocket, language=language, client_uid="uid", model="/models/x", **kwargs)
    return client, websocket


class TestText(unittest.TestCase):
    def test_clean_text_drops_all_tags(self):
        raw = "<|ja|><|EMO_UNKNOWN|><|Speech|><|withitn|>文明とは、共同で生活します。"
        self.assertEqual(sv.clean_text(raw), "文明とは、共同で生活します。")

    def test_clean_text_keeps_plain_text_and_comparison_signs(self):
        self.assertEqual(sv.clean_text(" 3 < 5 and 7 > 2 "), "3 < 5 and 7 > 2")

    def test_pick_language_keeps_supported_codes(self):
        for code in ("zh", "en", "yue", "ja", "ko"):
            self.assertEqual(sv.pick_language(code), code)

    def test_pick_language_falls_back_to_auto(self):
        for code in (None, "", "fr", "auto"):
            self.assertEqual(sv.pick_language(code), "auto")


class TestRecognize(unittest.TestCase):
    def test_passes_audio_and_language_to_the_model_and_cleans_the_output(self):
        model = MagicMock()
        model.generate.return_value = [{"key": "k", "text": "<|en|><|Speech|>hello world"}]
        text = sv.recognize(model, np.ones(1600, dtype=np.float32), "en")
        self.assertEqual(text, "hello world")
        kwargs = model.generate.call_args.kwargs
        self.assertEqual(kwargs["language"], "en")
        self.assertEqual(tuple(kwargs["input"].shape), (1600,))
        self.assertTrue(kwargs["disable_pbar"])

    def test_empty_model_result_is_empty_text(self):
        model = MagicMock()
        model.generate.return_value = []
        self.assertEqual(sv.recognize(model, np.zeros(1600, dtype=np.float32), "auto"), "")


class TestClient(unittest.TestCase):
    def test_handshake_sends_ready_and_names_the_backend(self):
        _, websocket = make_client()
        sent = json.loads(websocket.send.call_args.args[0])
        self.assertEqual(sent, {"uid": "uid", "message": "SERVER_READY", "backend": "sensevoice"})

    def test_unsupported_language_becomes_auto(self):
        client, _ = make_client(language="fr")
        self.assertEqual(client.language, "auto")

    def test_failed_model_load_reports_an_error_and_closes(self):
        ServeClientSenseVoice.SINGLE_MODEL = None
        websocket = MagicMock()
        with patch.object(sv, "load_model", side_effect=RuntimeError("boom")), \
                patch.object(sv.threading, "Thread") as thread:
            ServeClientSenseVoice(websocket, language="en", client_uid="uid", model="/models/x")
        self.assertEqual(json.loads(websocket.send.call_args.args[0])["status"], "ERROR")
        websocket.close.assert_called_once()
        thread.assert_not_called()

    def test_transcribe_builds_segments_from_speech_pieces(self):
        client, _ = make_client(language="en")
        audio = np.zeros(5 * 16000, dtype=np.float32)
        spans = [{"start": 16000, "end": 48000}, {"start": 64000, "end": 80000}]
        with patch.object(sv, "get_speech_timestamps", return_value=spans), \
                patch.object(sv, "recognize", side_effect=["first sentence", "second sentence"]):
            result = client.transcribe_audio(audio)
        self.assertEqual(result, [SenseVoiceSegment(1.0, 3.0, "first sentence"),
                                  SenseVoiceSegment(4.0, 5.0, "second sentence")])

    def test_transcribe_skips_clicks_and_empty_text(self):
        client, _ = make_client(language="en")
        audio = np.zeros(3 * 16000, dtype=np.float32)
        spans = [{"start": 0, "end": 1600}, {"start": 16000, "end": 32000}]  # 0.1 s, then 1 s
        with patch.object(sv, "get_speech_timestamps", return_value=spans), \
                patch.object(sv, "recognize", return_value="") as recognize:
            result = client.transcribe_audio(audio)
        self.assertIsNone(result)
        self.assertEqual(recognize.call_count, 1)

    def test_transcribe_returns_none_without_speech(self):
        client, _ = make_client()
        with patch.object(sv, "get_speech_timestamps", return_value=[]):
            self.assertIsNone(client.transcribe_audio(np.zeros(16000, dtype=np.float32)))

    def test_output_reaches_the_client_as_draft_segments(self):
        client, websocket = make_client(language="en")
        websocket.send.reset_mock()
        client.handle_transcription_output([SenseVoiceSegment(0.0, 2.0, "hello there")], 2.0)
        segments = json.loads(websocket.send.call_args.args[0])["segments"]
        self.assertEqual([(s["text"], s["completed"]) for s in segments], [("hello there", False)])

    def test_earlier_pieces_are_finished_and_the_last_stays_a_draft(self):
        client, websocket = make_client(language="en")
        websocket.send.reset_mock()
        pieces = [SenseVoiceSegment(0.0, 2.0, "one"), SenseVoiceSegment(3.0, 5.0, "two")]
        client.handle_transcription_output(pieces, 5.0)
        segments = json.loads(websocket.send.call_args.args[0])["segments"]
        self.assertEqual([(s["text"], s["completed"]) for s in segments], [("one", True), ("two", False)])


class TestPreload(unittest.TestCase):
    def setUp(self):
        ServeClientSenseVoice.SINGLE_MODEL = None

    def test_loads_once_and_warms_up_on_silence(self):
        model = MagicMock()
        with patch.object(sv, "load_model", return_value=model) as load, \
                patch.object(sv, "get_speech_timestamps", return_value=[]) as vad, \
                patch.object(sv, "recognize", return_value="") as recognize:
            ServeClientSenseVoice.preload("/models/x")
            ServeClientSenseVoice.preload("/models/x")
        load.assert_called_once_with("/models/x")
        self.assertEqual(recognize.call_count, 2)
        self.assertEqual(vad.call_count, 2)
        self.assertIs(ServeClientSenseVoice.SINGLE_MODEL, model)


class TestBackendType(unittest.TestCase):
    def test_sensevoice_is_a_valid_backend(self):
        self.assertTrue(BackendType.is_valid("sensevoice"))
        self.assertTrue(BackendType("sensevoice").is_sensevoice())
        self.assertFalse(BackendType.FASTER_WHISPER.is_sensevoice())


if __name__ == "__main__":
    unittest.main()
