"""The one message shape every part of the pipeline uses.

Services never call each other; they post these messages to a feed (a Redis stream) and read them
from one. ``version`` lets the shape grow without breaking old readers, ``kind`` says what the message is,
``session`` and ``segment`` let any service skip repeats, and ``made_at`` gives every stage its own
timestamp, which is how translation delay is measured apart from transcription delay.

    {"version": 1, "kind": "final_text", "session": "s-7f3a", "segment": 42, "start": 81.2, "end": 84.95,
     "lang": "en", "text": "...", "made_at": "2026-10-01T10:15:03.120Z", "source": "speech-engine@sensevoice"}
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer, model_validator

# Feeds (Redis stream names).
FINAL_TEXT = "final-text"
TRANSLATIONS = "translations"

# Message kinds.
KIND_FINAL_TEXT = "final_text"
KIND_TRANSLATION = "translation"

VERSION = 1


class FeedMessage(BaseModel):
    # A newer writer may add fields; an older reader ignores them.
    model_config = ConfigDict(extra="ignore")

    version: int = Field(default=VERSION, ge=1)
    kind: str = Field(min_length=1)
    session: str = Field(min_length=1)
    segment: int = Field(ge=0)
    # finite only: JSON has no infinity, and pydantic would write it as null, which no reader accepts
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    lang: str
    text: str
    made_at: AwareDatetime
    source: str

    @model_validator(mode="after")
    def _ends_after_it_starts(self):
        if self.end < self.start:
            raise ValueError("end must not be before start")
        return self

    @field_serializer("made_at")
    def _made_at_utc(self, value: datetime) -> str:
        # 2026-10-01T10:15:03.120Z: UTC, milliseconds
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"

    @classmethod
    def new(cls, *, kind: str, session: str, segment: int, start: float, end: float, lang: str, text: str,
            source: str) -> "FeedMessage":
        """A message stamped with the current time, to the millisecond (what the wire format keeps)."""
        now = datetime.now(timezone.utc)
        return cls(kind=kind, session=session, segment=segment, start=start, end=end, lang=lang, text=text,
                   made_at=now.replace(microsecond=now.microsecond // 1000 * 1000), source=source)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> "FeedMessage":
        return cls.model_validate_json(data)
