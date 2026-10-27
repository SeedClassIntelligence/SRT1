#!/usr/bin/env python3
"""Run a scenario through SRT1 with a real Claude model and record every model exchange, or replay a recording.

  live:    ANTHROPIC_API_KEY=... python3 scripts/record_llm_run.py tests/scenarios/storage_correction.json --yes
  replay:  python3 scripts/record_llm_run.py tests/scenarios/storage_correction.json --replay      (no key, no network)

Live mode spends real money (one model call per turn, plus a repair call if the model returns invalid JSON), so it
refuses to run without --yes. It always writes the recording, even when expectations fail: the recording is the
evidence. Commit the JSONL under tests/fixtures/llm/ and the replay test starts guarding it.
Exit codes: 0 all expectations met, 1 some failed, 2 could not run (missing SDK/credentials/recording).

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from srt1 import SRT1, AnthropicCompleter, LLMProposer, RecordingCompleter, ReplayCompleter  # noqa: E402
from srt1.scenario import run_scenario  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenario")
    ap.add_argument("--replay", action="store_true", help="replay the recording instead of calling a model")
    ap.add_argument("--out", help="recording path (default tests/fixtures/llm/<scenario name>.jsonl)")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--no-fallback", action="store_true", help="do not opt into server-side refusal fallback")
    ap.add_argument("--db", help="keep the resulting SQLite database here (default: temporary, deleted)")
    ap.add_argument("--yes", action="store_true", help="confirm that a live run may spend money")
    a = ap.parse_args(argv)

    scenario = json.loads(Path(a.scenario).read_text())
    name = scenario.get("name") or Path(a.scenario).stem
    rec = Path(a.out) if a.out else Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "llm" / f"{name}.jsonl"
    turns = len(scenario["turns"])

    if a.replay:
        if not rec.exists():
            print(f"no recording at {rec}. Record one first (live mode), then commit it.", file=sys.stderr)
            return 2
        completer, mode = ReplayCompleter(str(rec)), f"REPLAY of {rec}"
    else:
        if not a.yes:
            print(f"Live run: about {turns} model calls to {a.model} (more if it must repair invalid JSON). "
                  "This spends real money. Re-run with --yes to proceed.", file=sys.stderr)
            return 2
        try:
            inner = AnthropicCompleter(model=a.model, fallback_model=None if a.no_fallback else "claude-opus-4-8")
        except ImportError:
            print("The 'anthropic' package is not installed: pip install anthropic", file=sys.stderr)
            return 2
        except Exception as e:  # missing credentials surface here or on the first call
            print(f"could not create the Claude client: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        if rec.exists():
            print(f"{rec} already exists; refusing to append to or overwrite a committed recording. "
                  "Pass --out for a new file.", file=sys.stderr)
            return 2
        rec.parent.mkdir(parents=True, exist_ok=True)
        completer, mode = RecordingCompleter(inner, str(rec)), f"LIVE {a.model} -> recording {rec}"

    db = a.db or os.path.join(tempfile.mkdtemp(prefix="srt1-run-"), "run.db")
    srt = SRT1(db, proposer=LLMProposer(completer))
    print(f"scenario {name}: {turns} turns; {mode}\n")
    try:
        reports = run_scenario(srt, scenario, stop_on_failure=True)
    except Exception as e:  # the model/network failed mid-run: report what was recorded so far
        print(f"run aborted: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    bad = 0
    for r in reports:
        mark = "ok  " if not r.failures else "FAIL"
        print(f"{mark} turn {r.index:>2} {r.status:<24} {str(r.classification):<12} v{r.state_version}  {r.text[:60]}")
        for f in r.failures:
            bad += 1
            print(f"       - {f}")
    sid = next(iter(srt.store.q("SELECT stream_id FROM streams")))["stream_id"]
    st = srt.get_state(sid)
    print(f"\nfinal: v{st['state_version']}, intent={st['active_intent']['text'][:60] if st['active_intent'] else None!r}, "
          f"{len(st['governing_decisions'])} decision(s), {len(st['active_constraints'])} constraint(s), "
          f"{len(st['unresolved_items'])} unresolved, {len(st['seeds'])} seed(s); verify ok={srt.verify(sid)['ok']}")
    if reports and reports[-1].status == "FAILED":
        last = reports[-1]
        print(f"\nSTOPPED at turn {last.index}: the model call failed ({'; '.join(last.reasons)[:200]}). "
              "The source event was kept; the recording holds the exchanges before it.", file=sys.stderr)
        return 2
    print(f"{bad} expectation(s) failed" if bad else "all expectations met")
    if not a.replay:
        print(f"\nrecording written to {rec} -- review it, then commit it so the replay test guards it.")
    if a.db:
        print(f"database kept at {db}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
