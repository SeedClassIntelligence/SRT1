"""Reflection / classification producing candidate State Deltas (spec 7, 8).

The reflection capability is a *proposer*: it turns a source event plus current state into a
StateDelta. It never mutates anything; validation and commit belong to the core (8.3).

`Proposer` is the plug-in point for an LLM. `RuleProposer` is the deterministic baseline: it
understands explicit cognitive markers (`DECISION:`, `CORRECTION[id]:`, `SEED:` ...) and preserves
uncertainty instead of guessing when it cannot ground a target. Free prose with no markers is
classified NO_MATERIAL_CHANGE -- a rule engine cannot honestly interpret it.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Protocol

from .models import StateDelta
from .state import Model, item_text

STOP = frozenset("""a an and are as at be been but by can could did do does for from had has have how i if in into is it
its me my no not of on or our so than that the their them then there these they this to us was we were what when
where which who why will with would you your should shall may might also just about over under more most""".split())
GENERIC = frozenset("continue work next step current status proceed resume help task project thing going".split())


def terms(text: str) -> set:
    """Crude stemmed content terms (4-char stems). Good enough for V1 relevance; swap for embeddings later."""
    return {w[:4] if len(w) > 4 else w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) > 2 and w not in STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = terms(a), terms(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / math.sqrt(len(ta) * len(tb))


class Proposer(Protocol):
    def propose(self, source: dict, model: Model) -> dict | None: ...


class CallableProposer:
    """Wrap any function(source_row, model) -> StateDelta|dict|None, e.g. an LLM-backed reflector."""

    def __init__(self, fn: Callable):
        self.fn = fn

    def propose(self, source, model):
        out = self.fn(source, model)
        return out.to_dict() if isinstance(out, StateDelta) else out


CONF_ORDER = ["EXPLICIT", "SUPPORTED", "INFERRED", "UNCERTAIN"]
PRIORITY = ["PIVOT", "SUPERSESSION", "CORRECTION", "BRANCH", "RECLAMATION", "RESOLUTION", "ABANDONMENT",
            "DEFERMENT", "REFINEMENT", "EXPANSION", "CONTRACTION", "SEED", "CONTINUATION"]
HEDGE = re.compile(r"\b(maybe|might|perhaps|what if|could we|possibly|not sure)\b|\?\s*$", re.I)
CMD = re.compile(r"^\s*(?P<cmd>[A-Za-z][A-Za-z ]*?)\s*(?:\[(?P<ref>[^\]]*)\])?\s*:\s*(?P<body>.*\S)\s*$")
SIMPLE_ITEM = {"DECISION": "DECISION", "CONSTRAINT": "CONSTRAINT", "DISCOVERY": "DISCOVERY", "CONTENT": "CONTENT",
               "CONCEPT": "CONCEPT", "DISTINCTION": "DISTINCTION"}
OPEN_ITEM = {"QUESTION": "QUESTION", "UNRESOLVED": "UNRESOLVED", "ASSUMPTION": "ASSUMPTION"}


def weaker(a, b):
    return a if CONF_ORDER.index(a) >= CONF_ORDER.index(b) else b


def depth_for(d: dict) -> int:
    """Processing depth 0-3 scales with significance (7.1)."""
    cls = d["primary_classification"]
    if cls == "NO_MATERIAL_CHANGE":
        return 0
    if cls in ("PIVOT", "BRANCH", "SUPERSESSION", "RECLAMATION") or d.get("intent_change"):
        return 3
    if cls in ("CORRECTION", "SEED", "RESOLUTION", "DEFERMENT", "ABANDONMENT") or any(
            c["item_type"] in ("DECISION", "DISCOVERY") for c in d.get("create_items", [])):
        return 2
    return 1


def resolve_target(model: Model, ref, body, authorities, exclude_types=("INTENT",), types=None):
    """Find the item a command refers to. Explicit id => EXPLICIT; fuzzy text => SUPPORTED/INFERRED."""
    if ref:
        ref = ref.strip()
        hits = [i for k, i in model.items.items() if k == ref or k.startswith(ref)]
        return (hits[0], "EXPLICIT") if len(hits) == 1 else (None, "UNCERTAIN")
    best, score = None, 0.0
    for it in model.items.values():
        if it["authority_status"] not in authorities or it["item_type"] in exclude_types:
            continue
        if types and it["item_type"] not in types:
            continue
        s = similarity(body, item_text(it))
        if s > score:
            best, score = it, s
    if best is None or score < 0.2:
        return None, "UNCERTAIN"
    return best, ("SUPPORTED" if score >= 0.45 else "INFERRED")


class RuleProposer:
    """Deterministic marker-driven reflection. See module docstring."""

    def propose(self, source: dict, model: Model) -> dict | None:
        user = source["source_type"] == "USER_INPUT"
        base_conf = "EXPLICIT" if user else "SUPPORTED"
        auth_default = "CURRENT" if user else "PROVISIONAL"
        s = model.scalars
        d = StateDelta(stream_id=model.stream_id, expected_state_version=model.version,
                       source_event_ids=[source["source_event_id"]], primary_classification="CONTINUATION",
                       confidence=base_conf)
        classes: set = set()
        notes: list = []
        n = 0

        def ref():
            nonlocal n
            n += 1
            return f"${n}"

        def lower(c):
            d.confidence = weaker(d.confidence, c)

        def unmatched(cmd, body):
            r = ref()
            d.create_items.append({"ref": r, "item_type": "UNRESOLVED", "authority_status": "UNRESOLVED",
                                   "confidence": "UNCERTAIN",
                                   "content": {"text": f"Unmatched {cmd.lower()} target: {body}"}})
            classes.add("CONTINUATION")
            lower("UNCERTAIN")
            notes.append(f"{cmd}: no grounded target, uncertainty preserved")

        active_intent = s["active_intent"]
        open_branches = [b for b in s["branches"].values() if b["status"] == "OPEN"]
        for line in source["content"].splitlines():
            m = CMD.match(line)
            if not m:
                continue
            cmd, rf, body = m.group("cmd").upper().strip(), m.group("ref"), m.group("body").strip()
            text = body.split("|")[0].strip()
            extra = [x.strip() for x in body.split("|")[1:]]

            if cmd == "INTENT" or cmd in ("EXPAND", "NARROW"):
                if d.intent_change:  # one intent change per source event; keep the rest as uncertainty
                    unmatched(cmd, body)
                    continue
                r = ref()
                d.create_items.append({"ref": r, "item_type": "INTENT", "authority_status": "CURRENT",
                                       "confidence": base_conf, "content": {"text": text}})
                if not active_intent:
                    d.intent_change = {"kind": "ORIGIN", "item_id": r}
                    classes.add("CONTINUATION")
                else:
                    kind, rel, cl = {"INTENT": ("REFINE", "REFINES", "REFINEMENT"),
                                     "EXPAND": ("EXPAND", "EXPANDS", "EXPANSION"),
                                     "NARROW": ("NARROW", "NARROWS", "CONTRACTION")}[cmd]
                    d.intent_change = {"kind": kind, "item_id": r}
                    d.relationships_add.append({"from_item_id": r, "to_item_id": active_intent,
                                                "relationship_type": rel})
                    classes.add(cl)
                notes.append(cmd.lower())
            elif cmd == "PIVOT":
                if HEDGE.search(text):  # 10.2: uncertain pivot is preserved as a seed
                    d.create_items.append({"ref": ref(), "item_type": "SEED", "authority_status": "PROVISIONAL",
                                           "confidence": "INFERRED", "content": {
                                               "text": text, "potential_significance": "possible pivot, unconfirmed"}})
                    classes.add("SEED")
                    notes.append("hedged pivot kept as seed")
                elif not active_intent:
                    unmatched(cmd, body)
                else:
                    r = ref()
                    d.create_items.append({"ref": r, "item_type": "INTENT", "authority_status": "CURRENT",
                                           "confidence": base_conf, "content": {"text": text}})
                    d.intent_change = {"kind": "PIVOT", "item_id": r}
                    d.relationships_add.append({"from_item_id": r, "to_item_id": active_intent,
                                                "relationship_type": "SUPERSEDES"})
                    classes.add("PIVOT")
                    notes.append("pivot")
            elif cmd in SIMPLE_ITEM:
                c = {"text": text}
                if cmd == "DECISION" and extra:
                    c["rationale"] = extra[0]
                    if len(extra) > 1:
                        c["alternatives"] = [a.strip() for a in extra[1].split(";") if a.strip()]
                d.create_items.append({"ref": ref(), "item_type": SIMPLE_ITEM[cmd], "authority_status": auth_default,
                                       "confidence": base_conf, "content": c})
                classes.add("CONTINUATION")
            elif cmd in OPEN_ITEM:
                d.create_items.append({"ref": ref(), "item_type": OPEN_ITEM[cmd], "authority_status": "UNRESOLVED",
                                       "confidence": base_conf, "content": {"text": text}})
                classes.add("CONTINUATION")
            elif cmd == "SEED":
                c = {"text": text}
                if extra:
                    c["potential_significance"] = extra[0]
                d.create_items.append({"ref": ref(), "item_type": "SEED", "authority_status": "PROVISIONAL",
                                       "confidence": base_conf, "content": c})
                classes.add("SEED")
            elif cmd in ("CORRECTION", "CORRECT", "SUPERSEDE"):
                old_phrase, _, new = body.partition("=>")
                new = new.strip() if new else body
                old_phrase = old_phrase.strip() if _ else body
                tgt, tconf = resolve_target(model, rf, old_phrase, ("CURRENT", "PROVISIONAL", "UNRESOLVED"))
                if tgt is None:
                    unmatched(cmd, body)
                    continue
                lower(tconf)
                sup = cmd == "SUPERSEDE"
                r = ref()
                d.create_items.append({"ref": r, "item_type": tgt["item_type"], "authority_status": auth_default,
                                       "confidence": weaker(base_conf, tconf), "content": {"text": new}})
                d.relationships_add.append({"from_item_id": r, "to_item_id": tgt["item_id"],
                                            "relationship_type": "SUPERSEDES" if sup else "CORRECTS"})
                d.authority_changes.append({"item_id": tgt["item_id"],
                                            "to_status": "SUPERSEDED" if sup else "REJECTED",
                                            "lifecycle": "SUPERSEDED" if sup else "CLOSED",
                                            "reason": "superseded" if sup else "corrected"})
                classes.add("SUPERSESSION" if sup else "CORRECTION")
            elif cmd in ("RESOLVE", "DEFER", "ABANDON", "RECLAIM", "CONFIRM"):
                self._lifecycle(cmd, rf, body, model, d, classes, ref, lower, unmatched, base_conf, auth_default)
            elif cmd == "BRANCH":
                d.branch_actions.append({"action": "OPEN", "purpose": text})
                classes.add("BRANCH")
            elif cmd in ("END BRANCH", "RETURN"):
                tgt = next((b for b in open_branches if rf and b["branch_id"].startswith(rf.strip())), None) \
                    or (open_branches[-1] if open_branches and not rf else None)
                if tgt is None:
                    unmatched(cmd, body)
                else:
                    d.branch_actions.append({"action": "CLOSE", "branch_id": tgt["branch_id"], "outcome": text})
                    classes.add("BRANCH")
            elif cmd in ("FOCUS", "POSITION", "DIRECTION", "UNDERSTANDING"):
                d.focus["current_" + cmd.lower()] = text
                classes.add("CONTINUATION")
        if not classes:
            return None
        if "SEED" in classes and d.focus.get("current_direction") is not None:
            classes.discard("SEED")
        d.primary_classification = next((c for c in PRIORITY if c in classes), "CONTINUATION")
        d.summary = d.rationale = "; ".join(notes) or d.primary_classification.lower()
        d.depth = depth_for(d.to_dict())
        return d.to_dict()

    @staticmethod
    def _lifecycle(cmd, rf, body, model, d, classes, ref, lower, unmatched, base_conf, auth_default):
        pool = {"RESOLVE": ("UNRESOLVED",), "DEFER": ("UNRESOLVED", "PROVISIONAL"),
                "ABANDON": ("UNRESOLVED", "PROVISIONAL"), "CONFIRM": ("PROVISIONAL",),
                "RECLAIM": ("UNRESOLVED", "PROVISIONAL", "HISTORICAL")}[cmd]
        text = body.split("|")[0].strip()
        tgt, tconf = resolve_target(model, rf, text, pool)
        if tgt is None:
            unmatched(cmd, body)
            return
        lower(tconf)
        iid, seed = tgt["item_id"], tgt["item_type"] == "SEED"
        if cmd == "RESOLVE":
            r = ref()
            d.create_items.append({"ref": r, "item_type": "CONTENT", "authority_status": auth_default,
                                   "confidence": base_conf, "content": {"text": text}})
            d.relationships_add.append({"from_item_id": r, "to_item_id": iid, "relationship_type": "RESOLVES"})
            d.resolve.append(iid)
            classes.add("RESOLUTION")
        elif cmd == "DEFER":
            if seed:
                d.seed_actions.append({"action": "DEFER", "item_id": iid})
            else:
                d.authority_changes.append({"item_id": iid, "to_status": tgt["authority_status"],
                                            "lifecycle": "DEFERRED"})
            classes.add("DEFERMENT")
        elif cmd == "ABANDON":
            if seed:
                d.seed_actions.append({"action": "DISCARD", "item_id": iid})
            else:
                d.authority_changes.append({"item_id": iid, "to_status": "HISTORICAL", "lifecycle": "ABANDONED"})
            classes.add("ABANDONMENT")
        elif cmd == "CONFIRM":
            d.authority_changes.append({"item_id": iid, "to_status": "CURRENT", "lifecycle": "ACTIVE"})
            classes.add("CONTINUATION")
        else:  # RECLAIM
            apply_reclaim(d, tgt)
            classes.add("RECLAMATION")


def apply_reclaim(d: StateDelta, item: dict):
    """Add the mutations that reclaim `item` (seed, deferred or parked unresolved work) to delta `d`."""
    iid = item["item_id"]
    if item["item_type"] == "SEED":
        d.seed_actions.append({"action": "RECLAIM", "item_id": iid})
    elif item["authority_status"] == "HISTORICAL":
        d.reopen.append(iid)
    else:
        d.authority_changes.append({"item_id": iid, "to_status": item["authority_status"], "lifecycle": "OPEN"})
