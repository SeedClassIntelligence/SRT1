"""Deterministic StateDelta validation and the invariant registry (spec 8.2, 8.3, 17).

The model proposes meaning; this module decides whether continuity may change. It validates the
*post-state* of a concretized delta (computed by state.apply_delta) against the pre-state, so every
rule below is a check on what continuity would become, not on how the proposal was worded.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (AUTHORITY, CLASSIFICATIONS, CLOSING_LIFECYCLE, CONFIDENCE, FIRM_CONFIDENCE, INVALID,
                     ITEM_TYPES, LINEAGE_RELS, NON_CURRENT, REL_TYPES, STALE, VALID, CLARIFY, REF_TYPES)
from .state import Model, affected_item_ids

# Invariant registry: id -> short statement (full text lives in the specification, section 17).
INVARIANTS = {
    "SRT1-INV-004": "Explicit user corrections supersede conflicting earlier system interpretations.",
    "SRT1-INV-005": "Superseded information cannot silently regain current authority.",
    "SRT1-INV-006": "Seed capture cannot automatically alter active intent or trajectory.",
    "SRT1-INV-007": "Intentional pivots preserve the previous stopping state.",
    "SRT1-INV-008": "Unresolved cognition persists until explicitly closed.",
    "SRT1-INV-009": "Material current-state objects maintain provenance.",
    "SRT1-INV-016": "Every mutation originates from a source event or validated internal event.",
    "SRT1-INV-018": "Continuity-significant mutations are validated State Deltas.",
    "SRT1-INV-019": "Uncertain interpretation cannot destructively modify established continuity.",
    "SRT1-INV-020": "Correction is distinguished from historical supersession.",
    "SRT1-INV-021": "Seed capture is distinguished from trajectory change.",
    "SRT1-INV-022": "A return point is preserved for intentional branches.",
    "SRT1-INV-025": "SRT1's own inference never becomes independent source evidence.",
    "SRT1-INV-035": "Stale mutations cannot overwrite newer state without revalidation.",
    "SRT1-INV-039": "Intentional user direction changes outrank the previous trajectory.",
}


@dataclass
class Violation:
    code: str
    message: str
    invariant: str | None = None

    def to_dict(self):
        return {"code": self.code, "message": self.message, "invariant": self.invariant}


@dataclass
class Verdict:
    status: str
    violations: list = field(default_factory=list)   # hard failures
    clarifications: list = field(default_factory=list)  # reasons a human/user answer is needed

    def reasons(self):
        return [v.to_dict() for v in self.violations + self.clarifications]


def validate(before: Model, after: Model, d: dict, sources: dict) -> Verdict:
    """`d` is a concretized delta; `sources` maps source_event_id -> source row for the stream."""
    bad: list[Violation] = []
    ask: list[Violation] = []

    def hard(code, msg, inv=None):
        bad.append(Violation(code, msg, inv))

    def clarify(code, msg, inv=None):
        ask.append(Violation(code, msg, inv))

    if d["stream_id"] != before.stream_id:
        hard("STREAM_MISMATCH", "delta targets a different stream")
        return Verdict(INVALID, bad)
    if d["expected_state_version"] != before.version:
        return Verdict(STALE, [Violation(
            "STALE_VERSION", f"delta built on v{d['expected_state_version']}, state is v{before.version}",
            "SRT1-INV-035")])

    cls, conf = d["primary_classification"], d.get("confidence")
    if cls not in CLASSIFICATIONS:
        hard("BAD_CLASSIFICATION", cls)
    if conf not in CONFIDENCE:
        hard("BAD_CONFIDENCE", str(conf))
    ops = ("create_items", "update_items", "authority_changes", "relationships_add", "relationships_remove",
           "resolve", "reopen", "seed_actions", "branch_actions")
    has_ops = any(d.get(k) for k in ops) or d.get("intent_change") or d.get("focus") or d.get("trajectory")
    if cls == "NO_MATERIAL_CHANGE":
        if has_ops:
            hard("NOOP_WITH_OPS", "NO_MATERIAL_CHANGE delta carries mutations")
        return Verdict(INVALID if bad else VALID, bad)

    # --- provenance of the delta itself (INV-016)
    sids = d.get("source_event_ids") or []
    if not sids:
        hard("NO_SOURCE", "delta cites no source event", "SRT1-INV-016")
    src_rows = []
    for s in sids:
        if s not in sources:
            hard("UNKNOWN_SOURCE", f"source event {s} not in stream", "SRT1-INV-016")
        else:
            src_rows.append(sources[s])
    has_user = any(r["source_type"] == "USER_INPUT" for r in src_rows)

    # --- vocabularies and referential integrity
    new_ids = {c["item_id"] for c in d.get("create_items", [])}
    known = set(before.items) | new_ids
    for c in d.get("create_items", []):
        if c["item_type"] not in ITEM_TYPES:
            hard("BAD_ITEM_TYPE", c["item_type"])
        if c["authority_status"] not in AUTHORITY:
            hard("BAD_AUTHORITY", c["authority_status"])
        if c["confidence"] not in CONFIDENCE:
            hard("BAD_CONFIDENCE", c["confidence"])
        if c["item_id"] in before.items:
            hard("DUP_ITEM", f"item {c['item_id']} already exists")
    for key in ("update_items", "authority_changes", "seed_actions"):
        for x in d.get(key, []):
            if x["item_id"] not in known:
                hard("DANGLING", f"{key}: unknown item {x['item_id']}")
    for k in ("resolve", "reopen"):
        for iid in d.get(k, []):
            if iid not in known:
                hard("DANGLING", f"{k}: unknown item {iid}")
    for a in d.get("authority_changes", []):
        if a["to_status"] not in AUTHORITY:
            hard("BAD_AUTHORITY", a["to_status"])
    for r in d.get("relationships_add", []):
        if r["relationship_type"] not in REL_TYPES:
            hard("BAD_REL_TYPE", f"{r['relationship_type']} not in controlled vocabulary")
        if r["from_item_id"] not in known or r["to_item_id"] not in known:
            hard("DANGLING", f"relationship endpoint unknown ({r['from_item_id']}->{r['to_item_id']})")
        if r["from_item_id"] == r["to_item_id"]:
            hard("SELF_REL", "relationship to self")
    for rid in d.get("relationships_remove", []):
        rel = before.rels.get(rid)
        if rel is None:
            hard("DANGLING", f"relationship {rid} unknown")
        elif rel["relationship_type"] in LINEAGE_RELS:
            hard("LINEAGE_REMOVAL", f"{rel['relationship_type']} lineage cannot be removed", "SRT1-INV-005")
    if bad:
        return Verdict(INVALID, bad)

    # --- evidence rules on refs (INV-009, INV-025)
    def check_refs(refs, label, needs_external):
        if not refs:
            hard("NO_PROVENANCE", f"{label} has no source reference", "SRT1-INV-009")
            return
        external = False
        for r in refs:
            row = sources.get(r["source_event_id"])
            if row is None:
                hard("UNKNOWN_SOURCE", f"{label} cites unknown source {r['source_event_id']}", "SRT1-INV-009")
                continue
            if r["ref_type"] not in REF_TYPES:
                hard("BAD_REF_TYPE", r["ref_type"])
            if row["source_type"] == "INTERNAL_CONTINUITY_EVENT":
                if r["ref_type"] == "DIRECT":
                    hard("SELF_EVIDENCE", f"{label} treats an internal inference as direct evidence",
                         "SRT1-INV-025")
            else:
                external = True
        if needs_external and not external:
            hard("SELF_EVIDENCE", f"{label} has no evidence outside SRT1's own inference", "SRT1-INV-025")

    for c in d.get("create_items", []):
        check_refs(c["source_refs"], f"item {c['item_id']}", c["authority_status"] == "CURRENT")
    for r in d.get("relationships_add", []):
        check_refs(r["source_refs"], f"relationship {r['relationship_id']}", False)

    # --- authority transitions on pre-existing items
    destructive = False
    for iid, old in before.items.items():
        new = after.items[iid]
        if new["authority_status"] == old["authority_status"] and new["lifecycle"] == old["lifecycle"] \
                and new["content"] == old["content"]:
            continue
        o, n = old["authority_status"], new["authority_status"]
        if old["item_type"] == "INTENT":
            ok = bool(d.get("intent_change")) and iid == before.scalars["active_intent"] \
                and d["intent_change"]["item_id"] != iid and old["content"] == new["content"]
            if not ok:
                hard("INTENT_PROTECTED", "intent may change only through intent_change", "SRT1-INV-018")
        if o in NON_CURRENT and n not in NON_CURRENT:
            if not (cls in ("RECLAMATION", "REOPENING") and has_user):
                hard("SILENT_PROMOTION", f"{iid} regains {n} authority without user reclamation",
                     "SRT1-INV-005")
        if o == "UNRESOLVED" and n != "UNRESOLVED" and new["lifecycle"] not in CLOSING_LIFECYCLE:
            hard("UNRESOLVED_DROPPED", f"{iid} left UNRESOLVED without an explicit closing lifecycle",
                 "SRT1-INV-008")
        if o == "CURRENT" and n != "CURRENT":
            destructive = True

    # --- relationship post-conditions
    new_rels = d.get("relationships_add", [])
    by_type: dict = {}
    for r in new_rels:
        by_type.setdefault(r["relationship_type"], []).append(r)
        to_new = after.items[r["to_item_id"]]
        frm = after.items[r["from_item_id"]]
        if r["relationship_type"] in ("CORRECTS", "SUPERSEDES") and to_new["authority_status"] == "CURRENT":
            hard("CONFLICTING_CURRENT", f"{r['relationship_type']} target {r['to_item_id']} is still CURRENT",
                 "SRT1-INV-004")
        if r["relationship_type"] in ("CORRECTS", "SUPERSEDES") and frm["authority_status"] in NON_CURRENT:
            hard("DEAD_REPLACEMENT", "replacement item is itself non-current", "SRT1-INV-005")
        if r["relationship_type"] == "CONTRADICTS" and to_new["authority_status"] == "CURRENT" \
                and frm["authority_status"] == "CURRENT":
            hard("CONFLICTING_CURRENT", "two CURRENT items contradict each other; one must be provisional "
                 "or non-current", "SRT1-INV-010")
    if "CORRECTS" in by_type and cls != "CORRECTION":
        hard("WRONG_CLASS", "CORRECTS relationship requires CORRECTION classification", "SRT1-INV-020")
    if "SUPERSEDES" in by_type and cls not in ("SUPERSESSION", "PIVOT"):
        hard("WRONG_CLASS", "SUPERSEDES relationship requires SUPERSESSION or PIVOT", "SRT1-INV-020")

    ic = d.get("intent_change")
    need = {
        "CORRECTION": lambda: "CORRECTS" in by_type,
        "SUPERSESSION": lambda: "SUPERSEDES" in by_type,
        "CONTRADICTION": lambda: "CONTRADICTS" in by_type,
        "RESOLUTION": lambda: bool(d.get("resolve")),
        "REOPENING": lambda: bool(d.get("reopen")),
        "BRANCH": lambda: bool(d.get("branch_actions")),
        "PIVOT": lambda: bool(ic and ic["kind"] == "PIVOT"),
        "SEED": lambda: any(c["item_type"] == "SEED" for c in d.get("create_items", [])),
        "RECLAMATION": lambda: any(s["action"] == "RECLAIM" for s in d.get("seed_actions", []))
        or bool(d.get("reopen")) or any(a.get("lifecycle") == "OPEN" for a in d.get("authority_changes", []))
        or any(b["action"] == "RECLAIM" for b in d.get("branch_actions", [])),
        "DEFERMENT": lambda: any(s["action"] == "DEFER" for s in d.get("seed_actions", []))
        or any(a.get("lifecycle") == "DEFERRED" for a in d.get("authority_changes", [])),
        "ABANDONMENT": lambda: any(s["action"] == "DISCARD" for s in d.get("seed_actions", []))
        or any(a.get("lifecycle") == "ABANDONED" for a in d.get("authority_changes", [])),
        "REFINEMENT": lambda: bool({"REFINES"} & set(by_type)) or bool(d.get("update_items")),
        "EXPANSION": lambda: bool({"EXPANDS"} & set(by_type)) or bool(d.get("update_items")),
        "CONTRACTION": lambda: bool({"NARROWS"} & set(by_type)) or bool(d.get("update_items")),
    }
    if cls in need and not need[cls]():
        hard("CLASS_WITHOUT_EFFECT", f"{cls} delta has none of the mutations that define {cls}", "SRT1-INV-020")

    # --- intent rules (INV-018, INV-039)
    if ic:
        kind = ic["kind"]
        new_intent = after.items.get(ic["item_id"])
        if new_intent is None or new_intent["item_type"] != "INTENT":
            hard("BAD_INTENT", "intent_change must point to an INTENT item")
        elif kind == "ORIGIN":
            if before.scalars["originating_intent"]:
                hard("BAD_INTENT", "originating intent already exists")
        else:
            prev = before.scalars["active_intent"]
            relmap = {"REFINE": "REFINES", "EXPAND": "EXPANDS", "NARROW": "NARROWS", "PIVOT": "SUPERSEDES"}
            if kind not in relmap:
                hard("BAD_INTENT", f"unknown intent_change kind {kind}")
            elif prev is None:
                hard("BAD_INTENT", f"{kind} without an active intent")
            elif not any(r["from_item_id"] == ic["item_id"] and r["to_item_id"] == prev
                         for r in by_type.get(relmap[kind], [])):
                hard("LINEAGE_REQUIRED", f"{kind} must link the new intent to the previous one "
                     f"with {relmap[kind]}", "SRT1-INV-002")
            if kind == "PIVOT" and cls != "PIVOT":
                hard("WRONG_CLASS", "PIVOT intent change requires PIVOT classification", "SRT1-INV-021")
            if kind in ("REFINE", "EXPAND", "NARROW") and cls not in (
                    {"REFINE": "REFINEMENT", "EXPAND": "EXPANSION", "NARROW": "CONTRACTION"}[kind],):
                hard("WRONG_CLASS", f"{kind} requires matching classification")
        if not has_user:
            clarify("INTENT_NOT_USER", "intent changes require the user's own input", "SRT1-INV-039")
        if kind == "PIVOT" and conf not in FIRM_CONFIDENCE:
            clarify("PIVOT_UNCERTAIN", "pivot vs seed is uncertain; preserve the idea as a seed until "
                    "evidence supports a pivot", "SRT1-INV-021")
        if kind != "ORIGIN" and conf not in FIRM_CONFIDENCE:
            clarify("DESTRUCTIVE_UNCERTAIN", "intent change on uncertain interpretation", "SRT1-INV-019")

    # --- seeds (INV-006, INV-021)
    for c in d.get("create_items", []):
        if c["item_type"] == "SEED" and c["authority_status"] != "PROVISIONAL":
            hard("SEED_AUTHORITY", "a seed is potential, never CURRENT authority", "SRT1-INV-006")
    if cls == "SEED":
        if ic or d.get("branch_actions") or d.get("trajectory") or "current_direction" in (d.get("focus") or {}):
            hard("SEED_STEALS_TRAJECTORY", "seed capture may not change intent, branch or trajectory",
                 "SRT1-INV-006")

    # --- branches (INV-022)
    for b in d.get("branch_actions", []):
        if b["action"] == "OPEN":
            sc = before.scalars
            if not (sc["active_intent"] or sc["current_position"] or sc["current_focus"]):
                hard("NO_RETURN_POINT", "branch needs a parent context to return to", "SRT1-INV-022")
            if not b.get("purpose"):
                hard("BRANCH_PURPOSE", "branch needs a purpose")
        elif b["action"] == "CLOSE":
            br = before.scalars["branches"].get(b.get("branch_id"))
            if br is None or br["status"] != "OPEN":
                hard("BAD_BRANCH", f"branch {b.get('branch_id')} is not open")
        else:
            hard("BAD_BRANCH", f"unknown branch action {b['action']}")

    # --- correction precedence (INV-004) and destructive uncertainty (INV-019)
    if cls == "CORRECTION" and not has_user:
        clarify("CORRECTION_NOT_USER", "only the user's explicit correction outranks established state",
                "SRT1-INV-004")
    if destructive and conf not in FIRM_CONFIDENCE:
        clarify("DESTRUCTIVE_UNCERTAIN", "demotes CURRENT state on INFERRED/UNCERTAIN interpretation",
                "SRT1-INV-019")
    if after.version != before.version + 1:
        hard("VERSION", "delta must advance the version exactly once")

    if bad:
        return Verdict(INVALID, bad, ask)
    if ask:
        return Verdict(CLARIFY, [], ask)
    return Verdict(VALID)
