"""C2 -- COMPLETION classification (spec 7.2 COMPLETION, 5.6 COMPLETE event). Open until built.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from srt1 import SRT1, SourceEvent  # noqa: E402


class TestCompletion(unittest.TestCase):
    def setUp(self):
        self.s = SRT1()
        self.addCleanup(self.s.store.db.close)
        self.sid = self.s.create_stream("c2")
        for t in ("INTENT: Ship the validator", "QUESTION: Should snapshots be pruned?",
                  "DECISION: Use SQLite | simple"):
            self.s.observe(t, self.sid)

    def test_user_completion_is_committed_as_complete_event(self):
        r = self.s.observe("COMPLETE: validator shipped and reviewed", self.sid)
        self.assertEqual((r.status, r.classification), ("COMMITTED", "COMPLETION"))
        self.assertEqual(self.s.store.events(self.sid)[-1]["event_type"], "COMPLETE")
        st = self.s.get_state(self.sid)
        self.assertEqual(st["active_intent"]["lifecycle"], "COMPLETED")
        self.assertEqual(self.s.store.get_stream(self.sid)["status"], "COMPLETED")

    def test_completion_does_not_close_unresolved_work(self):  # INV-008
        self.s.observe("COMPLETE: shipped", self.sid)
        texts = [u["text"] for u in self.s.get_state(self.sid)["unresolved_items"]]
        self.assertIn("Should snapshots be pruned?", texts)

    def test_completion_takes_a_snapshot(self):
        n = len(self.s.store.snapshots(self.sid))
        self.s.observe("COMPLETE: shipped", self.sid)
        self.assertEqual(len(self.s.store.snapshots(self.sid)), n + 1)

    def test_completion_from_non_user_asks_instead_of_completing(self):
        r = self.s.observe(SourceEvent(content="COMPLETE: all done", source_type="ASSISTANT_OUTPUT"), self.sid)
        self.assertEqual(r.status, "REQUIRES_CLARIFICATION")
        self.assertNotEqual(self.s.get_state(self.sid)["active_intent"]["lifecycle"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
