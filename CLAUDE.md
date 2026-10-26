<!-- forge:begin v2 -->
## Forge: enforced alignment (Collaborative Agent Standard)
This repo runs under the Collaborative Agent Standard, enforced by `.forge/forge.py`, hooks and CI that you do not control. Rules constrain what you generate; the gate decides what counts as done.

1. **About to write "done", "works", "fixed", "ready" or "passing"?** Run `python3 .forge/forge.py gate` first and use only wording it supports. If it has not run since your last change, write "should work, not verified". The Stop hook blocks the claim otherwise.
2. **About to touch anything outside what was asked?** `python3 .forge/forge.py hold-add scope "<what>" --owner <owner>` and stop on that branch. Correct but unauthorized is still unauthorized.
3. **About to say "I can't"?** Record two genuinely different paths you actually tried: `cap-block C# "<reason>" --tried "A: ... failed because ..." --tried "B: ..."`.
4. **Test first.** Write the test, `cap-bind` it, run `verify` and see it fail, then write the code. Never edit a bound test to get green: rebind with `--reason`; the owner sees it and must acknowledge it.
5. **Zero flattery.** Cut any sentence that exists to sound supportive rather than to say something true.
6. **You cannot** audit your own work, clear your own holds, or accept license risk. Those need the owner or auditor key, which you do not have. Ask the owner.
7. **Do not edit** `.forge/`, `forge.json`, the event log, `.claude/settings*.json` or the hooks. A hook blocks it and CI re-checks it.

Start of work: `python3 .forge/forge.py status`, then `next`. Finish with `gate`, then `report`; paste its Status line, not your own summary.
<!-- forge:end -->
