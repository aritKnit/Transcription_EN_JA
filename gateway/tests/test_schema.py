import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from gateway.schema import KIND_FINAL_TEXT, FeedMessage

HLD_EXAMPLE = {
    "version": 1, "kind": "final_text", "session": "s-7f3a", "segment": 42, "start": 81.20, "end": 84.95,
    "lang": "en", "text": "…", "made_at": "2026-10-01T10:15:03.120Z", "source": "speech-engine@base-int8",
}


def make(**changes):
    fields = dict(HLD_EXAMPLE, **changes)
    return FeedMessage.model_validate(fields)


class TestFeedMessage(unittest.TestCase):
    def test_the_example_from_the_design_parses(self):
        m = make()
        self.assertEqual((m.kind, m.session, m.segment, m.start, m.end), ("final_text", "s-7f3a", 42, 81.2, 84.95))
        self.assertEqual(m.made_at, datetime(2026, 10, 1, 10, 15, 3, 120000, tzinfo=timezone.utc))

    def test_round_trip_through_json_keeps_everything(self):
        m = make(text="こんにちは world")
        again = FeedMessage.from_json(m.to_json())
        self.assertEqual(again, m)

    def test_made_at_is_written_as_utc_with_milliseconds_and_z(self):
        data = json.loads(make().to_json())
        self.assertEqual(data["made_at"], "2026-10-01T10:15:03.120Z")

    def test_made_at_with_another_offset_is_written_as_utc(self):
        data = json.loads(make(made_at="2026-10-01T12:15:03.120+02:00").to_json())
        self.assertEqual(data["made_at"], "2026-10-01T10:15:03.120Z")

    def test_unknown_fields_from_a_newer_writer_are_ignored(self):
        m = make(version=2, speaker="s1", confidence=0.9)
        self.assertEqual(m.version, 2)
        self.assertFalse(hasattr(m, "speaker"))

    def test_version_defaults_to_one(self):
        fields = {k: v for k, v in HLD_EXAMPLE.items() if k != "version"}
        self.assertEqual(FeedMessage.model_validate(fields).version, 1)

    def test_missing_fields_are_rejected(self):
        for name in ("kind", "session", "segment", "start", "end", "lang", "text", "made_at", "source"):
            with self.subTest(missing=name):
                fields = {k: v for k, v in HLD_EXAMPLE.items() if k != name}
                with self.assertRaises(ValidationError):
                    FeedMessage.model_validate(fields)

    def test_time_without_a_zone_is_rejected(self):
        with self.assertRaises(ValidationError):
            make(made_at="2026-10-01T10:15:03.120")

    def test_a_segment_cannot_end_before_it_starts(self):
        with self.assertRaises(ValidationError):
            make(start=10.0, end=9.0)

    def test_negative_numbers_are_rejected(self):
        for name in ("segment", "start", "end"):
            with self.subTest(field=name):
                with self.assertRaises(ValidationError):
                    make(**{name: -1})

    def test_empty_session_and_kind_are_rejected(self):
        for name in ("session", "kind"):
            with self.subTest(field=name):
                with self.assertRaises(ValidationError):
                    make(**{name: ""})

    def test_not_json_is_rejected(self):
        with self.assertRaises(ValidationError):
            FeedMessage.from_json("not json")

    def test_a_new_message_equals_itself_after_a_round_trip(self):
        m = FeedMessage.new(kind=KIND_FINAL_TEXT, session="s", segment=1, start=0, end=1, lang="en", text="x", source="t")
        self.assertEqual(m.made_at.microsecond % 1000, 0)
        self.assertEqual(FeedMessage.from_json(m.to_json()), m)

    def test_new_stamps_the_current_utc_time(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        m = FeedMessage.new(kind=KIND_FINAL_TEXT, session="s", segment=0, start=0, end=1, lang="en", text="x", source="t")
        self.assertLessEqual(before, m.made_at)
        self.assertLessEqual(m.made_at, datetime.now(timezone.utc))
        self.assertEqual(m.version, 1)


if __name__ == "__main__":
    unittest.main()
