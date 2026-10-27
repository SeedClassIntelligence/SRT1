"""Pull integration with a consuming intelligence (spec 13, 13.1, 21 step 12) and the push loop (13.2/13.3).

Two pieces:
  * `TOOLS` + `dispatch`: model-facing tool definitions (Anthropic tool-use schema) and a dispatcher, so any
    LLM runtime can pull continuity: intent, position, decisions, corrections, unresolved state, rationale,
    source support, seeds, and task context. Nothing here mutates state except `checkpoint`/`reclaim`.
  * `Consumer`: a turn loop for a consuming model. observe user -> pull context -> model call -> push check on the
    draft (one micro-reinjection retry) -> observe the final reply as ASSISTANT_OUTPUT. The model is any callable.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .core import SRT1
from .models import SourceEvent
from .push import render_push
from .retrieval import render
from .state import item_text

QUERY_KINDS = ("current_intent", "current_position", "decisions", "corrections", "unresolved", "rationale",
               "source_support", "seeds")


def _schema(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {"name": "srt1_get_context",
     "description": "Get continuity-complete working context for a task: active intent, current position, governing "
                    "decisions, corrections, constraints, relevant unresolved items and seeds. Call this before "
                    "starting or resuming work; do not reconstruct state from the transcript.",
     "input_schema": _schema({"task": {"type": "string"}, "budget": {"type": "integer",
                                                                    "description": "advisory token budget"},
                              "exact_wording": {"type": "boolean"}}, ["task"])},
    {"name": "srt1_query",
     "description": "Ask one narrow continuity question. kind: " + ", ".join(QUERY_KINDS) +
                    ". 'rationale' and 'source_support' need item_id (an id returned by another query).",
     "input_schema": _schema({"kind": {"type": "string", "enum": list(QUERY_KINDS)},
                              "item_id": {"type": "string"}}, ["kind"])},
    {"name": "srt1_get_state", "description": "Compact authoritative Living Continuity State.",
     "input_schema": _schema({"scope": {"type": "array", "items": {"type": "string"}}}, [])},
    {"name": "srt1_reclaim",
     "description": "Restore a deferred seed, parked unresolved item, or closed branch with its originating context.",
     "input_schema": _schema({"target": {"type": "string"}}, ["target"])},
    {"name": "srt1_checkpoint", "description": "Create an explicit recoverable snapshot.",
     "input_schema": _schema({"reason": {"type": "string"}}, [])},
    {"name": "srt1_verify", "description": "Validate stored state against the event log and evidence; with an item_id, "
                                            "also explain why SRT1 believes it.",
     "input_schema": _schema({"item_id": {"type": "string"}}, [])},
]


def query(srt: SRT1, stream_id: str, kind: str, item_id: str | None = None) -> dict:
    """Narrow pull queries (13.1). Answers come from maintained state first, evidence only when asked for."""
    if kind not in QUERY_KINDS:
        raise ValueError(f"unknown query kind {kind}; expected one of {QUERY_KINDS}")
    st = srt.get_state(stream_id)
    if kind == "current_intent":
        return {"originating_intent": st["originating_intent"], "active_intent": st["active_intent"],
                "lineage": st["intent_lineage_refs"], "state_version": st["state_version"]}
    if kind == "current_position":
        return {"focus": st["current_focus"], "position": st["current_position"],
                "direction": st["current_direction"], "continuation_point": st["continuation_point"]}
    if kind == "decisions":
        return {"decisions": st["governing_decisions"]}
    if kind == "corrections":  # prior rejection/correction, with what each replaced
        return {"corrections": st["applicable_corrections"]}
    if kind == "unresolved":
        return {"unresolved": st["unresolved_items"]}
    if kind == "seeds":
        return {"seeds": st["seeds"]}
    if not item_id:
        raise ValueError(f"{kind} needs item_id")
    ex = srt.explain(stream_id, item_id)
    if kind == "rationale":
        model = srt.load(stream_id)
        it = model.items[item_id]
        return {"item": ex["item"], "rationale": {k: v for k, v in it["content"].items() if k != "text"},
                "lineage": ex["lineage"], "development": ex["events"]}
    return {"item": ex["item"], "evidence": ex["evidence"]}  # source_support


def dispatch(srt: SRT1, stream_id: str, name: str, args: dict | None = None) -> dict:
    """Execute a model-issued tool call. Always returns a JSON-serialisable dict; errors are returned, not raised."""
    a = args or {}
    try:
        if name == "srt1_get_context":
            return srt.get_context(stream_id, a["task"], budget=a.get("budget"),
                                   exact_wording=bool(a.get("exact_wording")))
        if name == "srt1_query":
            return query(srt, stream_id, a["kind"], a.get("item_id"))
        if name == "srt1_get_state":
            return srt.get_state(stream_id, a.get("scope"))
        if name == "srt1_reclaim":
            return srt.reclaim(stream_id, a["target"])
        if name == "srt1_checkpoint":
            return {"snapshot_id": srt.checkpoint(stream_id, a.get("reason"))}
        if name == "srt1_verify":
            return srt.verify(stream_id, a.get("item_id"))
        return {"error": f"unknown tool {name}"}
    except (KeyError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


@dataclass
class TurnResult:
    reply: str
    package: dict
    push: dict | None = None            # the packet that triggered a correction, if any
    corrected: bool = False
    observed: list = field(default_factory=list)


class Consumer:
    """Drive a consuming model with maintained continuity instead of replayed history.

    `llm(context, user_text) -> str` is any callable. `context` is rendered continuity (plus a micro-reinjection
    notice on the retry), never the transcript.
    """

    def __init__(self, srt: SRT1, stream_id: str, llm: Callable[[str, str], str], budget: int | None = None,
                 max_corrections: int = 1):
        self.srt, self.stream_id, self.llm = srt, stream_id, llm
        self.budget, self.max_corrections = budget, max_corrections

    def turn(self, user_text: str) -> TurnResult:
        s, sid = self.srt, self.stream_id
        observed = [s.observe(SourceEvent(content=user_text, source_type="USER_INPUT"), sid)]
        pkg = s.get_context(sid, user_text, budget=self.budget)
        context = render(pkg)
        reply, push, corrected = self.llm(context, user_text), None, False
        for _ in range(self.max_corrections):
            packet = s.check_push(sid, reply)
            if not packet:
                break
            push, corrected = packet, True   # 13.3: minimum corrective packet, then one retry
            reply = self.llm(render_push(packet) + "\n\n" + context, user_text)
        observed.append(s.observe(SourceEvent(content=reply, source_type="ASSISTANT_OUTPUT"), sid))
        return TurnResult(reply, pkg, push, corrected, observed)
