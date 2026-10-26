#!/usr/bin/env python3
"""Required capabilities: a blocked capability the owner marked required fails the gate.
Run: python3 tests/test_required.py"""
import json, os, sys, unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from test_forge import Base  # noqa: E402

TRIED = ["--tried", "A: tried the live path and it failed because there is no credential available here",
         "--tried", "B: tried a recorded fixture and it failed because no recording exists to replay"]


class TestRequired(Base):
    def blocked(self, cid="C1", required=False):
        self.f("cap-add", "needs a live model", *(["--required"] if required else []), expect=0)
        self.f("cap-block", cid, "no way to run it", *TRIED, expect=0)

    def test_blocked_but_not_required_still_passes_the_gate(self):
        self.blocked()
        r = self.f("gate"); self.assertEqual(r.returncode, 0, r.stdout)

    def test_required_and_blocked_fails_the_gate(self):
        self.blocked(required=True)
        r = self.f("gate"); self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("REQUIRED", r.stdout); self.assertIn("C1", r.stdout)
        self.assertIn("GATE: FAIL", r.stdout)

    def test_cap_require_marks_an_existing_capability(self):
        self.blocked()
        self.assertEqual(self.f("gate").returncode, 0)
        self.f("cap-require", "C1", "--reason", "spec Production gate must not read as passing", expect=0)
        self.assertEqual(self.f("gate").returncode, 2)
        st = json.loads((Path(self.p) / "forge.json").read_text())
        self.assertTrue(st["capabilities"]["C1"]["required"])
        self.assertIn("Production gate", st["capabilities"]["C1"]["required_reason"])

    def test_required_report_status_is_not_done_and_names_the_capability(self):
        self.blocked(required=True)
        self.f("report")
        text = (Path(self.p) / "REPORT.md").read_text()
        self.assertIn("**Status: NOT DONE**", text)
        self.assertNotIn("GATE PASSED", text)
        self.assertIn("REQUIRED", text)

    def test_status_shows_required(self):
        self.blocked(required=True)
        self.assertIn("REQUIRED", self.f("status", expect=0).stdout)

    def test_cap_require_unknown_id_is_refused(self):
        r = self.f("cap-require", "C9", "--reason", "x"); self.assertNotEqual(r.returncode, 0)

    def test_cap_require_needs_a_reason(self):
        self.f("cap-add", "x", expect=0)
        r = self.f("cap-require", "C1"); self.assertNotEqual(r.returncode, 0)

    def test_required_capability_that_passes_does_not_fail_the_gate(self):
        self.green_capability()
        self.f("cap-require", "C1", "--reason", "core behaviour", expect=0)
        r = self.f("gate"); self.assertEqual(r.returncode, 0, r.stdout)

    def test_required_cannot_be_unset_by_a_command(self):
        self.blocked(required=True)
        r = self.f("cap-require", "C1", "--reason", "again", expect=0)  # idempotent, still required
        self.assertEqual(self.f("gate").returncode, 2)


if __name__ == "__main__":
    unittest.main()
