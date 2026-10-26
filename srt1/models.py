"""SRT1 canonical vocabularies and value objects (spec sections 5, 7, 8.1).

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

STREAM_STATUS = ("ACTIVE", "PAUSED", "COMPLETED", "ARCHIVED")
SOURCE_TYPES = ("USER_INPUT", "ASSISTANT_OUTPUT", "EXTERNAL_CONTENT", "SYSTEM_EVENT", "INTERNAL_CONTINUITY_EVENT")
PROCESSING_STATUS = ("RECEIVED", "PROCESSED", "FAILED", "DEFERRED")
REF_TYPES = ("DIRECT", "SUPPORTING", "CONTEXTUAL")

ITEM_TYPES = ("INTENT", "CONCEPT", "CONTENT", "DECISION", "CORRECTION", "CONSTRAINT", "QUESTION",
              "ASSUMPTION", "DISCOVERY", "DISTINCTION", "SEED", "UNRESOLVED")
AUTHORITY = ("CURRENT", "PROVISIONAL", "UNRESOLVED", "SUPERSEDED", "REJECTED", "HISTORICAL")
NON_CURRENT = frozenset({"SUPERSEDED", "REJECTED", "HISTORICAL"})
CONFIDENCE = ("EXPLICIT", "SUPPORTED", "INFERRED", "UNCERTAIN")
FIRM_CONFIDENCE = frozenset({"EXPLICIT", "SUPPORTED"})

REL_TYPES = ("DERIVED_FROM", "REFINES", "EXPANDS", "NARROWS", "CORRECTS", "SUPERSEDES", "CONTRADICTS",
             "SUPPORTS", "DEPENDS_ON", "RELATED_TO", "BRANCHED_FROM", "MERGED_INTO", "EMERGED_FROM",
             "RESOLVES", "REOPENS")
# Relationship types that record lineage and may never be removed (INV-005, INV-020).
LINEAGE_RELS = frozenset(set(REL_TYPES) - {"SUPPORTS", "DEPENDS_ON", "RELATED_TO"})

EVENT_TYPES = ("CREATE", "CONTINUE", "REFINE", "EXPAND", "NARROW", "CORRECT", "SUPERSEDE", "REJECT", "DEFER",
               "PIVOT", "BRANCH", "MERGE", "RECLAIM", "RESOLVE", "REOPEN", "COMPLETE", "ABANDON")
CLASSIFICATIONS = ("NO_MATERIAL_CHANGE", "CONTINUATION", "REFINEMENT", "EXPANSION", "CONTRACTION", "CORRECTION",
                   "SUPERSESSION", "CONTRADICTION", "PIVOT", "BRANCH", "SEED", "RECLAMATION", "RESOLUTION",
                   "REOPENING", "COMPLETION", "DEFERMENT", "ABANDONMENT")
CLASS_EVENT = {
    "CONTINUATION": "CONTINUE", "REFINEMENT": "REFINE", "EXPANSION": "EXPAND", "CONTRACTION": "NARROW",
    "CORRECTION": "CORRECT", "SUPERSESSION": "SUPERSEDE", "CONTRADICTION": "CREATE", "PIVOT": "PIVOT",
    "BRANCH": "BRANCH", "SEED": "CREATE", "RECLAMATION": "RECLAIM", "RESOLUTION": "RESOLVE",
    "REOPENING": "REOPEN", "COMPLETION": "COMPLETE", "DEFERMENT": "DEFER", "ABANDONMENT": "ABANDON",
}
SEED_LIFECYCLE = ("CAPTURED", "DEFERRED", "RECLAIMED", "DEVELOPING", "MERGED", "DISCARDED")
LIVE_SEED = frozenset({"CAPTURED", "DEFERRED", "RECLAIMED", "DEVELOPING"})
CLOSING_LIFECYCLE = frozenset({"RESOLVED", "ABANDONED", "SUPERSEDED", "CLOSED"})

VALID, INVALID, STALE, CLARIFY = "VALID", "INVALID", "STALE", "REQUIRES_CLARIFICATION"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class SourceEvent:
    """Immutable evidence (5.2). Content is never rewritten after receipt."""
    content: str
    source_type: str = "USER_INPUT"
    stream_id: str | None = None
    source_event_id: str = field(default_factory=lambda: new_id("src"))
    external_source_id: str | None = None
    source_metadata: dict | None = None
    occurred_at: str | None = None
    received_at: str | None = None
    processing_status: str = "RECEIVED"


@dataclass
class StateDelta:
    """A proposed (never directly applied) mutation of continuity (8.1).

    Items/relationships may reference each other through temporary "$name" keys; ids are
    assigned at commit time.
    """
    stream_id: str
    expected_state_version: int
    source_event_ids: list
    primary_classification: str
    confidence: str = "EXPLICIT"
    rationale: str = ""
    summary: str = ""
    delta_id: str = field(default_factory=lambda: new_id("dlt"))
    create_items: list = field(default_factory=list)
    update_items: list = field(default_factory=list)
    authority_changes: list = field(default_factory=list)
    relationships_add: list = field(default_factory=list)
    relationships_remove: list = field(default_factory=list)
    resolve: list = field(default_factory=list)
    reopen: list = field(default_factory=list)
    intent_change: dict | None = None
    focus: dict = field(default_factory=dict)
    trajectory: dict | None = None
    seed_actions: list = field(default_factory=list)
    branch_actions: list = field(default_factory=list)
    snapshot_recommendation: bool = False
    depth: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StateDelta":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class DeltaError(ValueError):
    """A delta that cannot even be made concrete (dangling reference, malformed shape)."""


class ProjectionCorrupt(RuntimeError):
    """The stored current-state projection is missing or fails its checksum."""
