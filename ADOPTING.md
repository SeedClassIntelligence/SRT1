# Using Forge in another repo

Goal: the code assistant in that repo cannot quietly drift from the doctrine, and if it does, the drift is blocked or visible.

## 1. Install (one repo, ~2 minutes)

```
python3 /path/to/forge/forge.py install -p /path/to/repo      # add --dry-run first if you like
cd /path/to/repo
python3 .forge/forge.py adopt --name myapp --idea "what it is" --builder claude
python3 .forge/forge.py adopt ... --baseline-suite --suite-cmd "npm test"   # existing repo with tests: guard current behaviour as C1
python3 .forge/forge.py doctor                                   # exit 2 + FAIL lines if anything is missing
git add -A && git commit -m "Adopt forge"
```

`install` vendors `.forge/forge.py` (+ `forge.pin`, doctrine references), merges hooks into `.claude/settings.json` (refuses invalid JSON, never clobbers), adds a managed doctrine block to `CLAUDE.md` and `AGENTS.md`, a CI workflow, and `.gitignore` lines. Re-running is safe and updates the block.

## 2. What each layer does (and for which assistant)

| Layer | Does | Covers |
|---|---|---|
| CLAUDE.md / AGENTS.md block | States the rules in the assistant's context | Claude Code, Codex, Cursor, Copilot, most others (they read AGENTS.md) |
| SessionStart hook | Injects rules + project status + next capability every session | Claude Code only |
| Stop hook | Blocks "done/works/fixed" unless a gate pass is on record for the *current* tree; 2 blocks, then the claim goes through and is logged in `.forge/overrides.jsonl` | Claude Code only |
| PreToolUse hook | Blocks edits to `.forge/`, `forge.json`, the log, hook config; best-effort on shell writes | Claude Code only |
| Event log | Hash-chained; hand edits to state are detected; `reseal --reason` is the logged way to accept one | all |
| CI workflow | Runs the **base branch's** forge; verifies signatures (keys, no project code), then gates (project code, no keys) with `--baseline` so history can't be rewritten | all (this is the real enforcement) |

Non-Claude assistants get the doctrine block and CI, not the live hooks.

## 3. Owner setup that Forge cannot do for you

1. Make `forge-gate` a **required status check**; require PR review (CODEOWNERS on `.forge/` and `.github/workflows/`). Without this the workflow file is just another thing a PR can edit.
2. Keep the assistant off your admin credentials and out of the shell where keys live.
3. For decisions that must be provably yours (strict mode):
   ```
   export FORGE_OWNER_KEY=$(python3 forge.py keygen)   # prints key and fingerprint; store the key in a password manager
   python3 .forge/forge.py adopt --strict ...            # registers the owner fingerprint
   python3 .forge/forge.py keys-register --role auditor --fingerprint <FP>
   ```
   Put `FORGE_OWNER_KEY` / `FORGE_AUDITOR_KEY` in CI secrets only. Then holds, license-risk acceptance, rebinds, reseals and spec approval need your signature; the assistant has no key.
4. Approve what "done" means: `python3 .forge/forge.py status` shows `spec ref`; `ack spec <ref> --note "..."` signs it. After that, any change to a capability's statement, test command or bound tests makes the gate fail until you re-approve.
5. Run the auditor as a separate agent or person, ideally another vendor's model, with the auditor key.

## 4. Daily loop for the assistant

`status` → `next` → write test → `cap-bind` → `verify` (see red) → code → `gate` → `report`. Anything out of scope: `hold-add`. "I can't": `cap-block` with two real attempts.

## 5. Limits, stated plainly

- Someone holding the owner key, CI admin or base-branch write access can defeat it. That is the point where it must be you.
- Passing tests can still be weak. Use `mutate` and independent oracles.
- Shell-write detection in the PreToolUse hook is best effort (an interpreter can obfuscate a write). CI re-checks the result; the hook is a speed bump, not a wall.
- Hooks are Claude Code only. `--git-hooks` adds a pre-push gate, which `--no-verify` bypasses.
- Auditor independence depends on you actually using a separate agent.
- "100% alignment" is not achievable; layered, checkable enforcement is.
