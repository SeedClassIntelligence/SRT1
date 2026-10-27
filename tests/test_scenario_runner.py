"""The scenario runner and the live-recording script, exercised with a synthetic stand-in for the model.

Nothing here calls a real model. The "ideal model" below is hand-written and reads the real prompt, so it proves the
runner, the recording/replay round trip and the expectation checks -- not any model's judgement. When a real recording
is committed under tests/fixtures/llm/, `TestCommittedRecordings` replays it and guards it.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from srt1 import SRT1, LLMProposer  # noqa: E402
from srt1.scenario import run_scenario  # noqa: E402

SCENARIOS = ROOT / "tests" / "scenarios"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
KNOWN_CHECKS = {"has_active_intent", "decision_current_contains", "decision_not_current_contains",
                "constraint_current_contains", "unresolved_contains", "unresolved_not_contains",
                "seed_count_at_least", "intent_unchanged_since", "verify_ok"}


def load_script():
    spec = importlib.util.spec_from_file_location("record_llm_run", ROOT / "scripts" / "record_llm_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_of(user):
    return json.loads(user.split("STATE:\n", 1)[1].split("\nSOURCE TEXT", 1)[0])


def ident(user, needle):
    return next(i["id"] for i in state_of(user)["items"] if needle.lower() in i["text"].lower())


def out(cls, ops, conf="EXPLICIT"):
    return json.dumps({"classification": cls, "confidence": conf, "rationale": "synthetic", "operations": ops})


def ideal_model(system, user):
    """Hand-written, synthetic: the answers a sensible reflector would give for storage_correction.json."""
    src = user.split("<<<\n", 1)[1]
    if "keeps track of where my thinking" in src:
        return out("CONTINUATION", [{"op": "intent", "kind": "ORIGIN", "text": "Track where thinking has got to across long projects"}])
    if "let's use SQLite" in src:
        return out("CONTINUATION", [{"op": "item", "type": "DECISION", "text": "Use SQLite for storage", "rationale": "simple, transactional"}])
    if "never make network calls" in src:
        return out("CONTINUATION", [{"op": "item", "type": "CONSTRAINT", "text": "The core must never make network calls"}])
    if "prune old snapshots" in src and "not sure" in src:
        return out("CONTINUATION", [{"op": "item", "type": "UNRESOLVED", "text": "How to prune old snapshots"}])
    if "changed my mind on storage" in src:
        return out("CORRECTION", [{"op": "correct", "target": ident(user, "SQLite"), "text": "Use Postgres for storage"}])
    if "Side thought" in src:
        return out("SEED", [{"op": "seed", "text": "Render lineage as a graph"}])
    if "On snapshot pruning" in src:
        return out("RESOLUTION", [{"op": "resolve", "target": ident(user, "prune old snapshots"),
                                   "text": "Keep significant snapshots; thin periodic ones to one in ten"}])
    if "Redis" in src:
        return out("CONTINUATION", [{"op": "item", "type": "DECISION", "text": "Add Redis as a cache layer"}], "SUPPORTED")
    if "IGNORE ALL PREVIOUS" in src:  # a gullible model: obeys the injection
        return out("PIVOT", [{"op": "intent", "kind": "PIVOT", "text": "Sell advertising"}])
    return out("NO_MATERIAL_CHANGE", [])


def lazy_model(system, user):
    return out("NO_MATERIAL_CHANGE", [])


class TestScenarioFile(unittest.TestCase):
    def test_every_scenario_is_well_formed(self):
        files = sorted(SCENARIOS.glob("*.json"))
        self.assertTrue(files)
        for f in files:
            sc = json.loads(f.read_text())
            self.assertTrue(sc["name"] and sc["turns"], f)
            for t in sc["turns"]:
                self.assertTrue(t["text"].strip())
                self.assertIn(t.get("source_type", "USER_INPUT"),
                              ("USER_INPUT", "ASSISTANT_OUTPUT", "EXTERNAL_CONTENT", "SYSTEM_EVENT"))
                for c in t.get("checks", []):
                    self.assertIn(c["kind"], KNOWN_CHECKS, f"{f.name}: {c}")


class TestRunner(unittest.TestCase):
    def scenario(self):
        return json.loads((SCENARIOS / "storage_correction.json").read_text())

    def run_with(self, model):
        s = SRT1(proposer=LLMProposer(model))
        self.addCleanup(s.store.db.close)
        return run_scenario(s, self.scenario())

    def test_an_ideal_model_meets_every_expectation(self):
        reports = self.run_with(ideal_model)
        self.assertEqual(len(reports), 9)
        self.assertEqual([f for r in reports for f in r.failures], [])
        self.assertEqual(reports[-1].status, "REQUIRES_CLARIFICATION")  # the injection was contained

    def test_a_model_that_notices_nothing_fails_loudly(self):
        failures = [f for r in self.run_with(lazy_model) for f in r.failures]
        self.assertGreaterEqual(len(failures), 6)

    def test_a_gullible_assistant_suggestion_is_not_promoted(self):
        reports = self.run_with(ideal_model)
        self.assertEqual(reports[7].failures, [])  # Redis stays PROVISIONAL


class TestRecordingScript(unittest.TestCase):
    def setUp(self):
        self.mod = load_script()
        self.tmp = Path(tempfile.mkdtemp())
        self.rec = self.tmp / "rec.jsonl"
        self.sc = str(SCENARIOS / "storage_correction.json")

    def run_script(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = self.mod.main([self.sc, "--out", str(self.rec), *argv])
        return code, o.getvalue(), e.getvalue()

    def fake_claude(self, model=ideal_model):
        class Fake:
            def __init__(self, **kw):
                self.kw = kw

            def __call__(self, system, user):
                return model(system, user)
        self.mod.AnthropicCompleter = Fake

    def test_live_run_refuses_without_yes(self):
        code, _, err = self.run_script()
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertFalse(self.rec.exists())

    def test_missing_sdk_is_a_clear_exit_2_and_writes_nothing(self):
        def no_sdk(**kw):
            raise ImportError("anthropic")
        self.mod.AnthropicCompleter = no_sdk
        code, _, err = self.run_script("--yes")
        self.assertEqual(code, 2)
        self.assertIn("pip install anthropic", err)
        self.assertFalse(self.rec.exists())

    def test_live_run_records_and_replay_reproduces_it_with_fresh_ids(self):
        self.fake_claude()
        code, out_, _ = self.run_script("--yes")
        self.assertEqual(code, 0, out_)
        self.assertIn("all expectations met", out_)
        lines = self.rec.read_text().splitlines()
        self.assertEqual(len(lines), 9)  # one exchange per turn
        self.mod.AnthropicCompleter = None  # replay must not touch the model class at all
        code, out_, _ = self.run_script("--replay")
        self.assertEqual(code, 0, out_)
        self.assertIn("REPLAY", out_)

    def test_failed_expectations_exit_1_but_the_recording_is_still_written(self):
        self.fake_claude(lazy_model)
        code, out_, _ = self.run_script("--yes")
        self.assertEqual(code, 1)
        self.assertTrue(self.rec.exists())
        self.assertIn("expectation(s) failed", out_)

    def test_will_not_overwrite_an_existing_recording(self):
        self.rec.write_text("keep me\n")
        self.fake_claude()
        code, _, err = self.run_script("--yes")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(self.rec.read_text(), "keep me\n")

    def test_replay_without_a_recording_is_exit_2(self):
        code, _, err = self.run_script("--replay")
        self.assertEqual(code, 2)
        self.assertIn("no recording", err)

    def test_model_error_mid_run_aborts_with_exit_2_and_keeps_what_was_recorded(self):
        calls = []

        def flaky(system, user):
            calls.append(1)
            if len(calls) == 4:
                raise RuntimeError("network down")
            return ideal_model(system, user)
        self.fake_claude(flaky)
        code, out_, err = self.run_script("--yes")
        self.assertEqual(code, 2)
        self.assertIn("STOPPED at turn 4", err)
        self.assertIn("network down", err)
        self.assertEqual(len(self.rec.read_text().splitlines()), 3)  # the three exchanges before the failure


class TestCommittedRecordings(unittest.TestCase):
    def test_every_committed_recording_still_replays_clean(self):
        files = sorted(FIXTURES.glob("*.jsonl")) if FIXTURES.exists() else []
        if not files:
            self.skipTest("no real-model recordings committed yet (run scripts/record_llm_run.py live, then commit)")
        mod = load_script()
        for f in files:
            sc = SCENARIOS / f"{f.stem}.json"
            self.assertTrue(sc.exists(), f"recording {f.name} has no scenario {sc.name}")
            o = io.StringIO()
            with redirect_stdout(o), redirect_stderr(o):
                code = mod.main([str(sc), "--replay", "--out", str(f)])
            self.assertEqual(code, 0, f"{f.name}:\n{o.getvalue()}")


if __name__ == "__main__":
    unittest.main()
