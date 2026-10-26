"""C4 -- reclaiming a branch changes state (spec 10.3, 10.4). Open until built.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from srt1 import SRT1  # noqa: E402


class TestBranchReclaim(unittest.TestCase):
    def setUp(self):
        self.s = SRT1()
        self.addCleanup(self.s.store.db.close)
        self.sid = sid = self.s.create_stream("c4")
        for t in ("INTENT: Build the tool", "POSITION: implementing the validator",
                  "BRANCH: evaluate vector search", "POSITION: reading vector papers",
                  "END BRANCH: parked for later", "POSITION: implementing the validator"):
            self.s.observe(t, sid)
        self.bid = next(iter(self.s.store.load_model(sid).scalars["branches"]))

    def test_reclaim_reopens_branch_at_its_stopping_position(self):
        pkg = self.s.reclaim(self.sid, self.bid)
        self.assertEqual(pkg["commit"]["status"], "COMMITTED")
        self.assertEqual(self.s.store.events(self.sid)[-1]["event_type"], "RECLAIM")
        st = self.s.get_state(self.sid)
        self.assertIn(self.bid, st["branches"])
        self.assertEqual(st["current_position"], "reading vector papers")  # resumes where it stopped

    def test_reclaimed_branch_returns_to_where_the_parent_was_at_reclaim(self):
        self.s.reclaim(self.sid, self.bid)
        br = self.s.get_state(self.sid)["branches"][self.bid]
        self.assertEqual(br["return_point"]["position"], "implementing the validator")
        self.s.observe("END BRANCH: done for real", self.sid)
        self.assertEqual(self.s.get_state(self.sid)["current_position"], "implementing the validator")

    def test_cannot_reclaim_a_branch_that_is_already_open(self):
        self.assertEqual(self.s.reclaim(self.sid, self.bid)["commit"]["status"], "COMMITTED")
        v = self.s.get_state(self.sid)["state_version"]
        pkg = self.s.reclaim(self.sid, self.bid)
        self.assertNotEqual(pkg.get("commit", {}).get("status"), "COMMITTED")
        self.assertEqual(self.s.get_state(self.sid)["state_version"], v)

    def test_reclaimed_branch_survives_recovery(self):
        self.assertEqual(self.s.reclaim(self.sid, self.bid)["commit"]["status"], "COMMITTED")
        expected = self.s.get_state(self.sid)
        self.assertIn(self.bid, expected["branches"])
        self.s.store.db.execute("DELETE FROM living_state")
        self.assertEqual(self.s.get_state(self.sid), expected)


if __name__ == "__main__":
    unittest.main()
