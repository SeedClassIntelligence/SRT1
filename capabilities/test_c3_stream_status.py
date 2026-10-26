"""C3 -- stream status transitions (spec 5.1: ACTIVE / PAUSED / COMPLETED / ARCHIVED). Open until built.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from srt1 import SRT1  # noqa: E402


class TestStreamStatus(unittest.TestCase):
    def setUp(self):
        self.s = SRT1()
        self.addCleanup(self.s.store.db.close)
        self.sid = self.s.create_stream("c3")
        self.s.observe("INTENT: keep going", self.sid)

    def status(self):
        return self.s.store.get_stream(self.sid)["status"]

    def test_pause_resume_are_logged_with_reason(self):
        self.s.set_stream_status(self.sid, "PAUSED", "waiting on review")
        self.assertEqual(self.status(), "PAUSED")
        self.s.set_stream_status(self.sid, "ACTIVE", "review done")
        self.assertEqual(self.status(), "ACTIVE")
        hist = self.s.stream_status_history(self.sid)
        self.assertEqual([(h["from"], h["to"]) for h in hist], [("ACTIVE", "PAUSED"), ("PAUSED", "ACTIVE")])
        self.assertEqual(hist[0]["reason"], "waiting on review")

    def test_illegal_transition_is_rejected_and_state_unchanged(self):
        self.s.set_stream_status(self.sid, "ARCHIVED", "retired")
        with self.assertRaises(ValueError):
            self.s.set_stream_status(self.sid, "ACTIVE", "oops")
        self.assertEqual(self.status(), "ARCHIVED")

    def test_archived_stream_refuses_new_input_but_stays_readable(self):
        v = self.s.get_state(self.sid)["state_version"]
        self.s.set_stream_status(self.sid, "ARCHIVED", "retired")
        r = self.s.observe("DECISION: sneak in", self.sid)
        self.assertEqual(r.status, "REFUSED")
        st = self.s.get_state(self.sid)
        self.assertEqual(st["state_version"], v)
        self.assertEqual(st["active_intent"]["text"], "keep going")  # history survives archiving

    def test_archived_stream_stores_nothing(self):
        self.s.set_stream_status(self.sid, "ARCHIVED", "retired")
        n = self.s.store.one("SELECT COUNT(*) AS n FROM source_events WHERE stream_id=?", (self.sid,))["n"]
        self.assertEqual(self.s.observe("DECISION: sneak in", self.sid).status, "REFUSED")
        self.assertEqual(self.s.store.one("SELECT COUNT(*) AS n FROM source_events WHERE stream_id=?",
                                          (self.sid,))["n"], n)  # archived = read-only, evidence included

    def test_paused_stream_still_preserves_source(self):
        self.s.set_stream_status(self.sid, "PAUSED", "break")
        r = self.s.observe("DECISION: thought of while paused", self.sid)
        self.assertEqual(r.status, "DEFERRED")  # evidence kept, interpretation waits for resume
        self.assertEqual(self.s.store.get_source_event(r.source_event_id)["processing_status"], "DEFERRED")


if __name__ == "__main__":
    unittest.main()
