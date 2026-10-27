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
| Model-backed reflection: prose -> proposed ops -> deterministic compile; record/replay completers (§7, 8.3) | `srt1/llm_proposer.py` |
| Conservative push, six triggers + micro-reinjection (§13.2–13.3) | `srt1/push.py` |
| Pull integration: model tool definitions, narrow queries, consuming-model turn loop (§13.1, build step 12) | `srt1/integration.py` |
| Acceptance matrix (§18), long-history and economy validation (§19) | `tests/` |

## Plugging in a consuming model

```python
from srt1.integration import TOOLS, dispatch, Consumer
# 1) tool use: pass TOOLS to your model API, route each tool call through dispatch()
result = dispatch(srt, sid, "srt1_get_context", {"task": "which database do we use?"})
# 2) or let Consumer run the turn: observe user -> pull context -> model -> push check -> observe reply
reply = Consumer(srt, sid, llm=lambda context, user_text: my_model(context, user_text)).turn("how do we load config?")
```

The model gets rendered continuity, never the transcript. If its draft trips a push trigger (superseded material
resurfacing, a constraint about to be ignored, repeated corrections, a relevant open question or seed, drifting from the
active intent) it gets one minimal corrective notice and one retry.

## Run the tests

```
python3 -m unittest discover -s tests -v
```

Python 3.10+, standard library only.

## The reflection layer

The spec says the model *proposes* and SRT1 *validates*. `Proposer` is that seam, with two implementations:

- **`RuleProposer`** (default): deterministic, driven by explicit markers (`INTENT:`, `DECISION:`, `CORRECTION[id]:`,
  `SEED:` ...). Free prose is classified `NO_MATERIAL_CHANGE`.
- **`LLMProposer(complete)`**: reads free prose. `complete(system, user) -> text` is any model callable;
  `AnthropicCompleter()` is the Claude one (`claude-opus-5-5`, refusal fallback on, needs `pip install anthropic` and
  credentials). The model returns a small JSON list of operations about *one* source event, shown only a compact view of
  current state. Operations are compiled by the same rule engine, so: ids the model names must exist in state (a made-up
  id becomes preserved UNRESOLVED uncertainty), text cannot inject extra commands, a non-user source cannot create CURRENT
  authority or change intent, and the model's own confidence caps the delta's confidence (a shaky correction asks the
  user instead of rewriting state). Garbled output is repaired once, then the event is kept as FAILED and is retryable.

```python
from srt1 import SRT1, LLMProposer, AnthropicCompleter
srt = SRT1("srt1.db", proposer=LLMProposer(AnthropicCompleter()))
```

**Testing rule:** the suite never calls a live model. Record a real run once with `RecordingCompleter(inner, "rec.jsonl")`,
commit the JSONL, replay with `ReplayCompleter("rec.jsonl")` (ids are normalised, so recordings survive fresh ids; an
unrecorded prompt is an error). The bundled tests use hand-written *synthetic* model answers: they test the adapter, not
any model's quality, and **no real model has been run against SRT1 yet**.

## Known V1 limits (tracked in Forge, not just here)

Run `python3 .forge/forge.py status` / `report` for the authoritative state; `PILOT-NOTES.md` logs the Forge pilot.

- **C2 COMPLETION, C3 stream status, C4 branch reclaim:** built and passing their bound tests. Not covered by a bound
  test (so unproven): a COMPLETED stream still accepts new input; resuming a PAUSED stream does not auto-reprocess the
  events deferred while paused (use `retry(source_event_id)`).
- **C6 the spec's Production gate (live-model integration) has NOT been run and is not passing.** Blocked: no model key and no
  recorded model output exist. It is not described as passing anywhere.
- **Concurrency (C5):** multi-process writers on one SQLite file are tested; see `PILOT-NOTES.md` for the sweep results and
  the two real defects it found and fixed. Not tested: network filesystems, non-SQLite backends.
- **Whole stream loaded per commit** (`bench/commit_scaling.py`): median ~84 ms at 1,000 items, crosses 100 ms at
  ~1,100-1,250. Owner-approved threshold: act before a stream passes ~1,000 items.
- **Retrieval** is stemmed-term overlap, not embeddings (paraphrases can miss).
- **`RuleProposer`** understands only explicit markers; a real model must be tested against recorded outputs, never live.
