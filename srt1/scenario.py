"""Scenario runner: scripted free-prose turns plus expectations, shared by the live recorder and the replay test.

A scenario is JSON: {"name", "turns": [{"text", "source_type"?, "expect"?: {"status_in", "classification_in"},
"checks"?: [{"kind", "value"}]}]}. Checks read the Living Continuity State after the turn. They describe what a
sensible reflection must leave behind, loosely enough to survive a model's paraphrasing; they are not exact-match.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core import SRT1
from .models import SourceEvent


@dataclass
class TurnReport:
    index: int
    text: str
    status: str
    classification: str | None
    state_version: int | None
    failures: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def _has(items: list, needle: str) -> bool:
    return any(needle.lower() in i["text"].lower() for i in items)


def run_check(srt: SRT1, sid: str, kind: str, value, memo: dict) -> str | None:
    """Return a failure message, or None if the check holds."""
    st = srt.get_state(sid)
    if kind == "has_active_intent":
        return None if st["active_intent"] else "no active intent"
    if kind == "decision_current_contains":
        return None if _has(st["governing_decisions"], value) else f"no CURRENT decision mentions {value!r}"
    if kind == "decision_not_current_contains":
        return f"a CURRENT decision still mentions {value!r}" if _has(st["governing_decisions"], value) else None
    if kind == "constraint_current_contains":
        return None if _has(st["active_constraints"], value) else f"no CURRENT constraint mentions {value!r}"
    if kind == "unresolved_contains":
        return None if _has(st["unresolved_items"], value) else f"no unresolved item mentions {value!r}"
    if kind == "unresolved_not_contains":
        return f"an unresolved item still mentions {value!r}" if _has(st["unresolved_items"], value) else None
    if kind == "seed_count_at_least":
        return None if len(st["seeds"]) >= value else f"{len(st['seeds'])} seed(s), wanted >= {value}"
    if kind == "intent_unchanged_since":
        was = memo.get("intent_after", {}).get(value)
        now = st["active_intent"]["item_id"] if st["active_intent"] else None
        return None if was == now else f"active intent changed since turn {value}"
    if kind == "verify_ok":
        v = srt.verify(sid)
        return None if v["ok"] else f"verify failed: {v['problems'][:2]}"
    return f"unknown check kind {kind!r}"


def run_scenario(srt: SRT1, scenario: dict, stream_id: str | None = None, stop_on_failure: bool = False) -> list:
    """Run every turn. With stop_on_failure, stop at the first FAILED turn (model/network error): later turns would
    run against state the scenario did not expect and only produce noise."""
    sid = stream_id or srt.create_stream(scenario.get("name"))
    memo: dict = {"intent_after": {}}
    reports = []
    for n, turn in enumerate(scenario["turns"], start=1):
        r = srt.observe(SourceEvent(content=turn["text"], source_type=turn.get("source_type", "USER_INPUT")), sid)
        ia = srt.get_state(sid)["active_intent"]
        memo["intent_after"][n] = ia["item_id"] if ia else None
        rep = TurnReport(n, turn["text"], r.status, r.classification, r.state_version, reasons=list(r.reasons))
        exp = turn.get("expect", {})
        if exp.get("status_in") and r.status not in exp["status_in"]:
            rep.failures.append(f"status {r.status} not in {exp['status_in']}")
        if exp.get("classification_in") and r.classification not in exp["classification_in"]:
            rep.failures.append(f"classification {r.classification} not in {exp['classification_in']}")
        for c in turn.get("checks", []):
            msg = run_check(srt, sid, c["kind"], c.get("value"), memo)
            if msg:
                rep.failures.append(msg)
        reports.append(rep)
        if stop_on_failure and r.status == "FAILED":
            break
    return reports
