# SRT1 — Seed Reflection Tool

**Created by Seed Class Intelligence.** SRT1 preserves the continuity of developing cognition.
This repository implements *SRT1 Master Build Specification — Canonical V1.0* (the `.docx` in this repo is the
build authority; if code and spec disagree, the spec wins and gets a controlled revision).

## What it does

SRT1 keeps one **Living Continuity State** per stream: originating and active intent (with lineage), what is
established, governing decisions, corrections, constraints, unresolved questions, seeds, branches, trajectory and a
continuation point. New input never mutates that state directly. A proposer suggests a `StateDelta`, a deterministic
validator enforces the invariant registry, and only then is it committed atomically with an append-only
`ContinuityEvent`. Consumers get a task-specific, continuity-complete context package instead of re-reading history.

```python
from srt1 import SRT1, SourceEvent, render

srt = SRT1("srt1.db")                      # SQLite file, or ":memory:"
sid = srt.create_stream("my project")
srt.observe("INTENT: Build a continuity tool", sid)
srt.observe("DECISION: Use SQLite | simple and transactional | Postgres; files", sid)
old = srt.get_state(sid)["governing_decisions"][0]["item_id"]
srt.observe(f"CORRECTION[{old}]: Use Postgres", sid)      # old item is kept, no longer CURRENT
srt.observe("SEED: graph view of lineage", sid)           # captured without changing intent
pkg = srt.get_context(sid, "which database do we use?")   # state first, escalates only if needed
print(render(pkg))
```

Public interface (spec §13): `observe`, `get_state`, `get_context`, `reclaim`, `checkpoint`, `verify`,
`close_session`, plus `recover`, `explain`, `check_push` (conservative push / micro-reinjection),
`answer_clarification`, `submit_delta` (the single mutation path) and `metrics`. Each takes an explicit `stream_id`.

## Layout (spec §20)

| Spec responsibility | Module |
|---|---|
| Canonical vocabularies, value objects | `srt1/models.py` |
| Living state, deterministic delta application, projection | `srt1/state.py` |
| Deterministic validation, invariant registry | `srt1/validation.py` |
| State store (SQLite: sources, items, relationships, events, snapshots) | `srt1/store.py` |
| Reflection / classification → candidate deltas | `srt1/reflection.py` |
| Retrieval, completeness check, escalation, economy metrics | `srt1/retrieval.py` |
| Core: observe → validate → atomic commit → snapshot, recovery, reclaim, verify | `srt1/core.py` |
| Acceptance matrix (§18), long-history and economy validation (§19) | `tests/` |

## Run the tests

```
python3 -m unittest discover -s tests -v
```

Python 3.10+, standard library only.

## The reflection layer: what is real and what is a plug-in

The spec says the model *proposes* and SRT1 *validates*. `Proposer` is that seam. The bundled `RuleProposer` is a
deterministic baseline driven by explicit markers (`INTENT:`, `DECISION:`, `CORRECTION[id]:`, `SUPERSEDE:`, `SEED:`,
`QUESTION:`, `RESOLVE:`, `BRANCH:`/`END BRANCH:`, `PIVOT:` …). It cannot understand free prose — unmarked text is
classified `NO_MATERIAL_CHANGE`, and ungrounded targets are preserved as UNRESOLVED uncertainty rather than guessed.
To use an LLM, wrap a function in `CallableProposer(fn)` that returns a `StateDelta`; the validator and committer
are unchanged, so a bad proposal can be rejected but cannot corrupt continuity.

## Known V1 limits (stated plainly)

- The stream model is loaded whole per commit (fine for thousands of items; needs incremental loading beyond that).
- Relevance is stemmed-term overlap, not embeddings. Sufficient for the completeness/escalation logic, weak for paraphrase.
- Concurrency is verified in-process (BEGIN IMMEDIATE + expected-version checks); multi-process contention is untested.
- `COMPLETION` and stream-status transitions, and reclaiming a branch as a state mutation, are not yet implemented
  (branch *reclamation packages* are returned; the mutation is for seeds and parked unresolved work).
- Live-integration tests (build gate "Production") need a real consuming model and are out of scope here.
