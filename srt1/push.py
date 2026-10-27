"""Conservative push continuity and micro-reinjection (spec 13.2, 13.3, INV-038).

`check_push` looks at text a consuming intelligence is about to say or act on and returns a *minimal*
corrective packet only when one of the six spec triggers is clearly met; otherwise None. Silence is the
default: a push that is wrong or noisy costs the user more than a missed nudge.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

import re

from .reflection import similarity, terms
from .state import Model, brief, item_text, project

NEG = re.compile(r"\b(never|must not|do not|don't|dont|no|not|without|avoid)\b\s+(?P<what>.+)", re.I)
SUPERSEDED_MIN, CONSTRAINT_MIN, UNRESOLVED_MIN, SEED_MIN = 0.6, 0.5, 0.6, 0.6
DEPARTURE_MIN_TERMS = 12   # short drafts are never "departures"
MAX_GOVERNING = 3          # micro-reinjection stays small (13.3)


def _packet(reasons: list, proj: dict, governing: list) -> dict:
    return {"reason": reasons[0], "reasons": reasons,
            "active_intent": proj["active_intent"], "current_focus": proj["current_focus"],
            "governing": governing[:MAX_GOVERNING], "continuation_point": proj["continuation_point"]}


def check_push(model: Model, draft: str) -> dict | None:
    proj = project(model)
    items = model.items
    reasons: list = []
    governing: list = []
    cur_best = max((similarity(draft, item_text(i)) for i in items.values()
                    if i["authority_status"] == "CURRENT"), default=0.0)

    # 1. superseded / corrected / rejected material resurfacing as if current
    for it in items.values():
        if it["authority_status"] in ("SUPERSEDED", "REJECTED") and it["item_type"] != "INTENT":
            s = similarity(draft, item_text(it))
            if s >= SUPERSEDED_MIN and s > cur_best:
                reasons.append("SUPERSEDED_RESURFACED")
                gov = [b for b in proj["applicable_corrections"]
                       if any(c["item_id"] == it["item_id"] for c in b.get("corrects", []))]
                governing += gov or proj["governing_decisions"][-1:]
                break

    # 2. a governing constraint is about to be ignored: "never X" while the draft does X
    for c in proj["active_constraints"]:
        m = NEG.search(c["text"])
        if m and similarity(m.group("what"), draft) >= CONSTRAINT_MIN and not NEG.search(draft):
            reasons.append("CONSTRAINT_AT_RISK")
            governing.append(c)
            break

    # 3. repeated user corrections: continuity has broken down
    recent = proj["recent_significant_changes"][-6:]
    if sum(1 for r in recent if r["classification"] == "CORRECTION") >= 3:
        reasons.append("REPEATED_CORRECTIONS")
        governing += proj["applicable_corrections"][-3:]

    # 4. unresolved material becomes immediately relevant
    best = max(proj["unresolved_items"], key=lambda b: similarity(draft, b["text"]), default=None)
    if best and similarity(draft, best["text"]) >= UNRESOLVED_MIN:
        reasons.append("UNRESOLVED_RELEVANT")
        governing.append(best)

    # 5. a prior seed becomes directly relevant
    best = max(proj["seeds"], key=lambda b: similarity(draft, b["text"]), default=None)
    if best and similarity(draft, best["text"]) >= SEED_MIN:
        reasons.append("SEED_RELEVANT")
        governing.append(best)

    # 6. unintentional departure from active intent: long draft sharing nothing with the live context
    dt = terms(draft)
    if len(dt) >= DEPARTURE_MIN_TERMS and proj["active_intent"] and not reasons:
        ctx = " ".join([proj["active_intent"]["text"], proj["current_focus"], proj["current_position"],
                        proj["current_direction"], proj["current_understanding"]]
                       + [b["text"] for k in ("governing_decisions", "established_content", "active_constraints")
                          for b in proj[k]])
        if len(dt & terms(ctx)) / len(dt) < 0.05:
            reasons.append("INTENT_DEPARTURE")
            governing.append(proj["active_intent"])

    if not reasons:
        return None
    seen, uniq = set(), []
    for g in governing:
        if g["item_id"] not in seen:
            seen.add(g["item_id"])
            uniq.append({k: g[k] for k in ("item_id", "item_type", "text", "authority_status") if k in g}
                        | ({"corrects": g["corrects"]} if g.get("corrects") else {}))
    return _packet(reasons, proj, uniq)


def render_push(packet: dict) -> str:
    """The micro-reinjection as text a consuming model can be given verbatim."""
    L = [f"CONTINUITY NOTICE ({', '.join(packet['reasons'])})"]
    if packet.get("active_intent"):
        L.append("Active intent: " + packet["active_intent"]["text"])
    if packet.get("current_focus"):
        L.append("Current focus: " + packet["current_focus"])
    for g in packet["governing"]:
        extra = "".join(f" (replaces: {c['was']})" for c in g.get("corrects", []))
        L.append(f"{g['item_type'].title()}: {g['text']}{extra}")
    cp = packet.get("continuation_point") or {}
    if cp.get("position"):
        L.append("Continue from: " + cp["position"])
    return "\n".join(L)
