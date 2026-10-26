"""Living Continuity State: model, deterministic delta application, projection (spec 6, 8.4).

Everything here is pure: apply_delta(model, delta) never touches storage, which is what lets
recovery replay committed events (11.2) and lets the validator inspect the post-state before commit.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field

from .models import CLASS_EVENT, ITEM_TYPES, LIVE_SEED, DeltaError, new_id

SCALAR_DEFAULTS = {
    "originating_intent": None, "active_intent": None, "intent_lineage_refs": [],
    "current_focus": "", "current_position": "", "current_direction": "", "current_understanding": "",
    "branches": {}, "trajectory": [], "recent_significant_changes": [],
}
FOCUS_KEYS = ("current_focus", "current_position", "current_direction", "current_understanding")
RECENT_LIMIT = 10
SEED_ACTION_LIFECYCLE = {"DEFER": "DEFERRED", "RECLAIM": "RECLAIMED", "DEVELOP": "DEVELOPING",
                         "DISCARD": "DISCARDED", "MERGE": "MERGED"}


@dataclass
class Model:
    stream_id: str
    version: int = 0
    items: dict = field(default_factory=dict)
    rels: dict = field(default_factory=dict)
    scalars: dict = field(default_factory=lambda: copy.deepcopy(SCALAR_DEFAULTS))

    def copy(self) -> "Model":
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return {"stream_id": self.stream_id, "version": self.version, "items": self.items,
                "rels": self.rels, "scalars": self.scalars}

    @classmethod
    def from_dict(cls, d: dict) -> "Model":
        return cls(d["stream_id"], d["version"], copy.deepcopy(d["items"]), copy.deepcopy(d["rels"]),
                   copy.deepcopy(d["scalars"]))


def checksum(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def est_tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return math.ceil(len(text) / 4)


def item_text(item: dict) -> str:
    c = item["content"]
    return c.get("text", "") if isinstance(c, dict) else str(c)


# --------------------------------------------------------------------------- concretize

def event_type_for(d: dict) -> str:
    ic = d.get("intent_change") or {}
    if ic.get("kind") == "ORIGIN":
        return "CREATE"
    acts = d.get("branch_actions") or []
    if acts and all(a["action"] == "CLOSE" for a in acts):
        return "MERGE"
    return CLASS_EVENT.get(d["primary_classification"], "CONTINUE")


def concretize(model: Model, delta: dict, at: str) -> dict:
    """Assign ids, resolve "$ref" keys, default provenance, capture originating context."""
    d = copy.deepcopy(delta)
    src = list(d.get("source_event_ids") or [])
    refmap: dict = {}

    def norm_content(c):
        return {"text": c} if isinstance(c, str) else dict(c or {})

    def norm_refs(refs):
        out = []
        for r in (refs if refs is not None else ([{"source_event_id": src[0]}] if src else [])):
            r = dict(r)
            r.setdefault("source_ref_id", new_id("sr"))
            r.setdefault("ref_type", "DIRECT")
            r.setdefault("location", None)
            out.append(r)
        return out

    def ref(x):
        if isinstance(x, str) and x.startswith("$"):
            if x not in refmap:
                raise DeltaError(f"unresolved reference {x}")
            return refmap[x]
        return x

    for ci in d.get("create_items", []):
        ci["item_id"] = ci.get("item_id") or new_id("itm")
        if ci.get("ref"):
            refmap[ci["ref"]] = ci["item_id"]
    s = model.scalars
    for ci in d.get("create_items", []):
        ci["content"] = norm_content(ci.get("content"))
        ci.setdefault("authority_status", "PROVISIONAL")
        ci.setdefault("confidence", d.get("confidence", "EXPLICIT"))
        ci.setdefault("lifecycle", {"SEED": "CAPTURED", "QUESTION": "OPEN", "UNRESOLVED": "OPEN",
                                    "ASSUMPTION": "OPEN"}.get(ci.get("item_type"), "ACTIVE"))
        ci["source_refs"] = norm_refs(ci.get("source_refs"))
        if ci.get("item_type") == "SEED":
            ai = model.items.get(s["active_intent"]) if s["active_intent"] else None
            ci["content"].setdefault("originating_context", {
                "focus": s["current_focus"], "position": s["current_position"],
                "direction": s["current_direction"], "active_intent_id": s["active_intent"],
                "active_intent_text": item_text(ai) if ai else "", "state_version": model.version,
                "trigger_source_event_ids": src})
    for u in d.get("update_items", []):
        u["item_id"] = ref(u["item_id"])
        old = model.items.get(u["item_id"])
        u["content"] = norm_content(u.get("content"))
        u["previous"] = copy.deepcopy(old["content"]) if old else None
    for a in d.get("authority_changes", []):
        a["item_id"] = ref(a["item_id"])
    for r in d.get("relationships_add", []):
        r["relationship_id"] = r.get("relationship_id") or new_id("rel")
        r["from_item_id"], r["to_item_id"] = ref(r["from_item_id"]), ref(r["to_item_id"])
        r["source_refs"] = norm_refs(r.get("source_refs"))
    d["resolve"] = [ref(x) for x in d.get("resolve", [])]
    d["reopen"] = [ref(x) for x in d.get("reopen", [])]
    if d.get("intent_change"):
        d["intent_change"]["item_id"] = ref(d["intent_change"]["item_id"])
    for sa in d.get("seed_actions", []):
        sa["item_id"] = ref(sa["item_id"])
    for ba in d.get("branch_actions", []):
        if ba["action"] == "OPEN":
            ba["branch_id"] = ba.get("branch_id") or new_id("br")
    d["at"] = at
    d["event_id"] = d.get("event_id") or new_id("ev")
    d["event_type"] = event_type_for(d)
    return d


def affected_item_ids(d: dict) -> list:
    ids = [c["item_id"] for c in d.get("create_items", [])]
    ids += [u["item_id"] for u in d.get("update_items", [])]
    ids += [a["item_id"] for a in d.get("authority_changes", [])]
    ids += list(d.get("resolve", [])) + list(d.get("reopen", []))
    ids += [s["item_id"] for s in d.get("seed_actions", [])]
    if d.get("intent_change"):
        ids.append(d["intent_change"]["item_id"])
    return list(dict.fromkeys(ids))


# --------------------------------------------------------------------------- apply

def apply_delta(model: Model, d: dict) -> Model:
    """Deterministically produce the next model. Exactly one version advance (8.4)."""
    m = model.copy()
    v, at, S = m.version + 1, d["at"], m.scalars

    def touch(it):
        it["updated_state_version"], it["updated_at"] = v, at

    def get(iid):
        if iid not in m.items:
            raise DeltaError(f"unknown item {iid}")
        return m.items[iid]

    for ci in d.get("create_items", []):
        m.items[ci["item_id"]] = {
            "item_id": ci["item_id"], "stream_id": m.stream_id, "item_type": ci["item_type"],
            "content": ci["content"], "authority_status": ci["authority_status"],
            "confidence": ci["confidence"], "lifecycle": ci["lifecycle"],
            "created_state_version": v, "updated_state_version": v, "created_at": at, "updated_at": at,
            "source_refs": ci["source_refs"]}
    for u in d.get("update_items", []):
        it = get(u["item_id"])
        it["content"] = {**it["content"], **u["content"]}
        if u.get("confidence"):
            it["confidence"] = u["confidence"]
        touch(it)
    for a in d.get("authority_changes", []):
        it = get(a["item_id"])
        it["authority_status"] = a["to_status"]
        if a.get("lifecycle"):
            it["lifecycle"] = a["lifecycle"]
        touch(it)
    for rid in d.get("relationships_remove", []):
        m.rels.pop(rid, None)
    for r in d.get("relationships_add", []):
        m.rels[r["relationship_id"]] = {
            "relationship_id": r["relationship_id"], "stream_id": m.stream_id,
            "from_item_id": r["from_item_id"], "to_item_id": r["to_item_id"],
            "relationship_type": r["relationship_type"], "source_refs": r["source_refs"],
            "created_state_version": v, "created_at": at}
    for iid in d.get("resolve", []):
        it = get(iid)
        it["authority_status"], it["lifecycle"] = "HISTORICAL", "RESOLVED"
        touch(it)
    for iid in d.get("reopen", []):
        it = get(iid)
        it["authority_status"], it["lifecycle"] = "UNRESOLVED", "OPEN"
        touch(it)

    stopping = {"focus": S["current_focus"], "position": S["current_position"],
                "direction": S["current_direction"]}
    ic = d.get("intent_change")
    if ic:
        kind, new = ic["kind"], ic["item_id"]
        prev = S["active_intent"]
        if kind == "ORIGIN":
            S["originating_intent"] = S["active_intent"] = new
            S["intent_lineage_refs"] = [new]
        else:
            if prev and prev in m.items and prev != new:
                m.items[prev]["authority_status"] = "SUPERSEDED" if kind == "PIVOT" else "HISTORICAL"
                touch(m.items[prev])
            S["active_intent"] = new
            S["intent_lineage_refs"] = S["intent_lineage_refs"] + [new]
            if kind == "PIVOT":  # INV-007: previous stopping state is preserved
                S["trajectory"] = S["trajectory"] + [{
                    "version": v, "kind": "PIVOT", "from_intent": prev, "to_intent": new,
                    "stopping_state": stopping}]
    for sa in d.get("seed_actions", []):
        life = SEED_ACTION_LIFECYCLE.get(sa["action"])
        if life:
            it = get(sa["item_id"])
            it["lifecycle"] = life
            if life in ("DISCARDED", "MERGED"):
                it["authority_status"] = "HISTORICAL"
            elif it["authority_status"] == "HISTORICAL":
                it["authority_status"] = "PROVISIONAL"
            touch(it)
    for ba in d.get("branch_actions", []):
        if ba["action"] == "OPEN":
            S["branches"] = {**S["branches"], ba["branch_id"]: {
                "branch_id": ba["branch_id"], "purpose": ba.get("purpose", ""), "status": "OPEN",
                "opened_version": v, "closed_version": None, "outcome": None,
                "return_point": {**stopping, "active_intent": S["active_intent"]}}}
            S["trajectory"] = S["trajectory"] + [{"version": v, "kind": "BRANCH_OPEN",
                                                  "branch_id": ba["branch_id"], "stopping_state": stopping}]
        else:  # CLOSE: return to the parent position without reconstruction (10.3)
            b = dict(S["branches"][ba["branch_id"]])
            b.update(status="CLOSED", closed_version=v, outcome=ba.get("outcome"))
            S["branches"] = {**S["branches"], ba["branch_id"]: b}
            rp = b["return_point"]
            S["current_focus"], S["current_position"], S["current_direction"] = (
                rp["focus"], rp["position"], rp["direction"])
            S["trajectory"] = S["trajectory"] + [{"version": v, "kind": "BRANCH_CLOSE",
                                                  "branch_id": ba["branch_id"], "stopping_state": stopping}]
    for k, val in (d.get("focus") or {}).items():
        if k in FOCUS_KEYS:
            S[k] = val
    if d.get("trajectory"):
        S["trajectory"] = S["trajectory"] + [{"version": v, **d["trajectory"]}]
    S["recent_significant_changes"] = (S["recent_significant_changes"] + [{
        "version": v, "event_id": d["event_id"], "event_type": d["event_type"],
        "classification": d["primary_classification"], "summary": d.get("summary", "")}])[-RECENT_LIMIT:]
    m.version = v
    return m


# --------------------------------------------------------------------------- projection

def brief(it: dict, with_refs: bool = True) -> dict:
    out = {"item_id": it["item_id"], "item_type": it["item_type"], "text": item_text(it),
           "authority_status": it["authority_status"], "confidence": it["confidence"],
           "lifecycle": it["lifecycle"], "updated_state_version": it["updated_state_version"]}
    extra = {k: v for k, v in it["content"].items() if k != "text"} if isinstance(it["content"], dict) else {}
    if extra:
        out["detail"] = extra
    if with_refs:
        out["source_event_ids"] = sorted({r["source_event_id"] for r in it["source_refs"]})
    return out


def corrected_by(model: Model) -> dict:
    """item_id -> [ids it corrected/superseded] for CURRENT/PROVISIONAL replacers."""
    out: dict = {}
    for r in model.rels.values():
        if r["relationship_type"] in ("CORRECTS", "SUPERSEDES"):
            out.setdefault(r["from_item_id"], []).append((r["relationship_type"], r["to_item_id"]))
    return out


def project(model: Model) -> dict:
    """The one authoritative Living Continuity State view (6). Compact, derived from items."""
    S, items = model.scalars, model.items

    def pick(types, status=("CURRENT",)):
        return sorted((brief(i) for i in items.values()
                       if i["item_type"] in types and i["authority_status"] in status),
                      key=lambda b: b["updated_state_version"])

    corr = []
    cb = corrected_by(model)
    for iid, targets in cb.items():
        it = items[iid]
        if it["authority_status"] == "CURRENT" and any(t[0] == "CORRECTS" for t in targets):
            corr.append({**brief(it), "corrects": [
                {"item_id": t, "was": item_text(items[t])} for k, t in targets if k == "CORRECTS" and t in items]})
    seen = {c["item_id"] for c in corr}
    corr += [b for b in pick({"CORRECTION"}) if b["item_id"] not in seen]
    open_branches = {k: v for k, v in S["branches"].items() if v["status"] == "OPEN"}
    cont = {"position": S["current_position"], "direction": S["current_direction"],
            "focus": S["current_focus"],
            "open_branch_return_points": {k: v["return_point"] for k, v in open_branches.items()}}
    cur_items = [i for i in items.values() if i["authority_status"] in ("CURRENT", "PROVISIONAL", "UNRESOLVED")]
    return {
        "stream_id": model.stream_id, "state_version": model.version,
        "originating_intent": brief(items[S["originating_intent"]]) if S["originating_intent"] else None,
        "active_intent": brief(items[S["active_intent"]]) if S["active_intent"] else None,
        "intent_lineage_refs": list(S["intent_lineage_refs"]),
        "current_focus": S["current_focus"], "current_position": S["current_position"],
        "current_direction": S["current_direction"], "current_understanding": S["current_understanding"],
        "established_content": pick({"CONCEPT", "CONTENT", "DISCOVERY", "DISTINCTION"}),
        "governing_decisions": pick({"DECISION"}),
        "applicable_corrections": corr,
        "active_constraints": pick({"CONSTRAINT"}),
        "unresolved_items": pick(set(ITEM_TYPES), ("UNRESOLVED",)),
        "provisional_items": [b for b in pick(set(ITEM_TYPES) - {"SEED"}, ("PROVISIONAL",))],
        "seeds": [brief(i) for i in items.values() if i["item_type"] == "SEED" and i["lifecycle"] in LIVE_SEED],
        "branches": open_branches,
        "trajectory": list(S["trajectory"]),
        "recent_significant_changes": list(S["recent_significant_changes"]),
        "continuation_point": cont,
        "provenance": {"items": len(items), "relationships": len(model.rels)},
        "confidence": {"current_items": len(cur_items),
                       "low_confidence": sorted(i["item_id"] for i in cur_items
                                                if i["confidence"] in ("INFERRED", "UNCERTAIN"))},
    }


def semantic_view(model: Model) -> dict:
    """What recovery must reproduce: projection plus every item's authority/lifecycle/content."""
    return {"projection": project(model),
            "items": {k: {"c": v["content"], "a": v["authority_status"], "l": v["lifecycle"],
                          "t": v["item_type"], "cf": v["confidence"]} for k, v in model.items.items()},
            "rels": sorted((r["from_item_id"], r["relationship_type"], r["to_item_id"])
                           for r in model.rels.values())}
