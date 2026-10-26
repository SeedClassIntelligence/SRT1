"""Continuity retrieval: current state first, progressive escalation, completeness check (spec 12, 15).

Order is fixed (12.1): Living State -> relevant items + lineage -> snapshots -> targeted original
source -> report ambiguity. Whole-history reading never happens by default. Budgets are advisory
and never override continuity completeness (INV-031).

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

import json
import math
import re

from .models import NON_CURRENT, LIVE_SEED
from .reflection import GENERIC, similarity, terms
from .state import Model, brief, est_tokens, item_text, project, semantic_view  # noqa: F401
from .store import Store

DEPTHS = ["STATE", "LINEAGE", "SNAPSHOT", "SOURCE"]


def _score(text: str, T: set) -> float:
    return similarity(text, " ".join(T)) if T else 0.0


class ContextBuilder:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ public
    def get_context(self, model: Model, task: str, *, budget: int | None = None, depth: str | None = None,
                    include_seeds: bool = True, require_source_refs: bool = True,
                    exact_wording: bool = False) -> dict:
        proj = project(model)
        T = terms(task)
        want = DEPTHS.index(depth) if depth else 0
        pkg = self._level1(model, proj, T, include_seeds, require_source_refs)
        metrics = {"state_tokens": est_tokens(pkg), "lineage_tokens": 0, "snapshot_tokens": 0, "source_tokens": 0}
        escalations, reached = [], 0
        warnings = self._warnings(model)

        def incomplete():
            return self._missing(model, pkg, T, require_source_refs)

        for lvl in (1, 2, 3):
            if lvl <= want or incomplete():
                reached = lvl
                escalations.append(DEPTHS[lvl])
                added = [self._lineage, self._snapshots, self._source][lvl - 1](model, pkg, T, exact_wording)
                metrics[["lineage_tokens", "snapshot_tokens", "source_tokens"][lvl - 1]] = est_tokens(added)
        if exact_wording and reached < 3:
            reached = 3
            escalations.append("SOURCE")
            metrics["source_tokens"] = est_tokens(self._source(model, pkg, T, True))
        missing = incomplete()
        if missing:
            pkg["ambiguities"] = missing  # step 5: preserve / report ambiguity (INV-026)
        pkg["complete"] = not missing
        pkg["warnings"] = warnings
        pkg["retrieval_depth"] = DEPTHS[reached]
        pkg["escalations"] = escalations
        pkg["state_version"] = model.version
        total = est_tokens(pkg)
        pkg["token_report"] = {
            "package_tokens": total, "budget": budget, "budget_exceeded": bool(budget and total > budget),
            **metrics, "historical_tokens": self._historical_tokens(model.stream_id)}
        if budget and total > budget:
            self._trim(pkg, budget)
            pkg["token_report"]["package_tokens"] = est_tokens(pkg)
            pkg["token_report"]["budget_exceeded"] = pkg["token_report"]["package_tokens"] > budget
        return pkg

    def record_metrics(self, stream_id, task, pkg):
        tr = pkg["token_report"]
        self.store.db.execute(
            "INSERT INTO context_metrics(stream_id,task,retrieval_depth,historical_tokens,state_tokens,"
            "lineage_tokens,snapshot_tokens,source_tokens,package_tokens,escalations,state_only,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (stream_id, task[:200], pkg["retrieval_depth"], tr["historical_tokens"], tr["state_tokens"],
             tr["lineage_tokens"], tr["snapshot_tokens"], tr["source_tokens"], tr["package_tokens"],
             len(pkg["escalations"]), int(not pkg["escalations"])))

    def platform_metrics(self, stream_id=None) -> dict:
        sql, args = "SELECT * FROM context_metrics", ()
        if stream_id:
            sql, args = sql + " WHERE stream_id=?", (stream_id,)
        rows = self.store.q(sql, args)
        n = len(rows)
        if not n:
            return {"requests": 0}
        avoided = sum(max(0, r["historical_tokens"] - r["package_tokens"]) for r in rows)
        return {
            "requests": n, "context_tokens_avoided": avoided,
            "full_history_reads_avoided": sum(1 for r in rows if r["retrieval_depth"] != "SOURCE"),
            "average_package_tokens": sum(r["package_tokens"] for r in rows) / n,
            "pct_answered_from_state_only": 100.0 * sum(r["state_only"] for r in rows) / n,
            "pct_escalated": 100.0 * sum(1 for r in rows if r["escalations"]) / n}

    # ------------------------------------------------------------------ level 1: living state
    def _level1(self, model, proj, T, include_seeds, need_refs) -> dict:
        def rel(b):
            return b | {"relevance": round(similarity(b["text"], " ".join(T)), 3)} if T else b | {"relevance": 0}

        def keep(lst, always=False):
            out = [rel(b) for b in lst]
            return [b for b in out if always or b["relevance"] > 0 or not T]

        def strip(lst):
            return [{k: v for k, v in b.items() if need_refs or k != "source_event_ids"} for b in lst]

        pkg = {
            "stream_id": model.stream_id,
            "originating_intent": proj["originating_intent"], "active_intent": proj["active_intent"],
            "current_focus": proj["current_focus"], "current_position": proj["current_position"],
            "current_direction": proj["current_direction"],
            "continuation_point": proj["continuation_point"],
            # Governing material is always supplied (INV-024): corrections and constraints are small and
            # a package that omits one is a failure regardless of token savings (15.3).
            "applicable_corrections": strip(keep(proj["applicable_corrections"], always=True)),
            "active_constraints": strip(keep(proj["active_constraints"], always=True)),
            "governing_decisions": strip(keep(proj["governing_decisions"])),
            "established_content": strip(keep(proj["established_content"])),
            "relevant_unresolved": strip(keep(proj["unresolved_items"])),
            "relevant_seeds": strip(keep(proj["seeds"])) if include_seeds else [],
            "recent_significant_changes": proj["recent_significant_changes"][-3:],
        }
        if proj["current_understanding"]:
            pkg["current_understanding"] = proj["current_understanding"]
        return pkg

    # ------------------------------------------------------------------ completeness (12.5)
    def _pkg_text(self, pkg) -> str:
        parts = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("text", "was", "excerpt", "current_focus", "current_position", "current_direction",
                             "focus", "position", "direction", "purpose"):
                        parts.append(str(v))
                    else:
                        walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(pkg)
        return " ".join(parts)

    def _missing(self, model, pkg, T, need_refs) -> list:
        out = []
        if model.scalars["active_intent"] and not pkg.get("active_intent"):
            out.append("governing intent missing")
        if model.scalars["current_position"] and not pkg.get("current_position"):
            out.append("current position missing")
        if need_refs:
            lacking = [b["item_id"] for k in ("applicable_corrections", "active_constraints", "governing_decisions",
                                              "established_content", "relevant_unresolved")
                       for b in pkg.get(k, []) if not b.get("source_event_ids")]
            if lacking:
                out.append(f"weak provenance for {len(lacking)} item(s)")
        req = {t for t in T if t not in GENERIC}
        if req:
            have = terms(self._pkg_text(pkg))
            if len(req & have) < math.ceil(len(req) / 2):
                absent = sorted(req - have)
                out.append("no continuity found for: " + ", ".join(absent))
        return out

    def _warnings(self, model) -> list:
        rows = self.store.q("SELECT processing_status, COUNT(*) AS n FROM source_events WHERE stream_id=? "
                            "AND processing_status!='PROCESSED' GROUP BY processing_status", (model.stream_id,))
        w = [f"{r['n']} source event(s) {r['processing_status']}: state may lag the latest input" for r in rows]
        n = len(self.store.open_clarifications(model.stream_id))
        if n:
            w.append(f"{n} clarification(s) pending: a proposed change is awaiting the user")
        return w

    # ------------------------------------------------------------------ level 2: lineage (12.1.2)
    def _lineage(self, model, pkg, T, exact) -> dict:
        ids = {b["item_id"] for k in ("applicable_corrections", "active_constraints", "governing_decisions",
                                      "established_content", "relevant_unresolved", "relevant_seeds")
               for b in pkg.get(k, [])}
        for k in ("active_intent", "originating_intent"):
            if pkg.get(k):
                ids.add(pkg[k]["item_id"])
        lin = []
        for r in model.rels.values():
            if r["from_item_id"] in ids or r["to_item_id"] in ids:
                a, b = model.items[r["from_item_id"]], model.items[r["to_item_id"]]
                lin.append({"relationship": r["relationship_type"], "from": brief(a), "to": brief(b),
                            "at_state_version": r["created_state_version"]})
        hist = [brief(i) | {"relevance": round(_score(item_text(i), T), 3)} for i in model.items.values()
                if i["authority_status"] in NON_CURRENT and T and _score(item_text(i), T) > 0
                and i["item_id"] not in ids]
        events = [e for e in self.store.events(model.stream_id)
                  if ids & set(e["affected_item_ids"])][-8:]
        added = {"lineage": lin, "historical_matches": sorted(hist, key=lambda b: -b["relevance"])[:5],
                 "development": [{"version": e["resulting_state_version"], "event_type": e["event_type"],
                                  "classification": e["classification"],
                                  "summary": e["delta"].get("summary", "")} for e in events]}
        pkg.update(added)
        return added

    # ------------------------------------------------------------------ level 3: snapshots (12.1.3)
    def _snapshots(self, model, pkg, T, exact) -> dict:
        found = []
        for snap in reversed(self.store.snapshots(model.stream_id)):
            payload = json.loads(snap["payload"])
            for iid, it in payload["items"].items():
                cur = model.items.get(iid)
                txt = it["content"].get("text", "")
                if T and _score(txt, T) > 0 and (
                        cur is None or cur["authority_status"] != it["authority_status"]
                        or item_text(cur) != txt):
                    found.append({"as_of_state_version": snap["state_version"], "item_id": iid, "text": txt,
                                  "authority_then": it["authority_status"]})
        added = {"snapshot_matches": found[:5]}
        pkg.update(added)
        return added

    # ------------------------------------------------------------------ level 4: original source (12.1.4)
    def _source(self, model, pkg, T, exact) -> dict:
        rows = self.store.source_events(model.stream_id)
        scored = []
        for r in rows.values():
            s = _score(r["content"], T)
            if s > 0:
                scored.append((s, r))
        scored.sort(key=lambda x: (-x[0], x[1]["seq"]))
        picks = [r for _, r in scored[:3]]
        if exact:  # exact wording: add the DIRECT evidence behind the governing items
            wanted = {ref["source_event_id"] for k in ("applicable_corrections", "active_constraints",
                                                       "governing_decisions", "relevant_unresolved")
                      for b in pkg.get(k, []) for ref in [{"source_event_id": s} for s in b.get("source_event_ids", [])]}
            picks += [rows[s] for s in sorted(wanted) if s in rows and rows[s] not in picks][:5]
        excerpts = [{"source_event_id": r["source_event_id"], "excerpt": _excerpt(r["content"], T)} for r in picks]
        added = {"source_excerpts": excerpts}
        pkg.update(added)
        return added

    def _historical_tokens(self, stream_id) -> int:
        r = self.store.one("SELECT COALESCE(SUM(LENGTH(content)),0) AS n FROM source_events WHERE stream_id=?",
                           (stream_id,))
        return math.ceil(r["n"] / 4)

    # ------------------------------------------------------------------ advisory budget
    def _trim(self, pkg, budget):
        """Drop lowest-relevance non-governing material first. Governing items are never dropped."""
        for key in ("source_excerpts", "snapshot_matches", "historical_matches", "development", "lineage",
                    "relevant_seeds", "established_content", "relevant_unresolved", "governing_decisions"):
            lst = pkg.get(key)
            if not lst:
                continue
            lst.sort(key=lambda b: b.get("relevance", 0) if isinstance(b, dict) else 0)
            while lst and est_tokens(pkg) > budget:
                if key in ("relevant_unresolved", "governing_decisions") and lst[-1].get("relevance", 0) > 0 \
                        and len(lst) == 1:
                    break
                lst.pop(0)
            if est_tokens(pkg) <= budget:
                return


def _excerpt(text: str, T: set, width: int = 240) -> str:
    low = text.lower()
    pos = min((low.find(t) for t in T if t in low), default=0)
    pos = max(0, pos)
    start = max(0, pos - width // 3)
    return text[start:start + width].strip()


def render(pkg: dict) -> str:
    """Prompt-ready rendering of a ContinuityPackage."""
    L = []
    if pkg.get("active_intent"):
        L.append("ACTIVE INTENT: " + pkg["active_intent"]["text"])
    if pkg.get("originating_intent") and pkg["originating_intent"] != pkg.get("active_intent"):
        L.append("ORIGINATING INTENT: " + pkg["originating_intent"]["text"])
    for k in ("current_focus", "current_position", "current_direction"):
        if pkg.get(k):
            L.append(f"{k.replace('_', ' ').upper()}: {pkg[k]}")
    for k, label in (("applicable_corrections", "CORRECTION"), ("active_constraints", "CONSTRAINT"),
                     ("governing_decisions", "DECISION"), ("established_content", "ESTABLISHED"),
                     ("relevant_unresolved", "UNRESOLVED"), ("relevant_seeds", "SEED")):
        for b in pkg.get(k, []):
            extra = "".join(f" (was: {c['was']})" for c in b.get("corrects", []))
            L.append(f"{label}: {b['text']}{extra}")
    for e in pkg.get("source_excerpts", []):
        L.append(f"SOURCE[{e['source_event_id']}]: {e['excerpt']}")
    for a in pkg.get("ambiguities", []):
        L.append("AMBIGUITY: " + a)
    return "\n".join(L)
