"""Pull integration (13.1), push triggers and micro-reinjection (13.2/13.3), branch items (10.3), metrics (15.2).

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from srt1 import SRT1, SourceEvent  # noqa: E402
from srt1.integration import QUERY_KINDS, TOOLS, Consumer, dispatch  # noqa: E402
from srt1.push import render_push  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.s = SRT1()
        self.addCleanup(self.s.store.db.close)
        self.sid = self.s.create_stream("t")
        for t in ("INTENT: Build a continuity tool for developing cognition",
                  "DECISION: Use SQLite for storage | simple and transactional | Postgres; files",
                  "CONSTRAINT: Never call the network from the core",
                  "QUESTION: How should snapshots be pruned over time?",
                  "SEED: render lineage as an interactive graph | helps reclamation",
                  "POSITION: designing the schema\nDIRECTION: persistence layer first"):
            self.s.observe(t, self.sid)

    def say(self, text, st="USER_INPUT"):
        return self.s.observe(SourceEvent(content=text, source_type=st), self.sid)

    def item(self, text):
        return next(i for i in self.s.store.load_model(self.sid).items.values() if i["content"].get("text") == text)


class TestPull(Base):
    def test_tool_definitions_are_well_formed(self):
        names = [t["name"] for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for t in TOOLS:
            self.assertTrue(t["description"])
            self.assertEqual(t["input_schema"]["type"], "object")
            self.assertTrue(set(t["input_schema"]["required"]) <= set(t["input_schema"]["properties"]))
        json.dumps(TOOLS)  # must serialise as-is into an API request

    def test_every_query_kind_answers_from_state(self):
        dec = self.item("Use SQLite for storage")["item_id"]
        for kind in QUERY_KINDS:
            out = dispatch(self.s, self.sid, "srt1_query", {"kind": kind, "item_id": dec})
            self.assertNotIn("error", out, (kind, out))
            json.dumps(out)
        self.assertEqual(dispatch(self.s, self.sid, "srt1_query", {"kind": "decisions"})["decisions"][0]["text"],
                         "Use SQLite for storage")
        self.assertEqual(dispatch(self.s, self.sid, "srt1_query", {"kind": "current_position"})["position"],
                         "designing the schema")

    def test_rationale_and_source_support(self):
        dec = self.item("Use SQLite for storage")["item_id"]
        r = dispatch(self.s, self.sid, "srt1_query", {"kind": "rationale", "item_id": dec})
        self.assertEqual(r["rationale"]["rationale"], "simple and transactional")
        e = dispatch(self.s, self.sid, "srt1_query", {"kind": "source_support", "item_id": dec})
        self.assertIn("Use SQLite for storage", e["evidence"][0]["content"])

    def test_errors_are_returned_not_raised(self):
        self.assertIn("error", dispatch(self.s, self.sid, "srt1_nope"))
        self.assertIn("error", dispatch(self.s, self.sid, "srt1_query", {"kind": "rationale"}))  # needs item_id
        self.assertIn("error", dispatch(self.s, self.sid, "srt1_query", {"kind": "bogus"}))
        self.assertIn("error", dispatch(self.s, self.sid, "srt1_get_context", {}))

    def test_get_context_checkpoint_verify_reclaim_via_dispatch(self):
        pkg = dispatch(self.s, self.sid, "srt1_get_context", {"task": "which database do we use"})
        self.assertEqual(pkg["governing_decisions"][0]["text"], "Use SQLite for storage")
        self.assertIn("snapshot_id", dispatch(self.s, self.sid, "srt1_checkpoint", {"reason": "x"}))
        self.assertTrue(dispatch(self.s, self.sid, "srt1_verify", {})["ok"])
        seed = self.s.get_state(self.sid)["seeds"][0]["item_id"]
        self.assertEqual(dispatch(self.s, self.sid, "srt1_reclaim", {"target": seed})["commit"]["status"], "COMMITTED")


class TestPush(Base):
    def test_silent_by_default(self):
        self.assertIsNone(self.s.check_push(self.sid, "Let's write the migration for the schema design now"))
        self.assertIsNone(self.s.check_push(self.sid, "ok"))

    def test_superseded_resurfacing(self):
        old = self.item("Use SQLite for storage")["item_id"]
        self.say(f"SUPERSEDE[{old}]: Use Postgres for storage")
        pkt = self.s.check_push(self.sid, "Let's use SQLite for storage here")
        self.assertEqual(pkt["reason"], "SUPERSEDED_RESURFACED")
        self.assertTrue(pkt["governing"])

    def test_constraint_about_to_be_ignored(self):
        pkt = self.s.check_push(self.sid, "I will call the network from the core to fetch the config")
        self.assertIn("CONSTRAINT_AT_RISK", pkt["reasons"])
        self.assertEqual(pkt["governing"][0]["text"], "Never call the network from the core")
        # saying the constraint back is not a violation
        self.assertIsNone(self.s.check_push(self.sid, "We must never call the network from the core, agreed"))

    def test_repeated_corrections(self):
        for i in range(3):
            self.say(f"DECISION: option number {i} for caching")
            tgt = self.item(f"option number {i} for caching")["item_id"]
            self.say(f"CORRECTION[{tgt}]: revised option {i} for caching")
        pkt = self.s.check_push(self.sid, "moving on to something else entirely")
        self.assertIn("REPEATED_CORRECTIONS", pkt["reasons"])

    def test_unresolved_becomes_relevant(self):
        pkt = self.s.check_push(self.sid, "Now deciding how snapshots should be pruned over time")
        self.assertIn("UNRESOLVED_RELEVANT", pkt["reasons"])

    def test_seed_becomes_relevant(self):
        pkt = self.s.check_push(self.sid, "We could render lineage as an interactive graph for reclamation")
        self.assertIn("SEED_RELEVANT", pkt["reasons"])

    def test_unintended_departure_from_intent(self):
        draft = ("Here is a detailed recipe for sourdough bread with hydration percentages, autolyse timing, "
                 "bulk fermentation temperatures, shaping technique and baking steam instructions")
        self.assertIn("INTENT_DEPARTURE", self.s.check_push(self.sid, draft)["reasons"])
        self.assertIsNone(self.s.check_push(self.sid, "bread recipe"))  # short drafts are never departures

    def test_packet_is_a_micro_reinjection(self):
        pkt = self.s.check_push(self.sid, "I will call the network from the core to fetch the config")
        self.assertLess(len(json.dumps(pkt)), 2500)
        text = render_push(pkt)
        self.assertIn("Never call the network", text)
        self.assertIn("Build a continuity tool", text)
        self.assertIn("designing the schema", text)

    def test_push_is_logged(self):
        self.s.check_push(self.sid, "I will call the network from the core to fetch the config")
        self.assertTrue(self.s.store.observations(self.sid, "PUSH"))
        self.assertGreaterEqual(self.s.metrics(self.sid)["push_interventions"], 1)


class TestConsumer(Base):
    def test_turn_gives_the_model_continuity_not_transcript_and_records_both_sides(self):
        seen = {}

        def llm(context, user):
            seen["ctx"], seen["user"] = context, user
            return "Working on the schema design now."
        for i in range(30):
            self.say(f"filler chatter {i} about unrelated lunch plans " * 5, "ASSISTANT_OUTPUT")
        res = Consumer(self.s, self.sid, llm).turn("which database do we use?")
        self.assertIn("Use SQLite for storage", seen["ctx"])
        self.assertIn("Build a continuity tool", seen["ctx"])
        self.assertNotIn("lunch", seen["ctx"])  # history was not replayed
        self.assertEqual([o.source_event_id is not None for o in res.observed], [True, True])
        types = [r["source_type"] for r in self.s.store.source_events(self.sid).values()][-2:]
        self.assertEqual(types, ["USER_INPUT", "ASSISTANT_OUTPUT"])

    def test_push_triggers_one_micro_reinjected_retry(self):
        calls = []

        def llm(context, user):
            calls.append(context)
            if "CONTINUITY NOTICE" in context:
                return "I will keep the core offline and read the config from a local file."
            return "I will call the network from the core to fetch the config."
        res = Consumer(self.s, self.sid, llm).turn("how do we load config?")
        self.assertTrue(res.corrected)
        self.assertEqual(len(calls), 2)
        self.assertIn("Never call the network", calls[1])
        self.assertEqual(res.reply, "I will keep the core offline and read the config from a local file.")
        last = list(self.s.store.source_events(self.sid).values())[-1]
        self.assertEqual(last["content"], res.reply)  # only the corrected reply is recorded

    def test_stubborn_model_is_not_looped_forever(self):
        calls = []
        res = Consumer(self.s, self.sid, lambda c, u: calls.append(1) or
                       "I will call the network from the core to fetch the config.").turn("config?")
        self.assertEqual(len(calls), 2)  # original + exactly one retry
        self.assertTrue(res.corrected)


class TestBranchItemsAndMetrics(Base):
    def test_branch_items_are_recorded_with_the_branch(self):
        self.say("BRANCH: evaluate vector search")
        self.say("DISCOVERY: embeddings add a model dependency")
        self.say("DECISION: Keep term overlap for V1 | no extra dependency")
        bid = next(iter(self.s.get_state(self.sid)["branches"]))
        br = self.s.get_state(self.sid)["branches"][bid]
        ids = {self.item("embeddings add a model dependency")["item_id"],
               self.item("Keep term overlap for V1")["item_id"]}
        self.assertEqual(set(br["item_ids"]), ids)
        self.say("END BRANCH: done")
        self.say("DECISION: Ship the schema first")
        later = self.item("Ship the schema first")
        self.assertNotIn("branch_id", later["content"])  # work after the branch belongs to the parent
        closed = self.s.store.load_model(self.sid).scalars["branches"][bid]
        self.assertEqual(set(closed["item_ids"]), ids)
        self.assertTrue(self.s.verify(self.sid)["ok"])

    def test_branch_items_survive_recovery(self):
        self.say("BRANCH: evaluate vector search")
        self.say("DISCOVERY: embeddings add a model dependency")
        expected = self.s.store.load_model(self.sid).scalars["branches"]
        self.s.store.db.execute("DELETE FROM living_state")
        self.assertEqual(self.s.load(self.sid).scalars["branches"], expected)  # load() heals from snapshot+events

    def test_platform_metrics_cover_15_2(self):
        old = self.item("Use SQLite for storage")["item_id"]
        self.say(f"CORRECTION[{old}]: Use Postgres for storage")
        self.s.observe(SourceEvent(content="INTENT: dup", source_event_id="src_x"), self.sid)
        self.s.observe(SourceEvent(content="INTENT: dup", source_event_id="src_x"), self.sid)
        self.s.store.db.execute("DELETE FROM living_state")
        self.s.get_state(self.sid)  # heals
        self.s.get_context(self.sid, "which database")
        m = self.s.metrics(self.sid)
        self.assertGreaterEqual(m["duplicates_prevented"], 1)
        self.assertGreaterEqual(m["recovery_operations"], 1)
        self.assertEqual(m["corrections"], 1)
        self.assertGreater(m["user_repair_burden_per_100_inputs"], 0)
        self.assertGreater(m["rework_rate"], 0)
        self.assertEqual(m["requests"], 1)


if __name__ == "__main__":
    unittest.main()
