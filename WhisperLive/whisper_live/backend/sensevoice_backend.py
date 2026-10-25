import json
import logging
import re
import threading
import time
from dataclasses import dataclass

import numpy as np
import torch
from faster_whisper.vad import VadOptions, get_speech_timestamps

from whisper_live.backend.base import ServeClientBase

# Languages SenseVoice-Small was trained on; anything else is left to its own detection.
SUPPORTED_LANGUAGES = {"zh", "en", "yue", "ja", "ko"}
AUTO_LANGUAGE = "auto"

# SenseVoice prefixes its text with tags such as <|ja|><|EMO_UNKNOWN|><|Speech|><|withitn|>.
_TAGS = re.compile(r"<\|[^|>]*\|>")

# Speech shorter than this is a click or a breath, not worth a model pass.
MIN_SPEECH_S = 0.25

# Sentence-sized pieces: faster-whisper's default of 2 s of silence would glue sentences together.
DEFAULT_VAD_PARAMETERS = {"threshold": 0.5, "min_silence_duration_ms": 500}


@dataclass
class SenseVoiceSegment:
    """One recognised stretch of speech, shaped like the segments the other backends return."""
    start: float
    end: float
    text: str
    no_speech_prob: float = 0.0


def clean_text(raw):
    """Drop SenseVoice's language, emotion and event tags and keep the spoken text."""
    return _TAGS.sub("", raw).strip()


def pick_language(language):
    """SenseVoice language code for a client's ``language`` option, ``auto`` when unsure."""
    return language if language in SUPPORTED_LANGUAGES else AUTO_LANGUAGE


def load_model(model_path, device="cpu"):
    """Load FunAudioLLM/SenseVoiceSmall from a local folder with FunASR."""
    from funasr import AutoModel

    logging.info(f"Loading SenseVoice model: {model_path}")
    return AutoModel(model=model_path, device=device, disable_update=True)


def recognize(model, audio, language):
    """Run SenseVoice on 16 kHz mono float32 audio and return the cleaned text."""
    result = model.generate(
        input=torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)),
        cache={},
        language=language,
        use_itn=True,
        batch_size_s=60,
        disable_pbar=True,
    )
    return clean_text(result[0]["text"]) if result else ""


class ServeClientSenseVoice(ServeClientBase):
    SINGLE_MODEL = None
    SINGLE_MODEL_LOCK = threading.Lock()

    def __init__(
        self,
        websocket,
        language=None,
        client_uid=None,
        model=None,
        vad_parameters=None,
        send_last_n_segments=10,
        no_speech_thresh=0.45,
        clip_audio=False,
        same_output_threshold=10,
        translation_queue=None,
    ):
        """
        Initialize a ServeClient instance for the SenseVoice-Small model.

        The model is shared by all clients and loaded once. Silero VAD cuts each audio chunk
        into speech pieces, SenseVoice recognises each piece, and the base class turns the
        pieces into drafts and finished segments like it does for the other backends.

        Args:
            websocket (WebSocket): The WebSocket connection for the client.
            language (str, optional): ``zh``, ``en``, ``yue``, ``ja`` or ``ko``. Anything else
                means automatic language detection.
            client_uid (str, optional): A unique identifier for the client.
            model (str, optional): Folder holding the FunAudioLLM/SenseVoiceSmall files.
            vad_parameters (dict, optional): Silero VAD options. Defaults to ``DEFAULT_VAD_PARAMETERS``.
            send_last_n_segments (int, optional): Number of most recent segments to send to the client.
            no_speech_thresh (float, optional): Segments above this no-speech probability are discarded.
            clip_audio (bool, optional): Whether to clip audio with no valid segments.
            same_output_threshold (int, optional): Number of repeated outputs before a draft counts as finished.
        """
        super().__init__(
            client_uid,
            websocket,
            send_last_n_segments,
            no_speech_thresh,
            clip_audio,
            same_output_threshold,
            translation_queue,
        )
        self.language = pick_language(language)
        self.model_path = model
        self.vad_options = VadOptions(**(vad_parameters or DEFAULT_VAD_PARAMETERS))

        try:
            self.transcriber = self.shared_model(model)
        except Exception as e:
            logging.error(f"Failed to load model: {e}")
            self.websocket.send(json.dumps({
                "uid": self.client_uid,
                "status": "ERROR",
                "message": f"Failed to load model: {model}"
            }))
            self.websocket.close()
            return

        self.trans_thread = threading.Thread(target=self.speech_to_text)
        self.trans_thread.start()
        self.websocket.send(json.dumps({
            "uid": self.client_uid,
            "message": self.SERVER_READY,
            "backend": "sensevoice"
        }))
        logging.info(f"Running sensevoice backend with language: {self.language}")

    @classmethod
    def shared_model(cls, model_path):
        """The one model all clients use, loaded on first need."""
        with cls.SINGLE_MODEL_LOCK:
            if cls.SINGLE_MODEL is None:
                cls.SINGLE_MODEL = load_model(model_path)
            return cls.SINGLE_MODEL

    @classmethod
    def preload(cls, model_path):
        """
        Load the shared model and run it once on silence, so the first client does not
        wait for the model load or for the first slow inference pass.

        Args:
            model_path (str): Folder holding the FunAudioLLM/SenseVoiceSmall files.
        """
        model = cls.shared_model(model_path)
        silence = np.zeros(2 * cls.RATE, dtype=np.float32)
        t0 = time.time()
        get_speech_timestamps(silence, VadOptions(**DEFAULT_VAD_PARAMETERS))
        with cls.SINGLE_MODEL_LOCK:
            recognize(model, silence, AUTO_LANGUAGE)
        logging.info(f"Model warm-up took {time.time() - t0:.1f}s")

    def transcribe_audio(self, input_sample):
        """
        Recognise the speech in the audio chunk.

        Args:
            input_sample (np.array): 16 kHz mono float32 audio.

        Returns:
            list of SenseVoiceSegment with times relative to the start of the chunk, or
            None when the chunk holds no speech.
        """
        segments = []
        for span in get_speech_timestamps(input_sample, self.vad_options):
            if (span["end"] - span["start"]) / self.RATE < MIN_SPEECH_S:
                continue
            with self.SINGLE_MODEL_LOCK:
                text = recognize(self.transcriber, input_sample[span["start"]:span["end"]], self.language)
            if text:
                segments.append(SenseVoiceSegment(span["start"] / self.RATE, span["end"] / self.RATE, text))
        return segments or None

    def handle_transcription_output(self, result, duration):
        """
        Update the transcript from the recognised pieces and send it to the client.

        Args:
            result (list): The pieces returned by ``transcribe_audio``.
            duration (float): Duration of the transcribed audio chunk.
        """
        segments = []
        if len(result):
            self.t_start = None
            last_segment = self.update_segments(result, duration)
            segments = self.prepare_segments(last_segment)

        if len(segments):
            self.send_transcription_to_client(segments)
