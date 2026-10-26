"""Critical long-history validation and context economy (spec sections 19, 19.1, 15).

A large stream is built from early assumptions, later corrections, rejected approaches, decisions,
branches, unresolved questions, seeds, repeated terminology and superseded architecture. A narrow
current-state question must be answered from maintained state, not by rereading history.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from srt1 import SRT1, SourceEvent, render  # noqa: E402
from srt1.state import est_tokens, semantic_view  # noqa: E402

FILLER = ("We talked at length about {t}. The discussion covered tradeoffs, edge cases, naming, and how the "
          "storage layer interacts with the retrieval layer. " * 6)


class TestLongHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = SRT1(snapshot_every=8)
        cls.addClassCleanup(cls.s.store.db.close)
        s, cls.sid = cls.s, cls.s.create_stream("long")

        def say(text, st="USER_INPUT"):
            return s.observe(SourceEvent(content=text, source_type=st), cls.sid)

        say("INTENT: Build SRT1, a continuity tool that preserves developing cognition")
        say("DECISION: Use a graph database for storage | relationships are central | relational; files")
        cls.graph_id = next(i["item_id"] for i in s.store.load_model(cls.sid).items.values()
                            if "graph database" in i["content"].get("text", ""))
        say("DECISION: Summarize history recursively to keep context small | token cost")
        cls.summ_id = next(i["item_id"] for i in s.store.load_model(cls.sid).items.values()
                           if "Summarize history" in i["content"].get("text", ""))
        say("CONSTRAINT: Never lose a governing correction to save tokens")
        say("QUESTION: What should the snapshot cadence be?")
        say("POSITION: choosing the storage engine\nDIRECTION: persistence first")
        for i in range(120):  # a lot of conversation, mostly prose, some structure
            say(FILLER.format(t=f"topic {i} storage retrieval graph summary"), "ASSISTANT_OUTPUT")
            if i % 6 == 0:
                say(f"DISCOVERY: finding {i} about index design and query plans")
            if i % 15 == 0:
                say(f"SEED: idea {i} about lineage visualisation | maybe useful")
            if i == 30:
                say(f"CORRECTION[{cls.graph_id}]: Use a relational database (SQLite) for storage")
            if i == 60:
                say("BRANCH: evaluate vector search")
                say("POSITION: reading vector papers")
            if i == 70:
                say("END BRANCH: not needed in V1")
            if i == 90:
                say(f"SUPERSEDE[{cls.summ_id}]: Derive state from previous state plus new source, never recursive "
                    "summaries")
        say("POSITION: implementing the validator")

    def test_current_state_question_uses_state_only(self):
        pkg = self.s.get_context(self.sid, "Which database do we use for storage?")
        self.assertEqual(pkg["retrieval_depth"], "STATE")
        self.assertTrue(pkg["complete"])
        decs = [d["text"] for d in pkg["governing_decisions"]]
        self.assertIn("Use a relational database (SQLite) for storage", decs)
        self.assertNotIn("Use a graph database for storage", decs)  # similar historical text not promoted
        corr = pkg["applicable_corrections"][0]
        self.assertEqual(corr["corrects"][0]["was"], "Use a graph database for storage")  # rationale kept
        self.assertTrue(corr["source_event_ids"])  # traceable
        self.assertEqual(pkg["current_position"], "implementing the validator")

    def test_package_is_far_smaller_than_history_and_still_faithful(self):
        pkg = self.s.get_context(self.sid, "Which database do we use for storage?")
        tr = pkg["token_report"]
        self.assertGreater(tr["historical_tokens"], 20000)
        self.assertLess(tr["package_tokens"], tr["historical_tokens"] * 0.05)
        text = render(pkg)
        self.assertIn("Never lose a governing correction", text)  # constraint survives reduction
        self.assertIn("relational database (SQLite)", text)
        self.assertIn("previous state plus new source", self.s.get_context(
            self.sid, "how is state derived")["governing_decisions"][0]["text"])

    def test_tiny_budget_never_drops_governing_items(self):  # INV-031 / 15.3
        pkg = self.s.get_context(self.sid, "Which database do we use for storage?", budget=50)
        self.assertTrue(pkg["token_report"]["budget_exceeded"])  # advisory, not enforced over fidelity
        self.assertTrue(pkg["applicable_corrections"])
        self.assertTrue(pkg["active_constraints"])
        self.assertTrue(pkg["active_intent"])

    def test_escalates_to_lineage_when_asked_for_rationale(self):
        pkg = self.s.get_context(self.sid, "why did we reject the graph database", depth="LINEAGE")
        self.assertIn("LINEAGE", pkg["escalations"])
        self.assertTrue(any(h["text"] == "Use a graph database for storage" for h in pkg["historical_matches"])
                        or any(l["to"]["text"] == "Use a graph database for storage" for l in pkg["lineage"]))

    def test_escalates_to_source_for_information_only_in_history(self):
        s, sid = self.s, self.sid
        s.observe(SourceEvent(content="By the way the pilot customer is Zephyrine Logistics, a freight "
                              "forwarder in Rotterdam.", source_type="USER_INPUT"), sid)  # prose: no structure
        pkg = s.get_context(sid, "who is the pilot customer Zephyrine")
        self.assertIn("SOURCE", pkg["escalations"])
        self.assertTrue(any("Zephyrine" in e["excerpt"] for e in pkg["source_excerpts"]))
        self.assertTrue(pkg["complete"])

    def test_absent_information_is_reported_not_invented(self):  # INV-026
        pkg = self.s.get_context(self.sid, "what is the quarterly revenue forecast for Mars colony")
        self.assertFalse(pkg["complete"])
        self.assertTrue(pkg["ambiguities"])
        self.assertEqual(pkg["retrieval_depth"], "SOURCE")  # it tried every level before reporting

    def test_exact_wording_pulls_original_source(self):
        pkg = self.s.get_context(self.sid, "database storage", exact_wording=True)
        self.assertIn("SOURCE", pkg["escalations"])
        self.assertTrue(pkg["source_excerpts"])

    def test_unresolved_and_seeds_still_present(self):
        st = self.s.get_state(self.sid)
        self.assertIn("What should the snapshot cadence be?", [u["text"] for u in st["unresolved_items"]])
        self.assertGreaterEqual(len(st["seeds"]), 8)
        self.assertEqual(st["branches"], {})

    def test_state_is_compact_relative_to_history(self):
        st = self.s.get_state(self.sid)
        hist = self.s.retriever._historical_tokens(self.sid)
        self.assertLess(est_tokens(st), hist * 0.25)

    def test_full_replay_equivalence_after_long_run(self):
        self.assertTrue(self.s.verify(self.sid)["ok"])
        expected = semantic_view(self.s.store.load_model(self.sid))
        self.s.store.db.execute("DELETE FROM living_state")
        self.s.store.db.execute("DELETE FROM relationships")
        self.s.store.db.execute("DELETE FROM source_refs")
        self.s.store.db.execute("DELETE FROM items")
        info = self.s.recover(self.sid)
        self.assertGreater(info["events_replayed"], 0)
        self.assertLess(info["events_replayed"], 12)  # snapshot + short tail, not a full replay
        self.assertEqual(semantic_view(self.s.store.load_model(self.sid)), expected)

    def test_platform_metrics_recorded(self):
        self.s.get_context(self.sid, "Which database do we use for storage?")
        m = self.s.metrics(self.sid)
        self.assertGreater(m["requests"], 0)
        self.assertGreater(m["context_tokens_avoided"], 0)
        self.assertGreater(m["pct_answered_from_state_only"], 0)

    def test_development_path_reconstructible(self):
        events = self.s.store.events(self.sid)
        classes = [e["classification"] for e in events]
        self.assertEqual(events[0]["event_type"], "CREATE")
        for c in ("CORRECTION", "BRANCH", "SUPERSESSION", "SEED"):
            self.assertIn(c, classes)
        self.assertEqual([e["resulting_state_version"] for e in events], list(range(1, len(events) + 1)))


if __name__ == "__main__":
    unittest.main()
