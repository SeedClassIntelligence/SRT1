---
name: platform-audit-protocol
description: Use this skill when a comprehensive, pre-deployment, or full-platform audit is needed — not a spot-check. Trigger on phrases like "audit the whole platform," "make sure everything works before we ship," "run a full check," or when the user describes a pattern of finding one bug, fixing it, then finding another (indicating prior audits were incomplete rather than the codebase being unusually buggy). Also trigger when paired with the CAS Auditor agent, whose Audit Protocol this skill supplies with fixed, concrete checklist content instead of agent-improvised scope. Do not use this for a quick check of one specific file or feature — this is the full-scope version.
---

# Platform Audit Protocol

## The Claim Verification Gate (Definition of Done)

This is separate from the nine-category sweep below, and it matters more day-to-day: it governs every single time an agent says a feature is "done," "ready," "working," or "should work now" — not just during a full audit.

**"I wrote the code that should produce this result" and "I confirmed this result actually happens" are different claims. Only the second one earns the word "done."**

The specific failure this closes: a backend route exists, a frontend button exists, both look correct in isolation, no error is thrown, and the agent reports completion — without ever having actually traced the click through to a real response. The code existing is not the same as the code being connected. Wiring gaps like this are invisible to code review and invisible to the agent that wrote both halves and assumed they met in the middle, because nothing in that process ever exercised the actual path a user would take.

**Before stating that a feature is done, ready, or working, the agent must have actually done one of the following — not assumed it, done it:**
- Actually invoked the specific entry point a user or caller would use (clicked the button, called the endpoint, ran the command) and observed the real response, not a hypothetical one.
- Traced the full path end to end at least once: trigger → handler → backend logic → response → whatever the trigger side is supposed to do with that response (render, update state, confirm).
- If the agent cannot actually execute this trace (no test environment available, no way to click a UI in the current session), it must say exactly that — "I've written this but have not been able to verify it executes end to end" — rather than defaulting to a confident "done" because the code looks right.

**"Should work" is not "does work,"** and a report is not allowed to blur the two. If an agent hasn't verified something, the honest sentence is "this should work based on the code, but I haven't confirmed it executes" — not "this is done." The second sentence is a different, stronger claim, and it needs the verification to back it, every time.

This rule applies constantly, not periodically — it's not something to run once before deployment like the nine-category sweep below. It's the standard for every individual completion claim made along the way, which is where the "click it, nothing happens" experience actually gets prevented — before it ever reaches you, not caught later in a separate audit pass.

## Why This Exists

Finding "another bug" repeatedly after each pass usually isn't bad luck — it's a symptom of **sampling instead of auditing.** Each pass checks a plausible-looking subset of the system, reports what it found, and gets treated as complete. The next pass samples a different subset and finds something new. This isn't the codebase being unusually broken; it's the checking process having no fixed boundary around what "checked" means.

The fix isn't "look harder" or "be more thorough" — those are instructions an agent can't actually execute against, because they don't define a stopping point. The fix is removing the agent's discretion over *what counts as checked in the first place.* This skill is that fixed scope. The categories below are not suggestions to adapt loosely — they're the actual test bank. An agent running this protocol works through all of them, every time, and does not report an audit "complete" until every category has a real answer, not until the categories it happened to think of have been checked.

## The Core Rule: Fixed Scope, Not Agent-Invented Scope

The agent running this protocol does not decide what to test. This skill decides that. The agent's job is to actually execute each category's checks against the real system and report real findings — not to write its own test plan from scratch, and not to skip a category because it seems unlikely to have problems. "Seems unlikely" is exactly the judgment that produces sampling instead of auditing.

If the agent believes a category is genuinely inapplicable to this specific platform (e.g., no UI exists, so Usability doesn't apply), it must say so explicitly with a one-line reason — silently skipping a category and silently declaring it inapplicable are different things, and only the second is acceptable.

## The Audit Taxonomy — Nine Fixed Categories

### 1. Functional Correctness
- Does every documented feature actually do what it claims, not just on the happy path but across its stated edge cases?
- Are there features that exist in code but aren't reachable from any real user or system path (dead functionality masking as working functionality)?
- Do documented inputs/outputs match actual behavior, or has the code drifted from its own documentation?

### 2. Interface & Integration Boundaries
- This is where most "another bug" discoveries actually hide — the seams between components, not the components themselves.
- For every point where one module calls another, one service calls an API, or one system hands data to the next: does the receiving side actually handle what the sending side actually sends, not what it's assumed to send?
- Check every boundary explicitly, not just the ones that were recently changed — a boundary that worked yesterday can break silently when something upstream of it changes.

### 3. Error Handling & Failure Modes
- What happens when a dependency is unavailable, a request times out, an input is malformed, or a permission check fails?
- Does the system fail loudly and safely, or does it fail silently and let bad state propagate?
- Are error messages accurate, or do they describe a different failure than what actually happened (a common source of wasted debugging time later)?

### 4. Data Integrity
- Does data survive round-trips (write, read back, confirm it matches)?
- Does data survive the specific transformations this platform performs (migrations, format conversions, serialization)?
- What happens under concurrent access — does simultaneous read/write produce corruption, lost updates, or race conditions?

### 5. Security & Permission Boundaries
- Does every action that should require authorization actually check for it, not just at the UI layer but at every layer that could be reached directly (API, CLI, direct database access)?
- Can a user or process do something the system's own documentation says they shouldn't be able to do?
- Are secrets, credentials, and sensitive data actually protected in transit and at rest, not just assumed to be?

### 6. Performance & Resource Behavior
- Does the system degrade gracefully under load, or does it fail catastrophically past a certain point?
- Are there unbounded operations (loops, queries, memory allocations) that could grow without limit given adversarial or simply large-scale real input?
- Does anything leak — memory, connections, file handles — over sustained operation?

### 7. Structural / Architectural Conformance
- Does the actual implementation match the documented architecture (the blueprints, the BEP packages, the module specs already produced for this platform), or has it drifted?
- Are there components doing work that was supposed to belong to a different component, per the documented separation of concerns?
- This category directly reuses this ecosystem's own BEP documentation as the source of truth — check code against blueprint, not code against itself.

### 8. Regression Coverage
- For every bug fixed previously, does a test exist that would catch it coming back?
- Run the full existing test suite, not just tests related to the current change — a fix in one area silently breaking something in an unrelated area is a common failure mode this category exists to catch.
- Check whether previously fixed bugs are actually still fixed, not just assumed fixed because they were addressed once.

### 9. Deployment Readiness
- Does the build/deploy process itself work end-to-end in an environment that matches production, not just in local development?
- Are environment variables, configuration, and secrets management actually correct for the target environment, not just for the developer's machine?
- Do database migrations, if any, actually run cleanly against a realistic copy of production-shaped data, not just an empty test database?

## The Completion Gate

An audit is not reportable as "complete" or "passed" until all nine categories have one of exactly two outcomes:
- **Checked, with findings** (including "no issues found" as a real finding, distinct from "not checked")
- **Explicitly marked inapplicable, with a one-line reason**

A report that only covers some categories is a **partial audit**, and must be labeled as one explicitly — never presented with the same confidence as a complete pass through all nine. This is the direct mechanism that stops the whack-a-mole pattern: the next person reading the report can see exactly what wasn't covered, instead of assuming silence means it was fine.

## Relationship to the CAS Auditor Agent and Test-First Discipline

This skill supplies the concrete checklist content for the CAS Auditor agent's Audit Protocol, replacing agent-improvised scope with the fixed nine categories above. It also complements — doesn't replace — the Test-First Discipline in the coding-agent doctrine: that discipline governs tests written *before* a specific piece of code is built; this protocol governs a *systematic sweep* across an entire platform, typically before deployment or after a cluster of related changes.

## Honest Limits

This protocol dramatically reduces incremental, non-systematic bug discovery by removing agent discretion over scope. It does not, and no testing protocol can, guarantee the complete absence of defects — that's a structural limit of testing itself (testing demonstrates the presence of bugs, not their total absence), not a shortfall in this skill's design. What changes is that gaps become visible and named, instead of invisible and assumed away.
