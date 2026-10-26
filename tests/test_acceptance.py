"""Acceptance matrix (spec section 18) and invariant checks, one test per row.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from srt1 import SRT1, CallableProposer, SourceEvent  # noqa: E402
from srt1.models import StateDelta  # noqa: E402
from srt1.state import semantic_view  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.s = SRT1(snapshot_every=5)
        self.sid = self.s.create_stream("t")
        self.addCleanup(self.s.store.db.close)

    def say(self, text, source_type="USER_INPUT", **kw):
        return self.s.observe(SourceEvent(content=text, source_type=source_type, **kw), self.sid)

    def state(self):
        return self.s.get_state(self.sid)

    def version(self):
        return self.state()["state_version"]

    def items(self):
        return self.s.store.load_model(self.sid).items

    def by_text(self, text):
        return next(i for i in self.items().values() if i["content"].get("text") == text)

    def seed_stream(self):
        self.say("INTENT: Build a continuity tool for developing cognition")
        self.say("DECISION: Use SQLite for storage | simple and transactional | Postgres; files")
        self.say("CONSTRAINT: No network calls in the core")
        self.say("POSITION: designing the schema\nDIRECTION: persistence layer first")


class TestAcceptance(Base):
    def test_continuation_updates_state_without_structural_mutation(self):
        self.seed_stream()
        n_items, v = len(self.items()), self.version()
        r = self.say("Thanks, that all sounds good to me.")  # free prose: nothing to reflect on
        self.assertEqual(r.status, "NO_CHANGE")
        self.assertEqual(self.version(), v)
        r = self.say("POSITION: schema reviewed")
        self.assertEqual(r.status, "COMMITTED")
        self.assertEqual(self.version(), v + 1)
        self.assertEqual(len(self.items()), n_items)  # state moved, no new objects

    def test_refinement_keeps_lineage(self):
        self.seed_stream()
        first = self.state()["active_intent"]["item_id"]
        self.say("INTENT: Build a continuity tool that preserves seeds without diverting work")
        st = self.state()
        self.assertEqual(st["originating_intent"]["item_id"], first)
        self.assertNotEqual(st["active_intent"]["item_id"], first)
        self.assertEqual(len(st["intent_lineage_refs"]), 2)
        self.assertEqual(self.items()[first]["authority_status"], "HISTORICAL")
        rels = {(r["relationship_type"]) for r in self.s.store.load_model(self.sid).rels.values()}
        self.assertIn("REFINES", rels)

    def test_refinement_in_place_preserves_identity_and_old_wording(self):
        self.seed_stream()
        dec = self.by_text("Use SQLite for storage")
        r = self.s.submit_delta(StateDelta(
            stream_id=self.sid, expected_state_version=self.version(),
            source_event_ids=[self.say("noted").source_event_id], primary_classification="REFINEMENT",
            update_items=[{"item_id": dec["item_id"], "content": {"text": "Use SQLite (WAL mode) for storage"}}]))
        self.assertEqual(r.status, "COMMITTED")
        self.assertEqual(self.items()[dec["item_id"]]["content"]["text"], "Use SQLite (WAL mode) for storage")
        ev = self.s.store.events(self.sid)[-1]
        self.assertEqual(ev["delta"]["update_items"][0]["previous"]["text"], "Use SQLite for storage")

    def test_correction_becomes_authoritative(self):
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        r = self.say("CORRECTION[%s]: Use Postgres for storage" % old["item_id"])
        self.assertEqual(r.status, "COMMITTED")
        self.assertEqual(self.items()[old["item_id"]]["authority_status"], "REJECTED")  # old kept, not CURRENT
        corr = self.state()["applicable_corrections"][0]
        self.assertEqual(corr["text"], "Use Postgres for storage")
        self.assertEqual(corr["corrects"][0]["was"], "Use SQLite for storage")
        texts = [d["text"] for d in self.state()["governing_decisions"]]
        self.assertEqual(texts, ["Use Postgres for storage"])
        self.assertTrue(self.s.verify(self.sid)["ok"])

    def test_correction_by_non_user_requires_clarification(self):  # INV-004
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        r = self.say("CORRECTION[%s]: Use Postgres for storage" % old["item_id"], "ASSISTANT_OUTPUT")
        self.assertEqual(r.status, "REQUIRES_CLARIFICATION")
        self.assertEqual(self.items()[old["item_id"]]["authority_status"], "CURRENT")

    def test_supersession_keeps_history_true(self):
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        self.say("SUPERSEDE[%s]: Use SQLite behind a repository interface" % old["item_id"])
        it = self.items()[old["item_id"]]
        self.assertEqual(it["authority_status"], "SUPERSEDED")  # was current then
        self.assertEqual(it["content"]["text"], "Use SQLite for storage")  # never rewritten
        self.assertNotIn(old["item_id"], [d["item_id"] for d in self.state()["governing_decisions"]])
        self.assertEqual(self.state()["applicable_corrections"], [])  # supersession is not correction

    def test_superseded_cannot_silently_regain_authority(self):  # INV-005
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        self.say("SUPERSEDE[%s]: Use Postgres" % old["item_id"])
        for src in ("USER_INPUT", "ASSISTANT_OUTPUT"):
            r = self.s.submit_delta(StateDelta(
                stream_id=self.sid, expected_state_version=self.version(),
                source_event_ids=[self.say("irrelevant chatter", src).source_event_id],
                primary_classification="CONTINUATION",
                authority_changes=[{"item_id": old["item_id"], "to_status": "CURRENT"}]))
            self.assertEqual(r.status, "INVALID")
            self.assertIn("SILENT_PROMOTION", [x["code"] for x in r.reasons])

    def test_seed_captured_without_diversion(self):
        self.seed_stream()
        before = self.state()
        self.say("SEED: render lineage as a graph | could help reclamation")
        after = self.state()
        self.assertEqual(len(after["seeds"]), 1)
        for k in ("active_intent", "current_direction", "current_position", "trajectory"):
            self.assertEqual(before[k], after[k])
        ctx = after["seeds"][0]["detail"]["originating_context"]
        self.assertEqual(ctx["position"], "designing the schema")

    def test_seed_class_delta_cannot_change_intent(self):  # INV-006
        self.seed_stream()
        sid = self.say("noted").source_event_id
        r = self.s.submit_delta(StateDelta(
            stream_id=self.sid, expected_state_version=self.version(), source_event_ids=[sid],
            primary_classification="SEED",
            create_items=[{"ref": "$s", "item_type": "SEED", "authority_status": "PROVISIONAL", "content": "idea"},
                          {"ref": "$i", "item_type": "INTENT", "authority_status": "CURRENT", "content": "new goal"}],
            relationships_add=[{"from_item_id": "$i", "to_item_id": self.state()["active_intent"]["item_id"],
                                "relationship_type": "REFINES"}],
            intent_change={"kind": "REFINE", "item_id": "$i"}))
        self.assertEqual(r.status, "INVALID")

    def test_pivot_preserves_previous_stopping_point(self):
        self.seed_stream()
        old_intent = self.state()["active_intent"]["item_id"]
        r = self.say("PIVOT: Build a workforce credential registry instead")
        self.assertEqual(r.status, "COMMITTED")
        st = self.state()
        self.assertNotEqual(st["active_intent"]["item_id"], old_intent)
        piv = [t for t in st["trajectory"] if t["kind"] == "PIVOT"][0]
        self.assertEqual(piv["stopping_state"]["position"], "designing the schema")
        self.assertEqual(piv["from_intent"], old_intent)
        self.assertEqual(self.items()[old_intent]["authority_status"], "SUPERSEDED")
        self.assertEqual(st["originating_intent"]["item_id"], old_intent)

    def test_hedged_pivot_is_a_seed_not_a_pivot(self):
        self.seed_stream()
        intent = self.state()["active_intent"]["item_id"]
        r = self.say("PIVOT: maybe we should build a credential registry?")
        self.assertEqual(r.classification, "SEED")
        self.assertEqual(self.state()["active_intent"]["item_id"], intent)

    def test_pivot_from_non_user_needs_clarification(self):  # INV-039
        self.seed_stream()
        r = self.say("PIVOT: Switch the project to a registry", "ASSISTANT_OUTPUT")
        self.assertEqual(r.status, "REQUIRES_CLARIFICATION")

    def test_branch_preserves_parent_and_return_point(self):
        self.seed_stream()
        self.say("BRANCH: evaluate vector search for retrieval")
        self.say("POSITION: reading vector search papers\nFOCUS: retrieval experiment")
        st = self.state()
        (br,) = st["branches"].values()
        self.assertEqual(br["return_point"]["position"], "designing the schema")
        self.assertIn(br["branch_id"], st["continuation_point"]["open_branch_return_points"])
        self.say("END BRANCH: not worth it for V1")
        st = self.state()
        self.assertEqual(st["current_position"], "designing the schema")  # back without reconstruction
        self.assertEqual(st["branches"], {})
        self.assertEqual(self.s.store.events(self.sid)[-1]["event_type"], "MERGE")

    def test_ambiguity_is_not_committed(self):
        self.seed_stream()
        v = self.version()
        r = self.say("CORRECTION: the weather forecast is wrong")  # no grounded target
        self.assertEqual(r.status, "COMMITTED")  # preserved only as explicit uncertainty
        un = self.state()["unresolved_items"]
        self.assertTrue(any("Unmatched" in u["text"] for u in un))
        self.assertEqual(self.state()["governing_decisions"][0]["text"], "Use SQLite for storage")
        self.assertEqual(self.version(), v + 1)

    def test_inferred_destructive_change_asks_instead_of_rewriting(self):  # INV-019
        self.seed_stream()
        v = self.version()
        r = self.say("CORRECTION: switch the storage engine to something else")  # weak fuzzy match
        decs = self.state()["governing_decisions"]
        if r.status == "REQUIRES_CLARIFICATION":
            self.assertEqual(decs[0]["text"], "Use SQLite for storage")
            self.assertEqual(self.version(), v)
            (c,) = self.s.pending_clarifications(self.sid)
            r2 = self.s.answer_clarification(c["clarification_id"], accept=False)
            self.assertEqual(r2.status, "NO_CHANGE")
            self.assertEqual(self.version(), v)
        else:  # matcher found no grounded target and kept uncertainty instead
            self.assertTrue(any("Unmatched" in u["text"] for u in self.state()["unresolved_items"]))

    def test_clarification_accept_commits_with_explicit_evidence(self):
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        r = self.say("CORRECTION[%s]: Use Postgres" % old["item_id"], "ASSISTANT_OUTPUT")
        self.assertEqual(r.status, "REQUIRES_CLARIFICATION")
        r2 = self.s.answer_clarification(r.clarification_id, accept=True, note="yes, that's right")
        self.assertEqual(r2.status, "COMMITTED")
        self.assertEqual(self.items()[old["item_id"]]["authority_status"], "REJECTED")
        self.assertEqual(self.s.pending_clarifications(self.sid), [])

    def test_unresolved_survives_topic_movement(self):
        self.seed_stream()
        self.say("QUESTION: How should snapshots be pruned?")
        for i in range(6):
            self.say(f"DISCOVERY: unrelated finding number {i} about indexing")
        self.assertIn("How should snapshots be pruned?", [u["text"] for u in self.state()["unresolved_items"]])

    def test_unresolved_cannot_be_dropped_silently(self):  # INV-008
        self.seed_stream()
        self.say("QUESTION: How should snapshots be pruned?")
        q = self.by_text("How should snapshots be pruned?")
        r = self.s.submit_delta(StateDelta(
            stream_id=self.sid, expected_state_version=self.version(),
            source_event_ids=[self.say("whatever").source_event_id], primary_classification="CONTINUATION",
            authority_changes=[{"item_id": q["item_id"], "to_status": "HISTORICAL"}]))
        self.assertEqual(r.status, "INVALID")
        self.assertIn("UNRESOLVED_DROPPED", [x["code"] for x in r.reasons])
        self.say("RESOLVE[%s]: Prune by keeping every significant snapshot and 1-in-10 periodic ones" % q["item_id"])
        self.assertEqual(self.state()["unresolved_items"], [])
        self.assertEqual(self.items()[q["item_id"]]["lifecycle"], "RESOLVED")

    def test_reclamation_restores_originating_context(self):
        self.seed_stream()
        self.say("SEED: render lineage as a graph | could help reclamation")
        seed = self.state()["seeds"][0]["item_id"]
        self.say("DEFER[%s]: later" % seed)
        self.say("INTENT: Build a continuity tool, with a focus on recovery")  # intent moved on meanwhile
        self.say("POSITION: working on snapshots")
        pkg = self.s.reclaim(self.sid, seed)
        self.assertEqual(pkg["originating_context"]["position"], "designing the schema")
        self.assertFalse(pkg["original_intent_relationship"]["unchanged"])
        self.assertEqual(pkg["stopping_position"], "designing the schema")
        self.assertEqual(pkg["continuation_point"]["position"], "working on snapshots")
        self.assertGreaterEqual(len(pkg["development_history"]), 2)
        self.assertEqual(pkg["commit"]["status"], "COMMITTED")
        self.assertEqual(self.items()[seed]["lifecycle"], "RECLAIMED")
        self.assertEqual(self.s.store.events(self.sid)[-1]["event_type"], "RECLAIM")

    def test_currency_vs_history(self):
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        self.say("SUPERSEDE[%s]: Use Postgres" % old["item_id"])
        cur = {d["text"] for d in self.state()["governing_decisions"]}
        self.assertEqual(cur, {"Use Postgres"})
        self.assertIn(old["item_id"], self.items())  # exists historically, not current

    def test_provenance_chain(self):
        self.seed_stream()
        dec = self.by_text("Use SQLite for storage")
        ex = self.s.explain(self.sid, dec["item_id"])
        self.assertIn("Use SQLite for storage", ex["evidence"][0]["content"])
        self.assertEqual(ex["events"][0]["classification"], "CONTINUATION")
        for it in self.items().values():
            self.assertTrue(it["source_refs"])

    def test_self_inference_is_not_evidence(self):  # INV-025
        self.seed_stream()
        internal = self.s.store.add_source_event(SourceEvent(
            content="SRT1 inferred a decision", source_type="INTERNAL_CONTINUITY_EVENT", stream_id=self.sid))[0]
        r = self.s.submit_delta(StateDelta(
            stream_id=self.sid, expected_state_version=self.version(),
            source_event_ids=[internal["source_event_id"]], primary_classification="CONTINUATION",
            create_items=[{"item_type": "DECISION", "authority_status": "CURRENT", "content": "Use Redis"}]))
        self.assertEqual(r.status, "INVALID")
        self.assertIn("SELF_EVIDENCE", [x["code"] for x in r.reasons])

    def test_provisional_when_not_from_user(self):
        self.seed_stream()
        self.say("DECISION: Add an LRU cache", "ASSISTANT_OUTPUT")
        self.assertEqual(self.by_text("Add an LRU cache")["authority_status"], "PROVISIONAL")
        self.assertNotIn("Add an LRU cache", [d["text"] for d in self.state()["governing_decisions"]])
        self.say("CONFIRM: Add an LRU cache")
        self.assertEqual(self.by_text("Add an LRU cache")["authority_status"], "CURRENT")

    def test_historical_reconstruction(self):
        self.seed_stream()
        self.say("INTENT: Build a continuity tool that preserves seeds")
        self.say("PIVOT: Build a credential registry")
        v = self.s.verify(self.sid)
        self.assertTrue(v["ok"])
        lineage = self.state()["intent_lineage_refs"]
        self.assertEqual(len(lineage), 3)
        kinds = [e["event_type"] for e in self.s.store.events(self.sid)]
        self.assertEqual(kinds[0], "CREATE")
        self.assertIn("PIVOT", kinds)

    def test_user_repair_burden_no_reteaching(self):
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        self.say("CORRECTION[%s]: Use Postgres for storage" % old["item_id"])
        pkg = self.s.get_context(self.sid, "which database do we use for storage")
        self.assertEqual([d["text"] for d in pkg["governing_decisions"]], ["Use Postgres for storage"])
        self.assertEqual(pkg["applicable_corrections"][0]["corrects"][0]["was"], "Use SQLite for storage")


class TestReliability(Base):
    def test_source_is_immutable_and_preserved(self):
        r = self.say("INTENT: original wording of the intent")
        self.say("CORRECTION: something that matches nothing")
        row = self.s.store.get_source_event(r.source_event_id)
        self.assertEqual(row["content"], "INTENT: original wording of the intent")

    def test_idempotent_source_event(self):
        ev = SourceEvent(content="INTENT: build it", source_event_id="src_fixed")
        r1 = self.s.observe(ev, self.sid)
        v = self.version()
        r2 = self.s.observe(SourceEvent(content="INTENT: build it", source_event_id="src_fixed"), self.sid)
        self.assertEqual((r1.status, r2.status), ("COMMITTED", "DUPLICATE"))
        self.assertEqual(self.version(), v)
        self.assertEqual(len(self.items()), 1)

    def test_idempotent_external_id(self):
        self.say("INTENT: build it", external_source_id="msg-1")
        v = self.version()
        r = self.say("INTENT: build it", external_source_id="msg-1")
        self.assertEqual(r.status, "DUPLICATE")
        self.assertEqual(self.version(), v)

    def test_same_delta_twice_commits_once(self):
        sid = self.say("hello").source_event_id
        d = StateDelta(stream_id=self.sid, expected_state_version=0, source_event_ids=[sid],
                       primary_classification="CONTINUATION", focus={"current_focus": "x"})
        self.assertEqual(self.s.submit_delta(d).status, "COMMITTED")
        self.assertEqual(self.s.submit_delta(d).status, "DUPLICATE")
        self.assertEqual(self.version(), 1)

    def test_stale_delta_cannot_overwrite(self):
        self.seed_stream()
        stale_v = self.version()
        self.say("POSITION: moved on")
        sid = self.say("later").source_event_id
        r = self.s.submit_delta(StateDelta(
            stream_id=self.sid, expected_state_version=stale_v, source_event_ids=[sid],
            primary_classification="CONTINUATION", focus={"current_position": "OVERWRITE"}))
        self.assertEqual(r.status, "STALE")
        self.assertEqual(self.state()["current_position"], "moved on")

    def test_stale_proposal_is_rebased_by_observe(self):
        self.seed_stream()
        calls = {"n": 0}
        inner = self.s.proposer

        def racing(src, model):
            calls["n"] += 1
            out = inner.propose(src, model)
            if calls["n"] == 1:  # another writer commits between propose and commit
                other = self.s.store.add_source_event(SourceEvent(content="x", stream_id=self.sid))[0]
                self.s.submit_delta(StateDelta(
                    stream_id=self.sid, expected_state_version=model.version,
                    source_event_ids=[other["source_event_id"]], primary_classification="CONTINUATION",
                    focus={"current_focus": "interloper"}))
            return out
        self.s.proposer = CallableProposer(racing)
        r = self.say("POSITION: after the race")
        self.assertEqual(r.status, "COMMITTED")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.state()["current_position"], "after the race")
        self.assertEqual(self.state()["current_focus"], "interloper")

    def test_commit_is_atomic(self):
        self.seed_stream()
        snap = semantic_view(self.s.store.load_model(self.sid))
        v = self.version()

        def boom(stage):
            raise RuntimeError("disk on fire")
        self.s._fault_hook = boom
        r = self.say("DECISION: Use Redis")
        self.s._fault_hook = None
        self.assertEqual(r.status, "FAILED")
        self.assertEqual(self.version(), v)
        self.assertEqual(semantic_view(self.s.store.load_model(self.sid)), snap)
        row = self.s.store.get_source_event(r.source_event_id)
        self.assertEqual(row["processing_status"], "FAILED")  # evidence kept, flagged for retry
        r2 = self.s.retry(r.source_event_id)
        self.assertEqual(r2.status, "COMMITTED")
        self.assertEqual(self.version(), v + 1)

    def test_interpretation_failure_does_not_corrupt(self):
        self.seed_stream()
        v = self.version()
        self.s.proposer = CallableProposer(lambda s, m: 1 / 0)
        r = self.say("DECISION: anything")
        self.assertEqual(r.status, "FAILED")
        self.assertEqual(self.version(), v)
        self.assertEqual(self.s.store.get_source_event(r.source_event_id)["content"], "DECISION: anything")

    def test_snapshot_on_significant_events_not_just_counts(self):
        self.say("INTENT: build it")
        self.assertEqual(self.s.store.snapshots(self.sid), [])
        self.say("DECISION: Use SQLite")  # major decision
        self.assertEqual(len(self.s.store.snapshots(self.sid)), 1)
        self.assertEqual(self.s.store.snapshots(self.sid)[0]["reason"], "DECISION")
        for i in range(5):
            self.say(f"POSITION: p{i}")
        self.assertEqual(len(self.s.store.snapshots(self.sid)), 2)  # periodic after 5 events
        self.assertEqual(self.s.store.snapshots(self.sid)[-1]["reason"], "PERIODIC")

    def test_recovery_from_destroyed_projection(self):
        self.seed_stream()
        self.say("SEED: graph lineage view")
        self.say("QUESTION: pruning?")
        old = self.by_text("Use SQLite for storage")
        self.say("CORRECTION[%s]: Use Postgres for storage" % old["item_id"])
        self.say("POSITION: after snapshot")
        self.say("FOCUS: recovery work")
        expected = semantic_view(self.s.store.load_model(self.sid))
        db = self.s.store.db
        db.execute("DELETE FROM living_state")
        db.execute("UPDATE items SET authority_status='CURRENT', content='{\"text\":\"garbage\"}'")
        db.execute("DELETE FROM source_refs WHERE owner_kind='ITEM'")
        info = self.s.recover(self.sid)
        self.assertGreater(info["events_replayed"], 0)
        self.assertEqual(semantic_view(self.s.store.load_model(self.sid)), expected)
        self.assertTrue(self.s.verify(self.sid)["ok"])

    def test_auto_recovery_when_projection_missing(self):
        self.seed_stream()
        expected = self.state()
        self.s.store.db.execute("DELETE FROM living_state")
        self.assertEqual(self.state(), expected)  # get_state heals transparently
        self.assertTrue(self.s.store.observations(self.sid, "RECOVERY"))

    def test_recovery_survives_corrupt_snapshot(self):
        self.seed_stream()
        for i in range(6):
            self.say(f"POSITION: p{i}")
        expected = semantic_view(self.s.store.load_model(self.sid))
        self.s.store.db.execute("UPDATE snapshots SET payload='{\"stream_id\":\"x\"}'")
        self.s.store.db.execute("DELETE FROM living_state")
        self.s.recover(self.sid)
        self.assertEqual(semantic_view(self.s.store.load_model(self.sid)), expected)  # replayed from event 1

    def test_verify_detects_tampering(self):
        self.seed_stream()
        self.s.store.db.execute("UPDATE items SET authority_status='REJECTED' WHERE item_type='CONSTRAINT'")
        self.assertFalse(self.s.verify(self.sid)["ok"])

    def test_checkpoint_and_close_session(self):
        self.seed_stream()
        n = len(self.s.store.snapshots(self.sid))
        self.s.checkpoint(self.sid, "before refactor")
        info = self.s.close_session(self.sid)
        self.assertEqual(len(self.s.store.snapshots(self.sid)), n + 2)
        self.assertEqual(info["continuation_point"]["position"], "designing the schema")

    def test_observability_has_no_source_content(self):
        self.say("INTENT: super secret plan ZEBRA")
        blob = json.dumps(self.s.store.observations(self.sid))
        self.assertNotIn("ZEBRA", blob)
        kinds = {o["kind"] for o in self.s.store.observations(self.sid)}
        self.assertTrue({"SOURCE_RECEIVED", "VALIDATION", "COMMITTED"} <= kinds)


class TestPush(Base):
    def test_push_when_superseded_resurfaces(self):
        self.seed_stream()
        old = self.by_text("Use SQLite for storage")
        self.say("SUPERSEDE[%s]: Use Postgres for storage" % old["item_id"])
        pkt = self.s.check_push(self.sid, "Let's use SQLite for storage here")
        self.assertEqual(pkt["reason"], "SUPERSEDED_RESURFACED")
        self.assertIn("continuation_point", pkt)
        self.assertLess(len(json.dumps(pkt)), 2500)  # micro-reinjection, not a dump

    def test_push_is_conservative(self):
        self.seed_stream()
        self.assertIsNone(self.s.check_push(self.sid, "Let's write the migration for the schema"))


if __name__ == "__main__":
    unittest.main()
