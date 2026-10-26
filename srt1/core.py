"""SRT1 Core: the bounded interface over one Living Continuity State per stream (spec 8, 11, 13, 14).

observe -> reflect (propose) -> concretize -> validate -> atomic commit -> snapshot evaluation.
The proposer suggests meaning; only this module commits it, and only after deterministic validation.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .models import CLARIFY, INVALID, STALE, DeltaError, ProjectionCorrupt, SourceEvent, StateDelta, utcnow
from .reflection import Proposer, RuleProposer, apply_reclaim, depth_for, similarity
from .retrieval import ContextBuilder
from .state import (Model, affected_item_ids, apply_delta, brief, checksum, concretize, item_text, project,
                    semantic_view)
from .store import Store
from .validation import validate

STRONG_CLASSES = frozenset({"CORRECTION", "SUPERSESSION", "PIVOT", "BRANCH", "RECLAMATION", "COMPLETION"})
MAX_REBASE = 3


@dataclass
class Result:
    status: str                      # COMMITTED | NO_CHANGE | DUPLICATE | STALE | INVALID | REQUIRES_CLARIFICATION | FAILED
    source_event_id: str | None = None
    state_version: int | None = None
    event_id: str | None = None
    classification: str | None = None
    snapshot_id: str | None = None
    reasons: list = field(default_factory=list)
    clarification_id: str | None = None


class SRT1:
    def __init__(self, db_path: str = ":memory:", proposer: Proposer | None = None, snapshot_every: int = 5):
        self.store = Store(db_path)
        self.proposer = proposer or RuleProposer()
        self.snapshot_every = snapshot_every
        self.retriever = ContextBuilder(self.store)
        self._fault_hook = None  # test seam: called mid-commit to prove atomicity (INV-036)

    # ================================================================== streams
    def create_stream(self, title: str | None = None) -> str:
        return self.store.create_stream(title)

    def load(self, stream_id: str) -> Model:
        """Load the authoritative model, rebuilding a damaged projection from snapshot + events (11.2)."""
        try:
            return self.store.load_model(stream_id)
        except ProjectionCorrupt:
            self.recover(stream_id)
            return self.store.load_model(stream_id)

    # ================================================================== observe
    def observe(self, event: SourceEvent | dict | str, stream_id: str | None = None) -> Result:
        if isinstance(event, str):
            event = SourceEvent(content=event)
        elif isinstance(event, dict):
            event = SourceEvent(**event)
        if stream_id:
            event.stream_id = stream_id
        if not event.stream_id:
            raise ValueError("observe() needs a stream_id")
        row, created = self.store.add_source_event(event)  # evidence is preserved before any interpretation
        sid, seid = row["stream_id"], row["source_event_id"]
        self.store.log(sid, "SOURCE_RECEIVED", {"created": created, "source_type": row["source_type"]}, seid)
        if not created and row["processing_status"] != "RECEIVED":
            self.store.log(sid, "DUPLICATE_SOURCE", {"status": row["processing_status"]}, seid)
            return Result("DUPLICATE", seid)
        return self._process(sid, row)

    def retry(self, source_event_id: str) -> Result:
        """Reprocess a FAILED/DEFERRED source event (14.1). Already-committed events stay duplicates."""
        row = self.store.get_source_event(source_event_id)
        if row["processing_status"] == "PROCESSED":
            return Result("DUPLICATE", source_event_id)
        return self._process(row["stream_id"], row)

    def _process(self, sid: str, row: dict) -> Result:
        seid = row["source_event_id"]
        last = Result("FAILED", seid)
        for attempt in range(1, MAX_REBASE + 1):
            try:
                model = self.load(sid)
                proposal = self.proposer.propose(row, model)
            except Exception as e:  # interpretation failure never touches state (14.1)
                self.store.set_processing(seid, "FAILED", f"interpretation: {type(e).__name__}: {e}")
                self.store.log(sid, "FAILURE", {"stage": "interpret", "error": type(e).__name__}, seid)
                return Result("FAILED", seid, reasons=[str(e)])
            if proposal is None or proposal.get("primary_classification") == "NO_MATERIAL_CHANGE":
                self.store.set_processing(seid, "PROCESSED")
                self.store.log(sid, "CLASSIFIED", {"classification": "NO_MATERIAL_CHANGE", "depth": 0}, seid)
                return Result("NO_CHANGE", seid, model.version, classification="NO_MATERIAL_CHANGE")
            try:
                r = self.submit_delta(proposal)
            except Exception as e:  # commit failed and rolled back: last valid state stays authoritative
                self.store.set_processing(seid, "FAILED", f"commit: {type(e).__name__}: {e}")
                self.store.log(sid, "FAILURE", {"stage": "commit", "error": type(e).__name__}, seid)
                return Result("FAILED", seid, reasons=[str(e)])
            r.source_event_id = seid
            if r.status == STALE:
                self.store.log(sid, "REBASE", {"attempt": attempt}, seid)
                last = r
                continue  # regenerate against the newer state, then revalidate (8.5)
            return r
        self.store.set_processing(seid, "DEFERRED", "could not rebase")
        return last

    # ================================================================== delta submission / commit
    def submit_delta(self, delta: StateDelta | dict) -> Result:
        """Validate and atomically commit a proposed StateDelta. The only mutation path (INV-018)."""
        d = delta.to_dict() if isinstance(delta, StateDelta) else dict(delta)
        sid = d["stream_id"]
        self.load(sid)  # ensure a healthy projection before opening the write transaction
        committed = None
        with self.store.tx():
            if self.store.delta_committed(d["delta_id"]) or self.store.claimed(d["source_event_ids"]):
                self.store.log(sid, "DUPLICATE_DELTA", {"delta_id": d["delta_id"]})
                return Result("DUPLICATE", state_version=self.store.get_stream(sid)["current_state_version"])
            model = self.store.load_model(sid)
            try:
                concrete = concretize(model, d, utcnow())
                after = apply_delta(model, concrete)
            except (DeltaError, KeyError) as e:
                return self._reject(sid, d, INVALID, [{"code": "MALFORMED", "message": str(e)}])
            sources = self.store.source_events(sid)
            verdict = validate(model, after, concrete, sources)
            self.store.log(sid, "VALIDATION", {"delta_id": d["delta_id"], "verdict": verdict.status,
                                               "classification": d["primary_classification"],
                                               "codes": [v["code"] for v in verdict.reasons()]})
            if verdict.status == STALE:
                return Result(STALE, state_version=model.version, reasons=verdict.reasons())
            if verdict.status == INVALID:
                return self._reject(sid, d, INVALID, verdict.reasons())
            if verdict.status == CLARIFY:
                cid = self.store.add_clarification(sid, d["source_event_ids"][-1], d, verdict.reasons())
                for s in d["source_event_ids"]:
                    if sources[s]["source_type"] != "INTERNAL_CONTINUITY_EVENT":
                        self.store.set_processing(s, "DEFERRED", "awaiting clarification " + cid)
                self.store.log(sid, "AMBIGUITY", {"clarification_id": cid})
                return Result(CLARIFY, state_version=model.version, reasons=verdict.reasons(),
                              clarification_id=cid, classification=d["primary_classification"])
            # ---- atomic commit: items/relationships, event, living state, version (once), snapshot
            self.store.write_model_diff(model, after)
            if self._fault_hook:
                self._fault_hook("after_write")
            ev = {"event_id": concrete["event_id"], "stream_id": sid, "event_type": concrete["event_type"],
                  "source_event_ids": concrete["source_event_ids"], "affected_item_ids": affected_item_ids(concrete),
                  "previous_state_version": model.version, "resulting_state_version": after.version,
                  "confidence": concrete["confidence"], "rationale": concrete.get("rationale"),
                  "created_at": concrete["at"]}
            self.store.append_event(ev, concrete)
            for s in concrete["source_event_ids"]:
                self.store.set_processing(s, "PROCESSED")
            snap = self._evaluate_snapshot(after, concrete)
            self.store.log(sid, "COMMITTED", {"event_id": ev["event_id"], "version": after.version,
                                              "classification": d["primary_classification"],
                                              "depth": depth_for(concrete), "snapshot": bool(snap)})
            committed = Result("COMMITTED", state_version=after.version, event_id=ev["event_id"],
                               classification=d["primary_classification"], snapshot_id=snap)
        return committed

    def _reject(self, sid, d, status, reasons) -> Result:
        for s in d["source_event_ids"]:
            row = self.store.get_source_event(s)
            if row and row["source_type"] != "INTERNAL_CONTINUITY_EVENT":
                self.store.set_processing(s, "FAILED", json.dumps(reasons)[:2000])
        self.store.log(sid, "REJECTED", {"delta_id": d["delta_id"], "codes": [r["code"] for r in reasons]})
        return Result(status, reasons=reasons, classification=d["primary_classification"])

    # ================================================================== snapshots (11)
    def _snapshot_reason(self, after: Model, d: dict) -> str | None:
        cls, ic = d["primary_classification"], d.get("intent_change")
        if d.get("snapshot_recommendation"):
            return "RECOMMENDED"
        if cls in STRONG_CLASSES:
            return cls
        if ic and ic["kind"] != "ORIGIN":
            return "INTENT_CHANGE"
        for c in d.get("create_items", []):
            if c["item_type"] in ("DECISION", "DISCOVERY") and c["authority_status"] == "CURRENT":
                return c["item_type"]
        since = after.version - self.store.last_snapshot_version(after.stream_id)
        return "PERIODIC" if since >= self.snapshot_every else None

    def _evaluate_snapshot(self, after: Model, d: dict) -> str | None:
        reason = self._snapshot_reason(after, d)
        if not reason:
            return None
        return self._make_snapshot(after, "PERIODIC" if reason == "PERIODIC" else "SIGNIFICANCE", reason,
                                   [d["event_id"]])

    def _make_snapshot(self, model: Model, stype: str, reason: str, triggers: list) -> str:
        sid = self.store.add_snapshot(model, stype, triggers, reason, project(model)["continuation_point"])
        self.store.log(model.stream_id, "SNAPSHOT", {"snapshot_id": sid, "type": stype, "reason": reason,
                                                     "version": model.version})
        return sid

    def checkpoint(self, stream_id: str, reason: str | None = None) -> str:
        model = self.load(stream_id)
        with self.store.tx():
            return self._make_snapshot(model, "EXPLICIT", reason or "EXPLICIT", [])

    def close_session(self, stream_id: str) -> dict:
        """Preserve a resumable session boundary (13): snapshot + the state needed to resume."""
        model = self.load(stream_id)
        with self.store.tx():
            sid = self._make_snapshot(model, "SESSION_BOUNDARY", "SESSION_BOUNDARY", [])
        return {"snapshot_id": sid, "state_version": model.version,
                "continuation_point": project(model)["continuation_point"]}

    # ================================================================== recovery (11.2)
    def recover(self, stream_id: str) -> dict:
        """Rebuild projection and item rows from the latest *valid* snapshot plus subsequent events."""
        base, used = Model(stream_id), None
        for snap in reversed(self.store.snapshots(stream_id)):
            payload = json.loads(snap["payload"])
            if checksum(payload) == snap["checksum"]:
                base, used = Model.from_dict(payload), snap["snapshot_id"]
                break
        replayed = 0
        for ev in self.store.events(stream_id, base.version):
            if ev["previous_state_version"] != base.version:
                raise RuntimeError(f"event chain gap at v{base.version}: cannot recover {stream_id}")
            base = apply_delta(base, ev["delta"])
            replayed += 1
        self.store.replace_model(base)
        self.store.log(stream_id, "RECOVERY", {"snapshot": used, "events_replayed": replayed,
                                               "version": base.version})
        return {"snapshot_id": used, "events_replayed": replayed, "state_version": base.version}

    # ================================================================== read interface
    def get_state(self, stream_id: str, scope: list | None = None) -> dict:
        """Authoritative compact Living Continuity State; `scope` limits the returned sections."""
        proj = project(self.load(stream_id))
        if scope:
            keep = set(scope) | {"stream_id", "state_version"}
            proj = {k: v for k, v in proj.items() if k in keep}
        return proj

    def get_context(self, stream_id: str, task: str, **options) -> dict:
        model = self.load(stream_id)
        pkg = self.retriever.get_context(model, task, **options)
        self.store.log(stream_id, "CONTEXT", {"depth": pkg["retrieval_depth"], "complete": pkg["complete"],
                                              "tokens": pkg["token_report"]["package_tokens"]})
        with self.store.tx():
            self.retriever.record_metrics(stream_id, task, pkg)
        return pkg

    def metrics(self, stream_id: str | None = None) -> dict:
        return self.retriever.platform_metrics(stream_id)

    # ================================================================== reclamation (10.4)
    def reclaim(self, stream_id: str, target: str, commit: bool = True) -> dict:
        """Restore preserved cognition (seed, deferred/abandoned work or a branch) with its originating context."""
        model = self.load(stream_id)
        branch = next((b for k, b in model.scalars["branches"].items() if k == target or k.startswith(target)), None)
        item = next((i for k, i in model.items.items() if k == target or k.startswith(target)), None)
        if item is None and branch is None:
            raise KeyError(f"nothing to reclaim for {target}")
        events = self.store.events(stream_id)
        pkg: dict = {"state_version": model.version, "continuation_point": project(model)["continuation_point"],
                     "current_active_intent": brief(model.items[model.scalars["active_intent"]])
                     if model.scalars["active_intent"] else None}
        if item is not None:
            ctx = item["content"].get("originating_context") or {}
            hist = [{"version": e["resulting_state_version"], "event_type": e["event_type"],
                     "summary": e["delta"].get("summary", "")} for e in events
                    if item["item_id"] in e["affected_item_ids"]]
            rels = [{"relationship": r["relationship_type"], "from": r["from_item_id"], "to": r["to_item_id"]}
                    for r in model.rels.values() if item["item_id"] in (r["from_item_id"], r["to_item_id"])]
            txt = item_text(item)
            related = [brief(i) for i in model.items.values()
                       if i["item_type"] in ("DECISION", "CONSTRAINT") and i["authority_status"] == "CURRENT"
                       and similarity(txt, item_text(i)) > 0.2]
            pkg.update({
                "reclaimed_object": brief(item), "originating_context": ctx,
                "original_intent_relationship": {
                    "intent_then": ctx.get("active_intent_text"),
                    "intent_now": pkg["current_active_intent"]["text"] if pkg["current_active_intent"] else None,
                    "unchanged": ctx.get("active_intent_id") == model.scalars["active_intent"]},
                "development_history": hist, "stopping_position": ctx.get("position"),
                "relevant_decisions_and_constraints": related, "relationships": rels,
                "source_event_ids": sorted({r["source_event_id"] for r in item["source_refs"]})})
        if branch is not None:
            pkg.update({"reclaimed_branch": branch, "stopping_position": branch["return_point"].get("position"),
                        "originating_context": branch["return_point"]})
        if commit and item is not None:
            pkg["commit"] = self._commit_reclaim(model, item)
        return pkg

    def _commit_reclaim(self, model: Model, item: dict) -> dict:
        """A reclaim mutation is a validated internal continuity event (INV-016), not free-floating state change."""
        row, _ = self.store.add_source_event(SourceEvent(
            content=f"reclaim {item['item_id']}", source_type="INTERNAL_CONTINUITY_EVENT",
            stream_id=model.stream_id))
        d = StateDelta(stream_id=model.stream_id, expected_state_version=model.version,
                       source_event_ids=[row["source_event_id"]], primary_classification="RECLAMATION",
                       confidence="EXPLICIT", summary=f"reclaimed {item['item_type'].lower()}",
                       rationale="explicit reclaim()")
        apply_reclaim(d, item)
        r = self.submit_delta(d)
        return {"status": r.status, "state_version": r.state_version, "reasons": r.reasons}

    # ================================================================== clarification (7.3, INV-019)
    def pending_clarifications(self, stream_id: str) -> list:
        return self.store.open_clarifications(stream_id)

    def answer_clarification(self, clarification_id: str, accept: bool, note: str = "") -> Result:
        """The user answers an ambiguity. Acceptance is new EXPLICIT evidence; rejection preserves state."""
        c = self.store.get_clarification(clarification_id)
        sid = c["stream_id"]
        answer = SourceEvent(content=note or ("yes" if accept else "no"), source_type="USER_INPUT", stream_id=sid)
        row, _ = self.store.add_source_event(answer)
        if not accept:
            self.store.set_clarification_status(clarification_id, "REJECTED")
            self.store.set_processing(row["source_event_id"], "PROCESSED")
            for s in c["delta"]["source_event_ids"]:
                self.store.set_processing(s, "PROCESSED")
            return Result("NO_CHANGE", row["source_event_id"], self.store.get_stream(sid)["current_state_version"])
        d = dict(c["delta"])
        d.update(confidence="EXPLICIT", expected_state_version=self.load(sid).version,
                 source_event_ids=list(d["source_event_ids"]) + [row["source_event_id"]])
        for ci in d.get("create_items", []):
            if ci.get("confidence") in ("INFERRED", "UNCERTAIN"):
                ci["confidence"] = "EXPLICIT"
        r = self.submit_delta(d)
        if r.status == "COMMITTED":
            self.store.set_clarification_status(clarification_id, "ACCEPTED")
        return r

    # ================================================================== verify / audit (14.2)
    def explain(self, stream_id: str, item_id: str) -> dict:
        """Why does SRT1 believe this? current item -> events/lineage -> source refs -> original evidence."""
        model = self.load(stream_id)
        it = model.items[item_id]
        sources = self.store.source_events(stream_id)
        return {
            "item": brief(it),
            "events": [{"version": e["resulting_state_version"], "event_type": e["event_type"],
                        "classification": e["classification"], "source_event_ids": e["source_event_ids"]}
                       for e in self.store.events(stream_id) if item_id in e["affected_item_ids"]],
            "lineage": [{"relationship": r["relationship_type"], "from": r["from_item_id"], "to": r["to_item_id"]}
                        for r in model.rels.values() if item_id in (r["from_item_id"], r["to_item_id"])],
            "evidence": [{"source_event_id": r["source_event_id"], "ref_type": r["ref_type"],
                          "source_type": sources[r["source_event_id"]]["source_type"],
                          "content": sources[r["source_event_id"]]["content"]} for r in it["source_refs"]]}

    def verify(self, stream_id: str, target: str | None = None) -> dict:
        """Validate the stored representation against deeper evidence: replay equivalence + audit trail."""
        model = self.store.load_model(stream_id)
        problems = []
        replay, prev = Model(stream_id), 0
        for ev in self.store.events(stream_id):
            if ev["previous_state_version"] != prev:
                problems.append(f"event chain gap before v{ev['resulting_state_version']}")
            replay, prev = apply_delta(replay, ev["delta"]), ev["resulting_state_version"]
        if semantic_view(replay) != semantic_view(model):
            problems.append("stored state differs from replay of the event log")
        sources = self.store.source_events(stream_id)
        for it in ([model.items[target]] if target and target in model.items else model.items.values()):
            if it["authority_status"] in ("CURRENT", "PROVISIONAL", "UNRESOLVED") and not it["source_refs"]:
                problems.append(f"{it['item_id']}: no provenance")
            for r in it["source_refs"]:
                if r["source_event_id"] not in sources:
                    problems.append(f"{it['item_id']}: dangling source {r['source_event_id']}")
            if it["authority_status"] == "CURRENT" and any(
                    r["relationship_type"] in ("CORRECTS", "SUPERSEDES") and r["to_item_id"] == it["item_id"]
                    for r in model.rels.values()):
                problems.append(f"{it['item_id']}: CURRENT despite being corrected/superseded")
        out = {"ok": not problems, "problems": problems, "state_version": model.version}
        if target and target in model.items:
            out["explanation"] = self.explain(stream_id, target)
        return out

    # ================================================================== push continuity (13.2, 13.3)
    def check_push(self, stream_id: str, draft: str) -> dict | None:
        """Conservative: return a micro-reinjection packet only when superseded material resurfaces as current,
        or governing corrections/constraints are being contradicted by repetition. Otherwise None."""
        model = self.load(stream_id)
        proj = project(model)
        best_cur = max((similarity(draft, item_text(i)) for i in model.items.values()
                        if i["authority_status"] == "CURRENT"), default=0.0)
        for it in model.items.values():
            if it["authority_status"] in ("SUPERSEDED", "REJECTED") and it["item_type"] != "INTENT":
                s = similarity(draft, item_text(it))
                if s >= 0.6 and s > best_cur:
                    gov = [b for b in proj["applicable_corrections"]
                           if any(c["item_id"] == it["item_id"] for c in b.get("corrects", []))]
                    return {"reason": "SUPERSEDED_RESURFACED", "resurfaced": brief(it, False),
                            "governing": gov or proj["governing_decisions"][-1:],
                            "active_intent": proj["active_intent"], "current_focus": proj["current_focus"],
                            "continuation_point": proj["continuation_point"]}
        recent = proj["recent_significant_changes"]
        if sum(1 for r in recent[-6:] if r["classification"] == "CORRECTION") >= 3:
            return {"reason": "REPEATED_CORRECTIONS", "active_intent": proj["active_intent"],
                    "governing": proj["applicable_corrections"][-3:],
                    "continuation_point": proj["continuation_point"]}
        return None
