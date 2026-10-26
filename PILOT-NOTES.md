# Forge pilot notes — SRT1

Purpose: feedback for Forge before it is open-sourced. Log every time a hook (or the gate) **blocked something it
should not have** (false positive) and every time something **slipped through** (false negative), plus friction.
Mode: non-strict, no keys, no required CI check. Builder: claude. Owner: Seed Class Intelligence.
Append, do not rewrite; each entry says whether it was seen in real use or only in a probe.

## Decisions (owner-approved in chat, recorded here)

- **Commit-time threshold:** act (incremental load / lazy diff) before a stream passes **~1,000 items**. Measured with
  `bench/commit_scaling.py`: median 84 ms @1,000, crosses 100 ms at ~1,100–1,250 (one container; re-measure elsewhere).
  Source: builder proposed, owner called it sensible, builder accepts. It is a threshold, not a worry.
- **C6 stays blocked.** No synthetic fixture or session-endpoint substitute. The spec's Production gate has not been run.

## Entries

### 1. Session-start hooks could not be confirmed in a fresh session — OPEN (owner action)
- The hooks were written mid-session; this session cannot restart itself, so "fresh session opens with the rules and
  `Next: C2`" is **not confirmed end to end**.
- What was verified instead, by running the hook commands directly with simulated input:
  `hook-session` prints the rules block + `Forge project 'srt1': 6 capabilities … last gate: fail.` + `Next: C2 — …`;
  `doctor` is ok (one warn: no CI workflow); `.claude/settings.json` registers SessionStart, PreToolUse (matcher
  `Edit|Write|MultiEdit|NotebookEdit|Bash`) and Stop with `$CLAUDE_PROJECT_DIR` paths.
- Not covered by that: whether Claude Code actually invokes them from `.claude/settings.json` in a new session
  (project trust, `$CLAUDE_PROJECT_DIR` expansion, cloud-session behaviour). **Owner: open a fresh session in the repo and
  check the first output.** If it is missing, that is a wiring/Forge bug.

### 2. PreToolUse hook probe (direct invocation, not real use)
| Probe | Result |
|---|---|
| Edit/Write `.forge/forge.json`, `.claude/settings.json` | blocked (correct) |
| Edit `srt1/core.py`, a bound test, `PILOT-NOTES.md` | allowed (correct) |
| Bash `forge cap-bind`, `git add -A`, `cat .forge/forge.json` | allowed (correct) |
| Bash `echo x > .forge/forge.json`, `sed -i … .forge/forge.py`, `git checkout -- .forge/forge.json` | blocked (correct) |
| **Bash `sed -i … srt1/core.py  # see .forge/forge.py`** | **blocked — false positive**: a *comment* naming a protected path trips the shell-write regex on an unrelated file |
| **Bash `python3 -c "open(chr(46)+'forge/forge.json','w')…"`** | **slipped through** (obfuscated write). Documented best-effort; CI re-check is the real control and this repo has no CI |
- Real-use note: every repo edit in this build was done with `python3 - <<EOF` rewrites and heredocs. None touched
  protected paths, but the hook cannot tell, so it gave no protection *or* friction for that route.

### 3. Stop hook probe (direct invocation, simulated transcript)
- "C2 is done and all tests pass now" with a failing last gate: blocked twice, third attempt passed through and is logged
  in `.forge/overrides.jsonl` (as designed). An honest status ("open and failing … not verified") was not blocked.
  Probe state files were removed afterwards.

### 4. `forge mutate` credits already-red capabilities as killers — friction (real use)
- Output said `killed … (by C1,C2,C3,C4,C5)`. C2–C4 fail at baseline, so they "kill" every mutation trivially.
  `c5-no-source-claims` was reported killed while **only** red capabilities caught it — a real coverage hole hidden by the
  noise (found by reading the list, not by the tool). Suggestion: exclude capabilities that fail on the unmutated tree, and
  report per-mutation "killed by a passing-at-baseline test".
- Also: each mutation re-runs every bound suite; the 3-mutation run exceeded 120 s. A `--only C5` filter would help.

### 5. Test-first flag worked (real use)
- C5's test passed on its first Forge run; Forge printed "never seen failing, so test-first is unproven". Correct: the
  failure had been observed before binding, outside Forge. Mitigated with `mutate`. Not a defect; the warning was useful.

### 6. Roadmap-order warning is noise when order is deliberate (real use)
- `C5 passed while C2 is unresolved — roadmap worked out of order`. C5 was done first on the owner's instruction.
  Suggestion: let the owner mark an explicit order or waive the warning with a reason.

### 7. Rebind prints an owner-ack command (real use) — informational
- Rebinding C5 prints `forge ack rebind C5:… --note …`. In non-strict mode nothing blocks if it is never run, so a
  rebind is visible but not enforced. Consistent with the stated pilot mode.

### 8. Real defect found by the heavier concurrency run (SRT1, not Forge)
- At 12 writers × 15 writes, ~25% of runs failed (`UNIQUE constraint failed: continuity_events.stream_id,
  resulting_state_version`, 96 of 180 events FAILED in one run). Root cause: a torn read (separate SELECTs on
  `living_state` and `streams`) was mistaken for a corrupt projection, so a writer ran `recover()` from a stale replay
  and rewound the projection while others were committing. Fix: consistent read snapshot for `load_model`, and
  recovery only under the write lock after re-checking the damage. Details in the final entry below.

### 8 (final). Concurrency: what the sweep found and the fixes (SRT1 defects, not Forge)
- Two real defects, both only visible above 6 writers: (a) torn read → spurious "corrupt projection" → stale `recover()`
  rewound the projection under live writers (12 writers: 6/8 runs passed); (b) rebase budget exhausted at 20 writers
  (1 event left DEFERRED). Fixes: snapshot reads, recovery only under the write lock after re-checking, bounded
  optimistic retries then one serialised attempt (liveness), savepoints for nested transactions.
- After the fixes (this container, SQLite file, `SRT1_C5_PROCS`/`SRT1_C5_PER`): 6×15 6/6, 12×15 15/15, 20×15 10/10,
  32×10 4/4. Not tested: >32 writers, network filesystems, non-SQLite backends. If only one process will ever write a
  stream, that is the simplest guarantee; multi-writer is now tested, not assumed.

### 9. Mutation outcome (real use)
- Run 1: 7/10 killed. Survivors exposed three weak tests (archived stream stored input; return point re-anchoring
  unobservable; torn read only caught by a rare race). One targeted test each, rebound with reasons. Run 2: **10/10
  killed**, each by the capability that targets it (the expected-version check is killed by C1's stale-delta test, not C5).
- Caveat that matters: 10/10 is only as strong as the 10 mutations I thought of.

### 10. Gate headline vs. a blocked top-level capability — friction (real use)
- `GATE: PASS (blocked, NOT built: C6)` / report `Status: GATE PASSED — with: 1 capability blocked…`. C6 is the spec's
  Production gate. The qualifier is present but the first words read as success. The owner's rule here is "never describe
  the Production gate as passing". Suggestion: let the owner mark a capability **required**, so blocked = gate FAIL, or lead
  the status line with the blocked count (`BLOCKED 1 — gate passed otherwise`).

### 11. Hook-session check, still open
- Entry 1 stands: a real fresh session has not been observed by me. Owner check outstanding.

### 12. C6 is now REQUIRED — this project runs a patched Forge (owner-requested; review it)
- Owner asked for C6 (spec Production gate) to be required. Forge had no such concept (entry 10), so Forge itself was changed:
  `cap-add --required`, new `cap-require <id> --reason`, and `problems()` now makes a **blocked required** capability a
  hard failure; `status` and `REPORT.md` label it REQUIRED. Result: `GATE: FAIL`, `Status: NOT DONE`.
- **Disclosure:** the builder changed the tool that gates the builder. The change only tightens the gate, but treat it as
  untrusted until reviewed. Review it: `pilot/forge-upstream/required-capability.patch` (68 lines against the Forge in
  `forge.skill`). Its tests are `pilot/forge-upstream/test_required.py` (9); Forge's own suite with them: 109/109 pass.
  The change was made in a scratch copy and vendored with `forge install` (not by editing `.forge/` in place).
- **Owner actions:** (a) review the patch; (b) `forge ack forge-update 7718c3b322ee --note '<reviewed the diff>'` — a CI
  baseline check will demand it once CI exists, because the vendored `forge.py` now differs from `forge.skill`'s;
  (c) decide whether to take the patch upstream (`forge.skill` in this repo is untouched).
- Known limits of the patch: `required` is one-way (no un-require command, deliberately) and the CI baseline check does not
  yet flag a lost `required` flag (direct edits of `forge.json` still trip the hash chain). Neither is tested.
