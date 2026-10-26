#!/usr/bin/env python3
"""FORGE v2 — the enforcement spine for building an app from an idea, existing
source, or observed behavior, under the Collaborative Agent Standard (CAS).

The agent does the creative work. This program holds the facts an agent cannot
be trusted to hold about itself, and turns CAS rules into checks that run
whether or not the agent remembers them.

  Mission Roadmap ........ capability ledger, worked in order, out-of-order flagged
  Claim Verification ..... a capability is "passed" only when FORGE ran its test
  Test-First Discipline .. test hashes locked at bind time; edits reopen the capability
  Courage Protocol ....... a capability cannot be blocked without >=2 recorded paths tried
  Hard stops / Reflex 10 . named holds; only the owner can clear them
  CAS Auditor ............ nine fixed audit categories; the builder cannot audit itself
  License gate ........... every borrowed source classified; copyleft needs an owner decision
  Honest report .......... the only approved wording for "done" comes from `forge report`

Three layers, each closing a different way an agent could get around the rules:

  1. LOCAL (always on): state is changed only through forge commands; every change is
     appended to a hash-chained event log; a hand edit of the state file is detected;
     writes are atomic and locked. Claude Code hooks (`forge install`) block completion
     claims that no fresh gate run supports, and block direct edits of forge state.
  2. SIGNED (strict projects): owner and auditor decisions are HMAC-signed with keys
     held outside the builder's environment (FORGE_OWNER_KEY / FORGE_AUDITOR_KEY).
     Project test commands are never run while those keys are in the environment.
  3. CI (root of trust): run the base branch's copy of this file with --baseline so
     the builder cannot rewrite history, weaken a test binding, delete a hold or edit
     this program without a signed owner ack.

Honest limits: nothing here makes a model comply. A builder that has the owner key,
control of CI, or write access to the base branch can defeat it. Tests can be vacuous
(use `forge mutate`). Hooks only exist for Claude Code; other assistants rely on the
AGENTS.md block, the pre-push hook and CI. Downloaded code is never executed by FORGE.
Stdlib only. Python 3.9+.

Exit codes: 0 ok, 1 usage/error, 2 gate failed (work is NOT done).
"""
import argparse, glob, hashlib, hmac, json, os, re, secrets, shutil, signal, subprocess, sys, tempfile, time
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX: no inter-process lock
    fcntl = None

VERSION = "2.0.0"
STATE_PATHS = (".forge/forge.json", "forge.json")
KEY_ENV = {"owner": "FORGE_OWNER_KEY", "auditor": "FORGE_AUDITOR_KEY"}
ORIGINS = ("intent", "claimed", "observed")
HOLD_KINDS = ("stop-condition", "scope", "ambiguity", "uncovered-risk")
ACK_KINDS = ("rebind", "forge-update", "spec", "policy")

# The nine fixed audit categories (platform-audit-protocol). The agent does not
# choose what counts as checked; this list does.
AUDIT_CATEGORIES = {
    "functional": "Functional correctness",
    "integration": "Interface & integration boundaries",
    "errors": "Error handling & failure modes",
    "data": "Data integrity",
    "security": "Security & permission boundaries",
    "performance": "Performance & resource behavior",
    "conformance": "Structural / architectural conformance",
    "regression": "Regression coverage",
    "deployment": "Deployment readiness",
}

# ---------------------------------------------------------------- licenses
# Order matters: AGPL and LGPL must be tested before GPL.
LICENSE_RULES = [
    ("AGPL-3.0", "restricted", r"GNU AFFERO GENERAL PUBLIC LICENSE"),
    ("LGPL", "review", r"GNU LESSER GENERAL PUBLIC LICENSE|GNU LIBRARY GENERAL PUBLIC LICENSE"),
    ("GPL-3.0", "restricted", r"GNU GENERAL PUBLIC LICENSE Version 3"),
    ("GPL-2.0", "restricted", r"GNU GENERAL PUBLIC LICENSE Version 2"),
    ("GPL", "restricted", r"GNU GENERAL PUBLIC LICENSE"),
    ("MPL-2.0", "review", r"Mozilla Public License"),
    ("EUPL/CDDL/EPL", "review", r"European Union Public Licen[cs]e|COMMON DEVELOPMENT AND DISTRIBUTION LICENSE|Eclipse Public License"),
    ("SSPL/BUSL/Commons-Clause", "restricted", r"Server Side Public License|Business Source License|Commons Clause"),
    ("Apache-2.0", "ok", r"Apache License Version 2\.0"),
    ("MIT", "ok", r"Permission is hereby granted, free of charge"),
    ("ISC", "ok", r"Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee"),
    ("BSD", "ok", r"Redistribution and use in source and binary forms"),
    ("Unlicense/CC0", "ok", r"free and unencumbered software released into the public domain|CC0 1\.0 Universal"),
]
SEVERITY = {"ok": 0, "review": 1, "restricted": 2, "blocked": 3}
LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|UNLICENSE)(\..*)?$", re.I)
SKIP_DIRS = {"node_modules", ".git", "vendor", "third_party", "dist", "build", "licenses"}


def classify_text(text):
    """Pass 1 reads the title region only: the real GPL-3 text mentions the
    'GNU Affero General Public License' in section 13, so scanning the whole
    body first would misname it. Pass 2 (title not found near the top) scans
    the body, with copyleft titles matched case-sensitively (they are
    all-caps titles; mixed-case mentions are prose, not the license itself)."""
    flat = re.sub(r"\s+", " ", text)
    head = flat[:1500]
    # The license's own title is the earliest license name in the file; a
    # later passing mention (GPL-2's preamble names the LGPL) must not win.
    hits = []
    for i, (name, verdict, pat) in enumerate(LICENSE_RULES):
        m = re.search(pat, head, re.I)
        if m:
            hits.append((m.start(), i, name, verdict))
    if hits:
        _, _, name, verdict = min(hits)
        return name, verdict
    for name, verdict, pat in LICENSE_RULES:
        flags = re.I if verdict == "ok" else 0
        if re.search(pat, flat, flags):
            return name, verdict
    return "UNRECOGNIZED", "blocked"


def detect_license(root):
    """Scan root and four levels down for license files (commercial 'ee/'
    licenses often sit at packages/<x>/ee/LICENSE); report each, take the
    strictest. Heuristic text matching — a human must confirm anything that is
    not clearly permissive."""
    root = Path(root)
    found = []
    for dirpath, dirs, files in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS] if depth < 4 else []
        for f in files:
            if LICENSE_FILE.match(f):
                p = Path(dirpath) / f
                try:
                    name, verdict = classify_text(p.read_text(errors="replace")[:60000])
                except OSError:
                    continue
                found.append({"file": str(p.relative_to(root)), "license": name, "verdict": verdict})
    declared = None
    pj = root / "package.json"
    if pj.exists():
        try:
            declared = json.loads(pj.read_text()).get("license")
        except Exception:
            pass
    if not found:
        return {"verdict": "blocked", "license": "NONE-FOUND", "files": [], "declared": declared,
                "note": "No license file. Without a license you have no right to reuse the code."}
    worst = max(found, key=lambda x: SEVERITY[x["verdict"]])
    note = ""
    if len({f["license"] for f in found}) > 1:
        note = "Multiple licenses found (dual-license or per-directory terms). Confirm which parts you take."
    return {"verdict": worst["verdict"], "license": worst["license"], "files": found,
            "declared": declared, "note": note}


# ------------------------------------------------------------------ state
def die(msg, code=1):
    print(f"forge: {msg}", file=sys.stderr)
    sys.exit(code)


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def state_sha(st):
    return hashlib.sha256(canon(st).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def state_path(proj):
    for n in STATE_PATHS:
        p = Path(proj) / n
        if p.exists():
            return p
    return None


def log_path(proj):
    sp = state_path(proj)
    if sp is None:
        return None
    return sp.with_name("log.jsonl") if sp.parent.name == ".forge" else sp.with_name("forge.log.jsonl")


def meta_dir(proj):
    d = Path(proj) / ".forge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_state_raw(proj):
    p = state_path(proj)
    if p is None:
        die(f"No forge state in {proj}. Run: forge init <name>   (or, in an existing repo: forge adopt)")
    try:
        st = json.loads(p.read_text())
    except (ValueError, OSError) as e:
        die(f"{p} is corrupt or unreadable ({e}). Restore it from git; the event log beside it shows what it should hold.")
    if not isinstance(st, dict) or "capabilities" not in st:
        die(f"{p} is corrupt: not a forge state file.")
    return st


def load(proj):
    st = read_state_raw(proj)
    st.setdefault("builder", "unnamed-builder")
    for k, v in (("audit", {}), ("holds", []), ("sources", []), ("require_audit", False), ("strict", False),
                 ("keys", {}), ("key_regs", {}), ("acks", []), ("build_dirs", ["build"])):
        st.setdefault(k, v)
    return st


# ---- tamper-evident event log: one JSON line per state change, hash-chained
def read_log(lp):
    ents = []
    if lp is not None and Path(lp).exists():
        for line in Path(lp).read_text().splitlines():
            if line.strip():
                try:
                    e = json.loads(line)
                    ents.append(e if isinstance(e, dict) else {"_corrupt": line})
                except ValueError:
                    ents.append({"_corrupt": line})
    return ents


def entry_hash(prev, e):
    body = {k: v for k, v in e.items() if k not in ("prev", "hash")}
    return hashlib.sha256((prev + canon(body)).encode()).hexdigest()


def chain_problems(ents):
    prev = "0" * 64
    for i, e in enumerate(ents):
        if "_corrupt" in e:
            return [f"event log chain broken at seq {i + 1}: unreadable entry"]
        if e.get("seq") != i + 1 or e.get("prev") != prev or e.get("hash") != entry_hash(prev, e):
            return [f"event log chain broken at seq {i + 1}: the log was edited, reordered or truncated"]
        prev = e["hash"]
    return []


def tamper_problems(proj):
    """Chain broken, or the state file no longer matches the last logged event."""
    ents = read_log(log_path(proj))
    if not ents:
        return []
    out = chain_problems(ents)
    if out:
        return out
    if ents[-1].get("state_sha") != state_sha(read_state_raw(proj)):
        out.append("forge state was edited outside forge (it does not match the last logged event). "
                   "If the edit was intentional, the owner records it with: forge reseal --reason '...'")
    return out


def append_event(proj, st, cmd, detail="", mid=False):
    lp = log_path(proj)
    ents = read_log(lp)
    prev = ents[-1].get("hash", "0" * 64) if ents else "0" * 64
    e = {"seq": len(ents) + 1, "at": now(), "cmd": cmd, "detail": detail, "state_sha": state_sha(st)}
    if mid:
        e["unrecorded_prior_state"] = True
    e["prev"] = prev
    e["hash"] = entry_hash(prev, e)
    with open(lp, "a") as f:
        f.write(json.dumps(e) + "\n")
        f.flush()
        os.fsync(f.fileno())


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(proj, st, cmd=None, detail="", create=None):
    """Write state atomically and append a chained event. Refuses to record over
    a state that was edited outside forge: a later save must not launder the edit."""
    if cmd is None:
        n = sys._getframe(1).f_code.co_name
        cmd = "verify" if n == "run_verifiers" else n.removeprefix("cmd_").replace("_", "-")
    sp = state_path(proj)
    existed = sp is not None
    if existed:
        bad = tamper_problems(proj)
        if bad:
            die(bad[0] + " Refusing to record over it.", 1)
    else:
        sp = Path(proj) / (create or "forge.json")
    atomic_write(sp, json.dumps(st, indent=2) + "\n")
    append_event(proj, st, cmd, detail, mid=existed and not read_log(log_path(proj)))


class Lock:
    """One forge command at a time per project: concurrent writers cannot lose updates."""
    def __init__(self, proj):
        self.path = Path(proj) / ".forge" / "lock"

    def __enter__(self):
        self.f = None
        if fcntl is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.f = open(self.path, "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()


def build_files(proj, st=None):
    out = []
    dirs = (st or {}).get("build_dirs")
    for d in (["build"] if dirs is None else dirs):
        b = Path(proj) / d
        if b.is_dir():
            out += [str(p.relative_to(proj)) for p in b.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    return sorted(out)


# ---- keys and signed decisions (strict projects)
def key_env(role):
    return os.environ.get(KEY_ENV[role]) or None


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def sign(key, payload):
    return hmac.new(key.encode(), canon(payload).encode(), hashlib.sha256).hexdigest()


def need_key(st, role):
    """The key for `role`, from the environment, matching the registered fingerprint."""
    k = key_env(role)
    if not k:
        die(f"this project is strict: the action needs {KEY_ENV[role]} in your environment. "
            f"The {role} holds that key; the builder must not.")
    reg = (st.get("keys") or {}).get(role)
    if not reg:
        die(f"no {role} key is registered for this project (the owner registers it: forge keys-register).")
    if fingerprint(k) != reg:
        die(f"{KEY_ENV[role]} does not match the key registered for the {role} role.")
    return k


def refuse_if_keys():
    """Commands that run project code never run with forge keys in the environment:
    that code is builder-controlled and could read them."""
    held = [v for v in KEY_ENV.values() if os.environ.get(v)]
    if held:
        die(f"refusing to run project test commands while {', '.join(held)} is set in the environment: "
            "tests are builder-controlled code and could read it. Unset it for this command "
            "(`forge signatures` verifies signatures without running any project code).", 1)


def p_hold(st, h):
    return {"p": st["name"], "kind": "hold-resolve", "id": h["id"], "hold_kind": h["kind"], "text": h["text"],
            "owner": h["owner"], "opened": h["opened"], "decision": h.get("decision"),
            "by": h.get("resolved_by"), "at": h.get("resolved")}


def p_audit(st, cat, v):
    return {"p": st["name"], "kind": "audit", "category": cat, "status": v["status"], "note": v["note"],
            "evidence": v.get("evidence", ""), "severity": v.get("severity"), "by": v["by"], "at": v["at"]}


def p_accept(st, s):
    return {"p": st["name"], "kind": "accept-risk", "repo": s["repo"], "license": s["license"],
            "verdict": s["verdict"], "decision": s.get("accepted_risk"), "at": s.get("accepted_at")}


def p_ack(st, a):
    return {"p": st["name"], "kind": "ack", "id": a["id"], "ack_kind": a["kind"], "ref": a["ref"],
            "note": a["note"], "by": a["by"], "at": a["at"]}


def p_keyreg(st, role, reg):
    return {"p": st["name"], "kind": "key-register", "role": role, "fp": reg["fp"], "at": reg["at"]}


def spec_digest(c, only=None):
    """Identity of what a capability promises and how it is checked: its statement,
    the test command, and (optionally restricted to a set of paths) the test files."""
    t = c.get("test") or {}
    files = test_files(t)
    if only is not None:
        files = {k: v for k, v in files.items() if k in only}
    return hashlib.sha256(canon({"s": c["statement"], "c": t.get("cmd"), "f": files}).encode()).hexdigest()[:12]


def spec_ref(st):
    """One identity for 'what done means' right now: every live capability's statement, test command and bound files."""
    items = sorted((k, spec_digest(c)) for k, c in st["capabilities"].items() if c["status"] != "blocked" or c.get("test"))
    return hashlib.sha256(canon(items).encode()).hexdigest()[:12]


def test_files(t):
    if not t:
        return {}
    return t.get("files") or ({t["file"]: t["sha256"]} if t.get("file") else {})


# ---- what is in the working tree: lets a hook tell whether a gate result is still current
_FP_SKIP_DIRS = {".git", ".forge", "sources", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".venv", "venv", ".tox", "target"}
_FP_SKIP_FILES = {"forge.json", "forge.log.jsonl", "REPORT.md", "PROVENANCE.md"}


def _file_digest(p):
    try:
        if p.stat().st_size < 5_000_000:
            return hashlib.sha256(p.read_bytes()).digest()
        return f"{p.stat().st_size}:{p.stat().st_mtime_ns}".encode()
    except OSError:
        return b"?"


def tree_fingerprint(proj):
    root = Path(proj)
    h = hashlib.sha256()

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
            return r.returncode, r.stdout
        except Exception:
            return 1, b""

    rc, out = git("rev-parse", "--is-inside-work-tree")
    if rc == 0 and out.strip() == b"true":
        excl = [":(exclude)" + x for x in (".forge", "sources", *sorted(_FP_SKIP_FILES))]
        h.update(b"git")
        h.update(git("rev-parse", "HEAD")[1])
        h.update(git("diff", "HEAD", "--", ".", *excl)[1])
        for n in sorted(x for x in git("ls-files", "--others", "--exclude-standard", "-z", "--", ".", *excl)[1].split(b"\0") if x):
            h.update(n)
            h.update(_file_digest(root / n.decode(errors="replace")))
        return h.hexdigest()
    h.update(b"walk")
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _FP_SKIP_DIRS)
        for f in sorted(files):
            if f in _FP_SKIP_FILES or f.endswith((".tmp", ".pyc")):
                continue
            p = Path(dirpath) / f
            h.update(str(p.relative_to(root)).encode())
            h.update(_file_digest(p))
    return h.hexdigest()


# ---- running a bound test command: own process group, whole group killed on timeout
def run_cmd(cmd, cwd, timeout, env=None):
    # Never let stale bytecode (same size, same-second mtime) make an edited file look unchanged to the gate.
    env = {**(os.environ if env is None else env), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             start_new_session=(os.name != "nt"), env=env)
    except OSError as e:
        return 127, f"could not start: {e}"
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, (out or "")[-400:]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL) if os.name != "nt" else p.kill()
        except (ProcessLookupError, PermissionError):
            p.kill()
        try:
            p.communicate(timeout=5)
        except Exception:
            pass
        return 124, f"timeout after {timeout}s (process group killed)"


def resolve_targets(proj, specs):
    """Files, directories or globs (relative to the project) -> sorted relative file paths.
    Anything that resolves outside the project is refused."""
    root = Path(proj).resolve()
    out = set()

    def add_file(fp):
        rp = fp.resolve()
        try:
            rel = rp.relative_to(root)
        except ValueError:
            die(f"{fp} resolves outside the project; refusing to bind it.", 1)
        out.add(rel.as_posix())

    def walk(d):
        for dirpath, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git", "node_modules", ".forge")]
            for f in files:
                if not f.endswith(".pyc"):
                    add_file(Path(dirpath) / f)

    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        if ".." in Path(spec).parts:
            die(f"{spec} reaches outside the project; refusing to bind it.", 1)
        base = Path(spec) if Path(spec).is_absolute() else root / spec
        if base.is_dir():
            try:
                base.resolve().relative_to(root)
            except ValueError:
                die(f"{spec} resolves outside the project; refusing to bind it.", 1)
            walk(base)
        elif base.is_file():
            add_file(base)
        else:
            for m in glob.glob(str(base), recursive=True):
                mp = Path(m)
                (walk if mp.is_dir() else add_file)(mp)
    return sorted(out)


# --------------------------------------------------------------- commands
def new_state(name, idea, builder, require_audit=False, strict=False, build_dirs=None):
    return {"name": name, "idea": idea or "", "created": now(), "builder": builder, "require_audit": require_audit,
            "strict": strict, "keys": {}, "key_regs": {}, "acks": [], "capabilities": {}, "sources": [],
            "holds": [], "audit": {}, "build_dirs": ["build"] if build_dirs is None else build_dirs}


def require_owner_key_for_strict():
    key = key_env("owner")
    if not key:
        die("a strict project is created with the owner key in the environment: set FORGE_OWNER_KEY "
            "(make one with `forge keygen`). The owner holds it; the builder must not.")
    return key


def register_owner(st, key):
    fp = fingerprint(key)
    reg = {"fp": fp, "at": now()}
    st["keys"]["owner"] = fp
    reg["sig"] = sign(key, p_keyreg(st, "owner", reg))
    st["key_regs"]["owner"] = reg


def cmd_init(a):
    root = Path(a.dir) / a.name
    if state_path(root):
        die(f"{root} already initialised")
    key = require_owner_key_for_strict() if a.strict else None
    for d in ("sources", "build", "tests"):
        (root / d).mkdir(parents=True, exist_ok=True)
    st = new_state(a.name, a.idea, a.builder, a.require_audit, a.strict)
    if key:
        register_owner(st, key)
    save(root, st, cmd="init", detail=f"project {a.name}", create="forge.json")
    print(f"Initialised {root}  (builder: {a.builder}; full audit required: {a.require_audit}; strict: {a.strict})\n"
          f"  sources/  untrusted pulled code (never run from here)\n"
          f"  build/    your app\n  tests/    tests, written BEFORE the code")


def cmd_cap_add(a):
    st = load(a.project)
    if a.origin not in ORIGINS:
        die(f"origin must be one of {ORIGINS}")
    cid = f"C{len(st['capabilities']) + 1}"
    st["capabilities"][cid] = {"statement": a.statement, "origin": a.origin,
                               "evidence": a.evidence or "", "status": "open", "test": None, "runs": []}
    if a.retrofit:
        st["capabilities"][cid]["retrofit"] = True
    save(a.project, st, detail=f"{cid}: {a.statement}")
    print(f"{cid} [{a.origin}]{' (retrofit: code already exists)' if a.retrofit else ''} {a.statement}")


def cmd_cap_block(a):
    """Courage Protocol: 'I can't' is not accepted without at least two
    genuinely different paths actually tried or considered, each with why it
    did not clear the constraint. Independence is judged by the auditor; FORGE
    enforces that the work was written down."""
    st = load(a.project)
    c = st["capabilities"].get(a.id) or die(f"no capability {a.id}")
    tried = [t.strip() for t in (a.tried or [])]
    if len(tried) < 2 or any(len(t) < 20 for t in tried) or len(set(tried)) < 2:
        die("a capability cannot be blocked without at least two distinct paths tried, each stating "
            "what was attempted and why it failed (--tried 'path A: ... failed because ...' "
            "--tried 'path B: ...'). 'I can't' without that is an unfinished job.", 1)
    c["status"], c["blocked_reason"], c["tried"] = "blocked", a.reason, tried
    save(a.project, st)
    print(f"{a.id} blocked: {a.reason}  ({len(tried)} paths recorded)")


def cmd_cap_bind(a):
    """Bind a verifier: one or more files, directories or globs. Their hashes are
    recorded NOW. If a bound file changes or disappears the capability reopens: a
    test cannot be quietly bent to fit the code (Test-First Discipline, rule 5).
    New files under a bound directory are allowed: a suite may grow, not shrink."""
    st = load(a.project)
    c = st["capabilities"].get(a.id) or die(f"no capability {a.id}")
    files = resolve_targets(a.project, a.file)
    if not files:
        die(f"nothing matched {', '.join(a.file)} — write the test first")
    fmap = {f: sha(Path(a.project) / f) for f in files}
    rebind = c["test"] is not None
    old_sha = c["test"]["sha256"][:12] if rebind else None
    if rebind and not a.reason:
        die("this capability already has a bound test. Rebinding replaces the specification; "
            "state why with --reason, and flag it to the owner (a changed test is a changed definition of done).")
    combined = next(iter(fmap.values())) if len(fmap) == 1 else hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(fmap.items())).encode()).hexdigest()
    c["test"] = {"file": ", ".join(a.file), "specs": list(a.file), "files": fmap, "sha256": combined,
                 "cmd": a.cmd, "bound_at": now(), "build_files_at_bind": build_files(a.project, st)}
    ref = None
    if rebind:
        ref = f"{a.id}:{spec_digest(c)}"
        c["test"]["rebind_reason"] = a.reason
        c.setdefault("rebinds", []).append({"at": now(), "reason": a.reason, "ref": ref, "files": sorted(fmap)})
        # keep the evidence: the earlier test's runs stay on record
        c.setdefault("history", []).extend({**r, "under_test_sha": old_sha} for r in c["runs"])
    c["status"], c["runs"] = "open", []
    save(a.project, st, detail=f"{a.id} -> {len(fmap)} file(s)" + (f" (rebind: {a.reason})" if rebind else ""))
    print(f"{a.id} bound to {len(fmap)} file(s) ({', '.join(a.file)}; sha256 {combined[:12]}…). "
          f"Edits or deletions of them will reopen {a.id}.")
    if rebind:
        print(f"  rebound. The owner acknowledges this change with: forge ack rebind {ref} --note '<why it is acceptable>'"
              + ("  (required: this project is strict)" if st.get("strict") else ""))
    if c["test"]["build_files_at_bind"] and not c.get("retrofit"):
        print(f"  note: {len(c['test']['build_files_at_bind'])} file(s) already in build/. "
              "Test-first is only demonstrable if the test fails before the code that satisfies it exists.")


def cmd_source_add(a):
    st = load(a.project)
    if a.capability not in st["capabilities"]:
        die(f"no capability {a.capability}")
    if st.get("strict") and a.accept_risk:
        die("this project is strict: license risk is accepted only by the owner, with a signature: "
            "forge source-accept <repo> --decision '...'  (needs FORGE_OWNER_KEY).")
    src = a.repo
    if re.match(r"^(https?://|git@)", src):
        dest = Path(a.project) / "sources" / re.sub(r"[^A-Za-z0-9._-]", "_", src.rstrip("/").split("/")[-1].removesuffix(".git"))
        if not dest.exists():
            r = subprocess.run(["git", "clone", "--depth", "1", src, str(dest)], capture_output=True, text=True)
            if r.returncode:
                die(f"clone failed: {r.stderr.strip()[:300]}")
    else:
        dest = Path(src)
        if not dest.is_dir():
            die(f"{src} is not a directory or URL")
    lic = detect_license(dest)
    entry = {"capability": a.capability, "repo": src, "local": str(dest), "taken": a.taken or "",
             "license": lic["license"], "verdict": lic["verdict"], "license_files": lic["files"],
             "declared": lic["declared"], "note": lic["note"], "accepted_risk": a.accept_risk, "added": now()}
    if a.accept_risk:
        entry["accepted_at"] = now()
    st["sources"].append(entry)
    save(a.project, st, detail=f"{a.capability} <- {src} ({lic['license']})")
    print(f"{a.capability} <- {src}\n  license: {lic['license']}  verdict: {lic['verdict'].upper()}")
    for f in lic["files"]:
        print(f"    {f['file']}: {f['license']}")
    if lic["note"]:
        print(f"  note: {lic['note']}")
    if lic["verdict"] in ("restricted", "blocked") and not a.accept_risk:
        print("  GATE: this source fails the gate until removed or the OWNER accepts the risk "
              "(--accept-risk '<owner decision>'). Restricted licenses can obligate your whole app.")
    if lic["verdict"] == "review":
        print("  review: weak copyleft — usable, but obligations apply to the files you take.")


def cmd_source_remove(a):
    """A failing source can be dropped instead of accepted. The removal and its
    reason stay on record so the provenance trail shows what was considered."""
    st = load(a.project)
    keep = [x for x in st["sources"] if x["repo"] != a.repo]
    if len(keep) == len(st["sources"]):
        die(f"no source {a.repo}")
    gone = [x for x in st["sources"] if x["repo"] == a.repo]
    st.setdefault("removed_sources", []).extend(
        {"repo": x["repo"], "license": x["license"], "verdict": x["verdict"], "reason": a.reason, "at": now()} for x in gone)
    st["sources"] = keep
    save(a.project, st)
    print(f"Removed {a.repo} ({len(gone)} entr{'y' if len(gone)==1 else 'ies'}). Reason recorded: {a.reason}")


def cmd_mutate(a):
    """Test-strength evidence. Applies each deliberate bug from a mutations file
    to a COPY of the project, runs every bound test command against it, and
    records which bugs the tests caught. A bug no test catches means the tests
    would also pass a wrong implementation. The original is never modified.
    Mutation file: JSON list of {"name","file","old","new"}; `old` must occur
    exactly once in `file`, otherwise the mutation is reported INVALID (stale)
    and counted against the record."""
    refuse_if_keys()
    st = load(a.project)
    muts = json.loads(Path(a.mutations).read_text())
    cmds = [(cid, c["test"]["cmd"]) for cid, c in st["capabilities"].items()
            if c.get("test") and c["status"] != "blocked"]
    if not cmds:
        die("no bound tests to run mutations against")
    killed, survived, invalid = [], [], []
    for m in muts:
        tmp = Path(tempfile.mkdtemp(prefix="forge-mut-"))
        try:
            dest = tmp / "p"
            shutil.copytree(a.project, dest, ignore=shutil.ignore_patterns("sources", "__pycache__", ".git"))
            fp = dest / m["file"]
            src = fp.read_text() if fp.exists() else ""
            if src.count(m["old"]) != 1:
                invalid.append(m["name"]); print(f"  INVALID   {m['name']} (pattern occurs {src.count(m['old'])}x)"); continue
            fp.write_text(src.replace(m["old"], m["new"], 1))
            by = []
            for cid, cmd in cmds:
                try:
                    r = subprocess.run(cmd, shell=True, cwd=dest, capture_output=True, text=True, timeout=a.timeout,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    if r.returncode != 0:
                        by.append(cid)
                except subprocess.TimeoutExpired:
                    by.append(cid)
            if by:
                killed.append(m["name"]); print(f"  killed    {m['name']}  (by {','.join(by)})")
            else:
                survived.append(m["name"]); print(f"  SURVIVED  {m['name']}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    st["mutation"] = {"at": now(), "by": a.by, "total": len(muts), "killed": len(killed),
                      "survived": survived, "invalid": invalid}
    save(a.project, st)
    print(f"\nMutation check: {len(killed)}/{len(muts)} killed; {len(survived)} survived; {len(invalid)} invalid.")
    if survived:
        print("A survivor means the tests would pass that wrong implementation. Strengthen them (rebind with --reason).")


def cmd_gap(a):
    """Declare scope the idea implies that this build does not cover. Keeps the
    report from implying more was built than was."""
    st = load(a.project)
    st.setdefault("gaps", []).append({"text": a.text, "at": now()})
    save(a.project, st)
    print(f"Gap recorded ({len(st['gaps'])}): {a.text}")


def cmd_source_update(a):
    """Correct what was actually taken from a source (e.g. 'design only, no code copied')."""
    st = load(a.project)
    hits = [x for x in st["sources"] if x["repo"] == a.repo]
    if not hits:
        die(f"no source {a.repo}")
    for x in hits:
        x["taken"] = a.taken
    save(a.project, st)
    print(f"Updated {a.repo}: {a.taken}")


def cmd_source_recheck(a):
    """Re-run license detection on every source (use after upgrading FORGE)."""
    st = load(a.project)
    for x in st["sources"]:
        lic = detect_license(x["local"])
        changed = (lic["license"], lic["verdict"]) != (x["license"], x["verdict"])
        x.update(license=lic["license"], verdict=lic["verdict"], license_files=lic["files"], note=lic["note"], declared=lic["declared"])
        print(f"{x['repo']}: {lic['license']} ({lic['verdict']}){'  ** CHANGED **' if changed else ''}"
              + (f"  [{len(lic['files'])} license files]" if len(lic['files']) > 1 else ""))
    save(a.project, st)


# -------------------------------------------------------------- holds
def cmd_hold_add(a):
    """Hard stop conditions, scope questions, ambiguity, risks the tests don't
    cover. A hold halts the gate until the NAMED owner answers it. 'A hold means
    something only if it gets read.'"""
    st = load(a.project)
    hid = f"H{len(st['holds']) + 1}"
    st["holds"].append({"id": hid, "kind": a.kind, "text": a.text, "owner": a.owner,
                        "impact": a.impact, "cause": a.cause, "tried": a.tried or [],
                        "opened": now(), "status": "open", "decision": None})
    save(a.project, st)
    print(f"{hid} OPEN [{a.kind}] -> {a.owner}: {a.text}\n  Work that depends on this stops here. "
          f"Do not continue on dependent branches while it is open.")


def cmd_hold_resolve(a):
    st = load(a.project)
    h = next((x for x in st["holds"] if x["id"] == a.id), None) or die(f"no hold {a.id}")
    if a.by != h["owner"]:
        die(f"hold {a.id} belongs to '{h['owner']}'. Only the named owner can resolve it "
            f"(a hold the builder clears itself is not a hold).", 1)
    if a.by == st["builder"]:
        die("the builder cannot resolve its own hold", 1)
    key = need_key(st, "owner") if st.get("strict") else None
    h["status"], h["decision"], h["resolved"], h["resolved_by"] = "resolved", a.decision, now(), a.by
    if key:
        h["sig"] = sign(key, p_hold(st, h))
    save(a.project, st, detail=f"{a.id} resolved by {a.by}")
    print(f"{a.id} resolved by {a.by}: {a.decision}")


# -------------------------------------------------------------- audit
def cmd_audit_set(a):
    """One of the nine fixed categories gets exactly one of two outcomes:
    checked (with findings and evidence — 'no issues found' counts, but must be
    stated) or inapplicable (with a one-line reason). The builder cannot fill
    this in: the CAS Auditor is a separate agent."""
    st = load(a.project)
    if a.by == st["builder"]:
        die(f"'{a.by}' is the builder. The builder does not audit its own work — "
            "run this as the CAS Auditor (a separate agent) under its own name.", 1)
    if a.status == "checked" and not a.evidence:
        die("checked requires --evidence (file/line, command, or observed result). "
            "A finding without evidence is a guess with a badge on it.", 1)
    if not a.note or len(a.note.strip()) < 8:
        die("--note is required: the finding (checked) or the one-line reason (inapplicable)", 1)
    key = need_key(st, "auditor") if st.get("strict") else None
    st["audit"][a.category] = {"status": a.status, "note": a.note, "evidence": a.evidence or "",
                               "by": a.by, "at": now(), "severity": a.severity}
    if key:
        st["audit"][a.category]["sig"] = sign(key, p_audit(st, a.category, st["audit"][a.category]))
    save(a.project, st, detail=f"{a.category} {a.status} by {a.by}")
    print(f"{a.category}: {a.status} by {a.by} ({len(st['audit'])}/9)")


def audit_label(st):
    n = len(st["audit"])
    if n == 9:
        return "COMPLETE (9/9)"
    return f"PARTIAL ({n}/9) — missing: {', '.join(k for k in AUDIT_CATEGORIES if k not in st['audit'])}"


# ----------------------------------------------------------- verification
def run_verifiers(a, st):
    for cid, c in st["capabilities"].items():
        t = c.get("test")
        if c["status"] == "blocked" or not t:
            continue
        files = test_files(t)
        root = Path(a.project)
        missing = [f for f in files if not (root / f).exists()]
        changed = [f for f, h in files.items() if f not in missing and sha(root / f) != h]
        if missing:
            c["status"] = "failed"
            c["runs"].append({"at": now(), "result": "test file missing: " + ", ".join(missing[:5])})
            continue
        if changed:
            c["status"] = "reopened"
            c["runs"].append({"at": now(), "result": "test file changed after binding — rebind with a stated reason: "
                                                     + ", ".join(changed[:5])})
            continue
        code, out = run_cmd(t["cmd"], a.project, a.timeout)
        c["status"] = "passed" if code == 0 else "failed"
        c["runs"].append({"at": now(), "exit": code, "tail": out})
        if t.get("specs"):
            c["tests_added_since_bind"] = len(set(resolve_targets(a.project, t["specs"])) - set(files))
    save(a.project, st)


def cmd_verify(a):
    refuse_if_keys()
    st = load(a.project)
    pre = tamper_problems(a.project)
    if pre:
        print_status(st)
        print_problems(pre, [])
        sys.exit(2)
    run_verifiers(a, st)
    print_status(load(a.project))


def problems(st):
    """Returns (hard, warnings). Hard problems mean 'not done'."""
    hard, warn = [], []
    caps = st["capabilities"]
    if not caps:
        hard.append("no capabilities defined — nothing has been specified")
    seen_unresolved = None
    for cid, c in caps.items():
        if c["status"] == "blocked":
            continue
        if not c["test"]:
            hard.append(f"{cid}: no test bound")
        elif c["status"] == "reopened":
            hard.append(f"{cid}: test changed after binding")
        elif c["status"] != "passed":
            hard.append(f"{cid}: {c['status']}")
        if c["status"] != "passed":
            seen_unresolved = seen_unresolved or cid
        elif seen_unresolved:
            warn.append(f"{cid} passed while {seen_unresolved} is unresolved — roadmap worked out of order "
                        "(each step is a checkpoint the next depends on)")
        if c["test"] and c["status"] == "passed":
            runs = c.get("history", []) + c["runs"]  # earliest evidence first, across rebinds
            if c.get("retrofit"):
                warn.append(f"{cid}: retrofit capability — the tests were written after the code, so test-first is not "
                            "demonstrated; its strength rests on the mutation check")
            elif runs and runs[0].get("exit") == 0:
                warn.append(f"{cid}: passed on its first ever run — never seen failing, so test-first is unproven "
                            "(confirm the test is not vacuous)")
            if c["test"].get("build_files_at_bind") and not c.get("retrofit"):
                warn.append(f"{cid}: {len(c['test']['build_files_at_bind'])} file(s) already in build/ when the test was bound")
            if c.get("tests_added_since_bind"):
                warn.append(f"{cid}: {c['tests_added_since_bind']} test file(s) added since binding (additive, allowed)")
        if c.get("rebinds"):
            warn.append(f"{cid}: test was rebound {len(c['rebinds'])}x — the definition of done changed; owner should see why")
    mut = st.get("mutation")
    if any(c["status"] == "passed" for c in caps.values()):
        if not mut:
            warn.append("no mutation check recorded: test strength is unmeasured (passing tests may not catch a wrong implementation)")
        else:
            if mut["survived"]:
                warn.append(f"mutation check: {len(mut['survived'])}/{mut['total']} deliberate bug(s) survived every test: "
                            + "; ".join(mut["survived"][:6]) + (" …" if len(mut["survived"]) > 6 else ""))
            if mut["invalid"]:
                warn.append(f"mutation check: {len(mut['invalid'])} mutation(s) did not apply (stale pattern): " + "; ".join(mut["invalid"][:4]))
    for s in st["sources"]:
        if s["verdict"] in ("restricted", "blocked") and not s["accepted_risk"]:
            hard.append(f"source {s['repo']}: {s['license']} ({s['verdict']}) without an owner decision")
        elif s["verdict"] in ("restricted", "blocked"):
            warn.append(f"source {s['repo']}: {s['license']} used under accepted risk — '{s['accepted_risk']}'")
    for h in st["holds"]:
        if h["status"] == "open":
            hard.append(f"hold {h['id']} [{h['kind']}] open, awaiting {h['owner']}: {h['text']}")
    if st["require_audit"] and len(st["audit"]) < 9:
        hard.append(f"full audit required for this project: {audit_label(st)}")
    for k, v in st["audit"].items():
        if v["status"] == "checked" and v.get("severity") in ("high", "critical"):
            hard.append(f"audit finding ({k}, {v['severity']}): {v['note']}")
    return hard, warn


def print_status(st):
    print(f"\n{st['name']} — {st['idea']}   [builder: {st['builder']}{'; strict' if st.get('strict') else ''}]\n")
    for cid, c in st["capabilities"].items():
        mark = {"passed": "[x]", "blocked": "[B]"}.get(c["status"], "[ ]")
        extra = f": {c['blocked_reason']}" if c["status"] == "blocked" else ""
        print(f"{mark} {cid} ({c['origin']}) {c['statement']}  — {c['status']}{extra}")
    if st["capabilities"]:
        print(f"\nspec ref: {spec_ref(st)}" + ("  (owner approval: forge ack spec <ref>)" if st.get("strict") else ""))
    if st["sources"]:
        print("\nSources:")
        for s in st["sources"]:
            risk = " [owner accepted risk]" if s["accepted_risk"] else ""
            print(f"  {s['capability']}  {s['repo']}  {s['license']} ({s['verdict']}){risk}")
    opens = [h for h in st["holds"] if h["status"] == "open"]
    if opens:
        print("\nOpen holds:")
        for h in opens:
            print(f"  {h['id']} [{h['kind']}] -> {h['owner']}: {h['text']}")
    print(f"\nAudit: {audit_label(st)}\n")


def cmd_status(a):
    print_status(load(a.project))
    for x in tamper_problems(a.project):
        print(f"INTEGRITY: {x}")


def cmd_next(a):
    """Mission Roadmap: the next step is the first unresolved capability."""
    st = load(a.project)
    for cid, c in st["capabilities"].items():
        if c["status"] not in ("passed", "blocked"):
            print(f"Next: {cid} — {c['statement']}  (status: {c['status']})")
            if not c["test"]:
                print("  Write its test first, then: forge cap-bind", cid, "--file ... --cmd ...")
            return
    print("Roadmap clear. Run: forge gate")


# ------------------------------------------------------------- integrity layer
def reseal_events(ents):
    out = []
    for e in ents:
        if e.get("cmd") == "reseal":
            try:
                d = json.loads(e.get("detail") or "{}")
            except ValueError:
                d = {}
            out.append((e, d if isinstance(d, dict) else {}))
    return out


def baseline_problems(proj, st, base):
    """Monotonic invariants against a trusted baseline (the base branch in CI):
    history is append-only; nothing the owner relied on was weakened or removed."""
    hard, warn = [], []
    bsp = state_path(base)
    if bsp is None:
        return hard, ["baseline has no forge state (first adoption): history and invariants were not compared"]
    try:
        bst = json.loads(bsp.read_text())
    except (ValueError, OSError):
        return ["baseline forge state is unreadable"], warn
    acks = {(a.get("kind"), a.get("ref")) for a in st.get("acks", [])}
    if bst.get("strict") and not st.get("strict"):
        hard.append("strict mode lowered vs baseline")
    if bst.get("require_audit") and not st.get("require_audit"):
        hard.append("the full-audit requirement was lowered vs baseline")
    for role, fp_ in (bst.get("keys") or {}).items():
        if (st.get("keys") or {}).get(role) != fp_:
            hard.append(f"registered key for the {role} role changed vs baseline")
    bents, ents = read_log(log_path(base)), read_log(log_path(proj))
    if bents and ents[:len(bents)] != bents:
        hard.append("baseline log is not a prefix of the current log (history rewritten)")
    for cid, bc in bst.get("capabilities", {}).items():
        c = st["capabilities"].get(cid)
        if c is None:
            hard.append(f"capability {cid} is missing from current (present in baseline)")
            continue
        if not bc.get("test"):
            continue
        keep = set(test_files(bc["test"]))
        if spec_digest(c, only=keep) != spec_digest(bc, only=keep):
            acked = any(("rebind", rb.get("ref")) in acks and rb["ref"].endswith(":" + spec_digest(c, only=set(rb.get("files", []))))
                        for rb in c.get("rebinds", []) if rb.get("ref"))
            if not acked:
                hard.append(f"capability {cid} changed vs baseline (statement, test command or a bound test file) "
                            "without an acknowledged rebind")
    cur_holds = {h["id"]: h for h in st["holds"]}
    for bh in bst.get("holds", []):
        h = cur_holds.get(bh["id"])
        if h is None:
            hard.append(f"hold {bh['id']} is missing from current (present in baseline)")
            continue
        if (h["text"], h["owner"], h["kind"]) != (bh["text"], bh["owner"], bh["kind"]):
            hard.append(f"hold {bh['id']} changed vs baseline")
        if bh["status"] == "resolved" and (h["status"] != "resolved" or h.get("decision") != bh.get("decision")):
            hard.append(f"hold {bh['id']} resolution changed vs baseline")
    cur_gaps = {g["text"] for g in st.get("gaps", [])}
    for g in bst.get("gaps", []):
        if g["text"] not in cur_gaps:
            hard.append(f"gap '{g['text'][:60]}' is missing from current (present in baseline)")
    have = {s_["repo"] for s_ in st["sources"]} | {r["repo"] for r in st.get("removed_sources", [])}
    for bs in bst.get("sources", []):
        if bs["repo"] not in have:
            hard.append(f"source {bs['repo']} is missing from current (present in baseline)")
    for cat in bst.get("audit", {}):
        if cat not in st["audit"]:
            hard.append(f"audit entry '{cat}' is missing from current (present in baseline)")
    bf, cf = Path(base) / ".forge" / "forge.py", Path(proj) / ".forge" / "forge.py"
    if bf.exists():
        if not cf.exists():
            hard.append("vendored .forge/forge.py is missing from current (present in baseline)")
        elif sha(bf) != sha(cf):
            ref = sha(cf)[:12]
            if ("forge-update", ref) not in acks:
                hard.append(f"vendored .forge/forge.py differs from baseline (new sha {ref}) without an owner ack: "
                            f"forge ack forge-update {ref} --note '<reviewed the diff>'")
    return hard, warn


def integrity(proj, st, strict=False, require_sigs=False, baseline=None, keys=None):
    """Tamper, signature and baseline checks. Returns (hard, warn)."""
    hard, warn = [], []
    keys = keys or {}
    strict = bool(strict or st.get("strict"))
    ents = read_log(log_path(proj))
    if not ents:
        if strict:
            hard.append("no tamper-evident history: the event log is missing or empty")
        else:
            warn.append("no event log: tamper-evidence is unavailable for this (legacy) project")
    else:
        bad = tamper_problems(proj)
        hard += bad
        if not bad and ents[0].get("unrecorded_prior_state"):
            msg = "event log begins mid-history (the state before it was not recorded)"
            if strict:
                hard.append("no tamper-evident history: " + msg)
            else:
                warn.append(msg)
        for e, d in reseal_events(ents):
            warn.append(f"state was resealed by hand at seq {e.get('seq')}: {d.get('reason', '(no reason)')}")
    pin, vend = Path(proj) / ".forge" / "forge.pin", Path(proj) / ".forge" / "forge.py"
    if pin.exists() and vend.exists() and sha(vend) != pin.read_text().strip():
        (hard if strict else warn).append("vendored .forge/forge.py does not match .forge/forge.pin (edited after install)")
    ov = Path(proj) / ".forge" / "overrides.jsonl"
    if ov.exists() and ov.read_text().strip():
        warn.append(f"{len(ov.read_text().splitlines())} claim override(s) recorded: the Stop hook let a completion claim "
                    "through after blocking it twice")
    # ---- signed decisions
    items = []
    for h in st["holds"]:
        if h["status"] == "resolved":
            items.append((f"hold {h['id']} resolution", "owner", p_hold(st, h), h.get("sig")))
    for cat, v in st["audit"].items():
        items.append((f"audit '{cat}'", "auditor", p_audit(st, cat, v), v.get("sig")))
    for sx in st["sources"]:
        if sx.get("accepted_risk"):
            items.append((f"license acceptance for {sx['repo']}", "owner", p_accept(st, sx), sx.get("accept_sig")))
    for ak in st["acks"]:
        items.append((f"ack {ak['id']} ({ak['kind']} {ak['ref']})", "owner", p_ack(st, ak), ak.get("sig")))
    for role, reg in st["key_regs"].items():
        items.append((f"{role} key registration", "owner", p_keyreg(st, role, reg), reg.get("sig")))
    if strict:
        for e, d in reseal_events(ents):
            items.append((f"reseal at seq {e.get('seq')}", "owner",
                          {"p": st["name"], "kind": "reseal", "reason": d.get("reason"), "state_sha": e.get("state_sha")}, d.get("sig")))
    unverified = []
    for desc, role, payload, sig in items:
        if not sig:
            if strict:
                hard.append(f"{desc} is unsigned (hand-written, or made without the {role} key)")
            continue
        k = keys.get(role)
        if not k:
            unverified.append((desc, role))
            continue
        reg = (st.get("keys") or {}).get(role)
        if reg and fingerprint(k) != reg:
            hard.append(f"{KEY_ENV[role]} does not match the registered {role} key; cannot verify {desc}")
            continue
        if not hmac.compare_digest(sign(k, payload), str(sig)):
            hard.append(f"invalid signature on {desc}")
    if unverified:
        roles = sorted({r for _, r in unverified})
        if require_sigs:
            hard.append(f"cannot verify {len(unverified)} signature(s): set {' and '.join(KEY_ENV[r] for r in roles)} "
                        "(CI secrets) and re-run `forge signatures`")
        else:
            warn.append(f"{len(unverified)} signature(s) present but not verified in this environment (no key here); "
                        "`forge signatures` with the keys verifies them")
    if strict:
        have = {(a.get("kind"), a.get("ref")) for a in st["acks"]}
        for cid, c in st["capabilities"].items():
            for rb in c.get("rebinds", []):
                if rb.get("ref") and ("rebind", rb["ref"]) not in have:
                    hard.append(f"{cid}: rebind awaiting owner ack — the owner runs: forge ack rebind {rb['ref']} --note '...'")
    if strict and any(a_.get("kind") == "spec" for a_ in st["acks"]):
        if ("spec", spec_ref(st)) not in {(a_.get("kind"), a_.get("ref")) for a_ in st["acks"]}:
            hard.append(f"spec changed since the owner last approved what done means — the owner runs: forge ack spec {spec_ref(st)} --note '...'")
    if baseline:
        bh, bw = baseline_problems(proj, st, baseline)
        hard += bh
        warn += bw
    return hard, warn


def write_last_gate(proj, st, hard, warn, strict):
    try:
        ssha = state_sha(read_state_raw(proj))
    except SystemExit:
        ssha = None
    rec = {"at": now(), "result": "fail" if hard else "pass", "state_sha": ssha, "tree_sha": tree_fingerprint(proj),
           "strict": bool(strict or st.get("strict")), "audit": audit_label(st), "audit_complete": len(st["audit"]) == 9,
           "hard": hard[:8], "warnings": len(warn), "version": VERSION,
           "blocked": [k for k, c in st["capabilities"].items() if c["status"] == "blocked"]}
    atomic_write(meta_dir(proj) / "last-gate.json", json.dumps(rec, indent=2) + "\n")


def print_problems(hard, warn):
    for w in warn:
        print(f"  ~ {w}")
    for x in hard:
        print(f"  ! {x}")


def cmd_gate(a):
    """The only way to earn the word 'done'. Re-runs every verifier itself."""
    refuse_if_keys()
    st = load(a.project)
    pre = tamper_problems(a.project)        # before anything is saved: a save would launder a hand edit
    if pre:
        print_status(st)
        print_problems(pre, [])
        write_last_gate(a.project, st, pre, [], a.strict)
        print("\nGATE: FAIL — do not report this as done.")
        sys.exit(2)
    run_verifiers(a, st)
    st = load(a.project)
    print_status(st)
    hard, warn = problems(st)
    ih, iw = integrity(a.project, st, strict=a.strict, baseline=a.baseline)
    hard, warn = hard + ih, warn + iw
    print_problems(hard, warn)
    blocked = [k for k, c in st["capabilities"].items() if c["status"] == "blocked"]
    write_last_gate(a.project, st, hard, warn, a.strict)
    if hard:
        print("\nGATE: FAIL — do not report this as done.")
        sys.exit(2)
    print("\nGATE: PASS" + (f" (blocked, NOT built: {', '.join(blocked)})" if blocked else ""))
    if len(st["audit"]) < 9:
        print(f"  Audit is {audit_label(st)} — you may not describe this as audited or fully verified.")
    if blocked:
        print("  Report the blocked capabilities to the owner. They were not built.")


def cmd_signatures(a):
    """Verify every signed decision, the event log and (with --baseline) the history
    invariants. Runs NO project code, so it is safe to run with the keys present."""
    st = load(a.project)
    keys = {r: key_env(r) for r in KEY_ENV if key_env(r)}
    hard, warn = integrity(a.project, st, strict=True, require_sigs=True, baseline=a.baseline, keys=keys)
    print_problems(hard, warn)
    if hard:
        print("\nSIGNATURES: FAIL")
        sys.exit(2)
    print(f"\nSIGNATURES: OK (verified with: {', '.join(sorted(keys)) or 'no keys'})")


def cmd_keygen(a):
    key = secrets.token_hex(32)
    print(f"key: {key}")
    print(f"fingerprint: {fingerprint(key)}")
    print("\nStore the key outside this repo and outside any agent's environment (password manager, CI secret).\n"
          "The owner key is FORGE_OWNER_KEY; the auditor key is FORGE_AUDITOR_KEY. Export one only in the shell of the\n"
          "person or agent that holds that role. Register an auditor with: forge keys-register --role auditor --fingerprint <fp>")


def cmd_keys_register(a):
    st = load(a.project)
    if not st.get("strict"):
        die("keys apply to strict projects (forge init --strict / forge adopt --strict).")
    key = need_key(st, "owner")
    fpv = a.fingerprint.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16}", fpv):
        die("--fingerprint must be the 16-hex fingerprint printed by `forge keygen`.")
    reg = {"fp": fpv, "at": now()}
    reg["sig"] = sign(key, p_keyreg(st, a.role, reg))
    st["keys"][a.role] = fpv
    st["key_regs"][a.role] = reg
    save(a.project, st, detail=f"{a.role} key registered ({fpv})")
    print(f"{a.role} key registered ({fpv}).")


def cmd_ack(a):
    """Owner acknowledgement of a change the builder cannot approve itself:
    a rebound test, an updated forge.py, a policy change."""
    st = load(a.project)
    if a.kind == "rebind":
        pending = {rb["ref"] for c in st["capabilities"].values() for rb in c.get("rebinds", []) if rb.get("ref")}
        if a.ref not in pending:
            die(f"no rebind with ref {a.ref}. Pending: {', '.join(sorted(pending)) or 'none'}")
    if a.kind == "spec" and a.ref != spec_ref(st):
        die(f"the spec has changed or the ref is wrong. Current spec ref: {spec_ref(st)} — review `forge status`, then ack that.")
    ak = {"id": f"A{len(st['acks']) + 1}", "kind": a.kind, "ref": a.ref, "note": a.note.strip(), "by": a.by, "at": now(), "sig": None}
    if len(ak["note"]) < 3:
        die("--note is required: say what was reviewed and why it is acceptable.")
    if st.get("strict"):
        ak["sig"] = sign(need_key(st, "owner"), p_ack(st, ak))
    else:
        del ak["sig"]
    st["acks"].append(ak)
    save(a.project, st, detail=f"{ak['id']} {a.kind} {a.ref}")
    print(f"{ak['id']}: {a.kind} {a.ref} acknowledged by {a.by}.")


def cmd_source_accept(a):
    st = load(a.project)
    hits = [x for x in st["sources"] if x["repo"] == a.repo]
    if not hits:
        die(f"no source {a.repo}")
    key = need_key(st, "owner") if st.get("strict") else None
    for x in hits:
        x["accepted_risk"], x["accepted_at"] = a.decision, now()
        if key:
            x["accept_sig"] = sign(key, p_accept(st, x))
    save(a.project, st, detail=f"license risk accepted for {a.repo}")
    print(f"Owner decision recorded for {a.repo}: {a.decision}")


def cmd_reseal(a):
    """The sanctioned way to accept an intentional manual edit of the state file.
    It is recorded in the log with a reason (and, in strict projects, signed)."""
    raw = read_state_raw(a.project)
    ents = read_log(log_path(a.project))
    bad = chain_problems(ents)
    if bad:
        die("cannot reseal: " + bad[0] + " Restore the log from version control.")
    detail = {"reason": a.reason, "from": (ents[-1].get("state_sha") or "")[:12] if ents else None}
    if raw.get("strict"):
        detail["sig"] = sign(need_key(raw, "owner"), {"p": raw["name"], "kind": "reseal", "reason": a.reason, "state_sha": state_sha(raw)})
    append_event(a.project, raw, "reseal", canon(detail), mid=not ents)
    print(f"Resealed: the current state is now the recorded state. Reason on record: {a.reason}")


def cmd_report(a):
    """Generates the honest report. Final claims to the owner should be this
    text, not the agent's own summary."""
    refuse_if_keys()
    st = load(a.project)
    pre = tamper_problems(a.project)
    if pre:
        print("**Status: NOT DONE**\n")
        print_problems(pre, [])
        sys.exit(2)
    run_verifiers(a, st)
    st = load(a.project)
    hard, warn = problems(st)
    ih, iw = integrity(a.project, st, baseline=getattr(a, "baseline", None))
    hard, warn = hard + ih, warn + iw
    write_last_gate(a.project, st, hard, warn, False)
    L = [f"# Report — {st['name']}", "", f"Generated {now()}. Builder: {st['builder']}. Idea: {st['idea']}", ""]
    verdict = "NOT DONE" if hard else "GATE PASSED"
    quals = []
    nblocked = sum(1 for c in st["capabilities"].values() if c["status"] == "blocked")
    if nblocked:
        quals.append(f"{nblocked} capability blocked, not built")
    if st.get("gaps"):
        quals.append(f"{len(st['gaps'])} known gap(s) not built")
    if len(st["audit"]) < 9:
        quals.append(f"audit {audit_label(st).split(' —')[0]}")
    sev = {}
    for v in st["audit"].values():
        if v["status"] == "checked" and v.get("severity") not in (None, "none"):
            sev[v["severity"]] = sev.get(v["severity"], 0) + 1
    if sev:
        order = ["critical", "high", "medium", "low"]
        quals.append("audit findings: " + ", ".join(f"{sev[k]} {k}" for k in order if k in sev))
    if st.get("strict") and any("not verified in this environment" in w for w in warn):
        quals.append("signatures not verified in this run (CI verifies them)")
    mu = st.get("mutation")
    if mu:
        quals.append(f"test strength: {mu['killed']}/{mu['total']} mutations killed" + (f", {len(mu['survived'])} survived" if mu["survived"] else ""))
    elif any(c["status"] == "passed" for c in st["capabilities"].values()):
        quals.append("test strength unmeasured")
    L += [f"**Status: {verdict}**" + (f" — with: {'; '.join(quals)}" if quals and not hard else ""), ""]
    ver = [(k, c) for k, c in st["capabilities"].items() if c["status"] == "passed"]
    L.append("## Verified (FORGE ran the bound test and it passed)")
    L += [f"- {k}: {c['statement']}  — test `{c['test']['file']}`, last run {c['runs'][-1]['at']}" for k, c in ver] or ["- none"]
    L += ["", "## Not verified"]
    nv = [(k, c) for k, c in st["capabilities"].items() if c["status"] not in ("passed", "blocked")]
    L += [f"- {k}: {c['statement']} — {c['status']}. Wording allowed: \"should work based on the code, not confirmed\"" if c["status"] != "failed"
          else f"- {k}: {c['statement']} — FAILING" for k, c in nv] or ["- none"]
    L += ["", "## Blocked (not built)"]
    for k, c in st["capabilities"].items():
        if c["status"] == "blocked":
            L.append(f"- {k}: {c['statement']} — {c['blocked_reason']}")
            L += [f"    - tried: {t}" for t in c.get("tried", [])]
    if not any(c["status"] == "blocked" for c in st["capabilities"].values()):
        L.append("- none")
    L += ["", "## Known gaps (in scope or implied by the idea, not built, not blocked)"]
    L += [f"- {g['text']}" for g in st.get("gaps", [])] or ["- none declared"]
    L += ["", "## Open holds (need an owner decision)"]
    L += [f"- {h['id']} [{h['kind']}] → {h['owner']}: {h['text']} (impact: {h.get('impact') or 'n/a'})"
          for h in st["holds"] if h["status"] == "open"] or ["- none"]
    L += ["", "## Sources and licenses"]
    L += [f"- {s['repo']}: {s['license']} ({s['verdict']})" + (f" — owner accepted risk: {s['accepted_risk']}" if s["accepted_risk"] else "")
          for s in st["sources"]] or ["- no third-party code"]
    if st.get("removed_sources"):
        L += ["", "## Sources considered and rejected"]
        L += [f"- {r['repo']}: {r['license']} ({r['verdict']}) — {r['reason']}" for r in st["removed_sources"]]
    L += ["", "## Test strength (mutation check)"]
    if st.get("mutation"):
        m = st["mutation"]
        L.append(f"- {m['killed']}/{m['total']} deliberate bugs caught by the tests ({m['at']}, run by {m['by']}).")
        L += [f"- SURVIVED (tests would pass this wrong code): {x}" for x in m["survived"]]
        L += [f"- did not apply (stale): {x}" for x in m["invalid"]]
    else:
        L.append("- not measured")
    L += ["", f"## Audit: {audit_label(st)}"]
    for k, title in AUDIT_CATEGORIES.items():
        v = st["audit"].get(k)
        L.append(f"- {title}: " + (f"{v['status']} by {v['by']} — {v['note']}" if v else "NOT AUDITED"))
    if hard or warn:
        L += ["", "## Open problems"]
        L += [f"- ! {x}" for x in hard] + [f"- ~ {x}" for x in warn]
    L += ["", "## Limits", "- Passing tests confirm the tests were satisfied, not that every requirement is met.",
          "- A partial audit is not an audit. Silence on a category means it was not checked."]
    out = Path(a.project) / "REPORT.md"
    out.write_text("\n".join(L) + "\n")
    print("\n".join(L))
    if hard:
        sys.exit(2)


def cmd_manifest(a):
    st = load(a.project)
    lines = [f"# Provenance — {st['name']}", "", f"Generated {now()}. Idea: {st['idea']}", ""]
    if not st["sources"]:
        lines.append("No third-party source code was incorporated.")
    for s in st["sources"]:
        lines += [f"## {s['repo']}", f"- Used for: {s['capability']} — {st['capabilities'][s['capability']]['statement']}",
                  f"- Taken: {s['taken'] or '(not recorded)'}", f"- License: {s['license']} ({s['verdict']})"]
        for f in s["license_files"]:
            lines.append(f"  - {f['file']}: {f['license']}")
        if s["accepted_risk"]:
            lines.append(f"- Owner decision: {s['accepted_risk']}")
        if s["note"]:
            lines.append(f"- Note: {s['note']}")
        lines.append("")
    for r in st.get("removed_sources", []):
        lines += [f"## (rejected) {r['repo']}", f"- License: {r['license']} ({r['verdict']})", f"- Not used: {r['reason']}", ""]
    out = Path(a.project) / "PROVENANCE.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"Wrote {out}")


def cmd_license(a):
    print(json.dumps(detect_license(a.dir), indent=2))


# ------------------------------------------------------------- hooks (Claude Code)
RULES_TEXT = """\
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
"""

CLAIM_RE = re.compile(r"\b(?:done|complete[d]?|finished|ready|fixed|resolved|works|verified|shipped|implemented|"
                      r"(?:all|every)\s+(?:tests?\s+)?(?:pass(?:es|ed)?|green)|tests?\s+pass(?:es|ed)?)\b", re.I)
HEDGE_RE = re.compile(r"(?:\b(?:not|never|no|should|might|may|could|would|unverified|untested|yet|pending|unable|cannot|todo|if|"
                      r"once|until|whether|still|remaining|open|expect|expected|hope|hopefully|likely|probably|will|going|next|"
                      r"plan|let|lets)\b|n't|'ll)", re.I)
AUDIT_RE = re.compile(r"\b(?:fully\s+)?audited\b|\bfully\s+verified\b", re.I)
TESTCMD_RE = re.compile(r"\b(?:pytest|py\.test|unittest|tox|npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+test|jest|vitest|mocha|"
                        r"cargo\s+(?:test|check|build)|go\s+(?:test|build|vet)|make\s+(?:test|check)|mvn|gradle|ctest|rspec|"
                        r"phpunit|dotnet\s+test|tsc|forge\.py\s+(?:gate|verify|report))\b", re.I)

_PTOK = (r"(?:(?<![\w.-])forge\.json(?![\w-])|(?<![\w.-])forge\.log\.jsonl(?![\w-])|(?<![\w.-])\.forge(?:/|(?![\w.-]))|"
         r"(?<![\w.-])\.claude/settings(?:\.local)?\.json(?![\w-])|(?<![\w.-])\.claude/hooks)")
PROTECTED_PATHS = [re.compile(x) for x in (r"(^|/)forge\.json$", r"(^|/)forge\.log\.jsonl$", r"(^|/)\.forge(/|$)",
                                           r"(^|/)\.claude/settings(\.local)?\.json$", r"(^|/)\.claude/hooks(/|$)")]
SHELL_WRITES = [re.compile(x) for x in (
    r">>?\s*[\"']?[^\s\"'|;&<>]*" + _PTOK,
    r"\b(?:tee|truncate|shred|rm|mv|cp|install|ln|chmod|chown|dd|unlink|rsync|touch)\b[^|;&\n]*" + _PTOK,
    r"\b(?:sed|perl|ruby)\s+(?:-[A-Za-z0-9]*\s+)*-[A-Za-z]*i[^|;&\n]*" + _PTOK,
    r"\bgit\s+(?:checkout|restore|reset|stash|clean|rm|mv|apply|am)\b[^|;&\n]*" + _PTOK,
    r"open\(\s*[\"'][^\"']*" + _PTOK + r"[^\"']*[\"']\s*,\s*[\"'][wax+]",
    r"(?:write_text|write_bytes|writeFile(?:Sync)?|json\.dump|shutil\.(?:copy|move)\w*|os\.(?:remove|unlink|rename|replace))\b[^\n]*" + _PTOK,
)]


def hook_input():
    try:
        d = json.loads(sys.stdin.read() or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def read_transcript(tp):
    msgs = []
    for line in Path(tp).read_text(errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") in ("user", "assistant") and isinstance(d.get("message"), dict):
            msgs.append(d)
    return msgs


def msg_blocks(m):
    c = m["message"].get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def is_human_prompt(m):
    if m.get("type") != "user" or m.get("isMeta"):
        return False
    bl = msg_blocks(m)
    return (any(b.get("type") == "text" and (b.get("text") or "").strip() for b in bl)
            and not any(b.get("type") == "tool_result" for b in bl))


def claim_sentences(text):
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"`[^`\n]*`", " ", t)
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", t):
        x = sent.strip()
        if x and not x.endswith("?") and CLAIM_RE.search(x) and not HEDGE_RE.search(x):
            out.append(x)
    return out


def stop_reason(proj, data):
    """None to allow the stop; (reason, claim_sentence) to block it."""
    tp = data.get("transcript_path")
    if not tp or not Path(tp).is_file():
        return None
    msgs = read_transcript(tp)
    idx = max([i for i, m in enumerate(msgs) if is_human_prompt(m)], default=-1)
    turn = msgs[idx + 1:]
    final = ""
    for m in turn:
        if m.get("type") == "assistant":
            txt = "\n".join(b.get("text", "") for b in msg_blocks(m) if b.get("type") == "text")
            if txt.strip():
                final = txt
    claims = claim_sentences(final)
    if not claims:
        return None
    claim = claims[0]
    if state_path(proj):
        bad = tamper_problems(proj)
        if bad:
            return bad[0], claim
        lg = Path(proj) / ".forge" / "last-gate.json"
        try:
            rec = json.loads(lg.read_text())
        except (OSError, ValueError):
            return "no gate run is on record for this project", claim
        if rec.get("result") != "pass":
            first = (rec.get("hard") or ["see `forge gate`"])[0]
            return f"the last gate run failed: {first}", claim
        if rec.get("state_sha") != state_sha(read_state_raw(proj)) or rec.get("tree_sha") != tree_fingerprint(proj):
            return "the project changed since the last gate run, so that result no longer describes it", claim
        if any(AUDIT_RE.search(c) for c in claims) and not rec.get("audit_complete"):
            return f"the claim says audited/fully verified, but the audit is {rec.get('audit')}", claim
        return None
    test_ids = set()
    for m in turn:
        for b in msg_blocks(m):
            if b.get("type") == "tool_use" and b.get("name") == "Bash" and TESTCMD_RE.search(str((b.get("input") or {}).get("command", ""))):
                test_ids.add(b.get("id"))
    for m in turn:
        for b in msg_blocks(m):
            if b.get("type") == "tool_result" and b.get("tool_use_id") in test_ids and not b.get("is_error"):
                return None
    return "no passing test or build run was found in this turn", claim


def cmd_hook_stop(a):
    data = hook_input()
    proj = a.project
    try:
        res = stop_reason(proj, data)
    except BaseException as e:           # a broken hook must never wedge the session
        print(f"forge hook-stop: internal error ignored ({type(e).__name__}: {e})", file=sys.stderr)
        sys.exit(0)
    if not res:
        sys.exit(0)
    reason, claim = res
    script = os.path.abspath(__file__)
    sid = str(data.get("session_id") or "session")
    hs = meta_dir(proj) / "hook-state.json"
    try:
        stt = json.loads(hs.read_text())
    except (OSError, ValueError):
        stt = {}
    n = stt.get(sid, 0) if data.get("stop_hook_active") else 0
    if n >= 2:
        stt[sid] = 0
        atomic_write(hs, json.dumps(stt))
        with open(meta_dir(proj) / "overrides.jsonl", "a") as f:
            f.write(json.dumps({"at": now(), "session": sid, "claim": claim[:200], "reason": reason}) + "\n")
        sys.exit(0)
    stt[sid] = n + 1
    atomic_write(hs, json.dumps(stt))
    print(f"FORGE: completion claim blocked — {reason}.\n"
          f"The claim: \"{claim[:160]}\"\n"
          f"Run `python3 {script} gate -p {proj}` and use only the wording its result supports. If you cannot run it, "
          f"say 'should work, not verified'. (Block {n + 1} of 2; after that the claim goes through and is logged for the owner.)",
          file=sys.stderr)
    sys.exit(2)


def cmd_hook_pretool(a):
    data = hook_input()
    try:
        tool, ti = data.get("tool_name"), data.get("tool_input") or {}
        target = None
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            fp_ = str(ti.get("file_path") or ti.get("notebook_path") or "").replace("\\", "/")
            if fp_ and any(r.search(fp_) for r in PROTECTED_PATHS):
                target = fp_
        elif tool == "Bash":
            cmd = str(ti.get("command") or "").replace('\\"', '"').replace("\\'", "'")
            if any(r.search(cmd) for r in SHELL_WRITES):
                target = "a command that writes to forge state, the hooks or the vendored forge.py"
    except BaseException:
        sys.exit(0)
    if target:
        print(f"FORGE: blocked — {target} is protected: forge state, its log, the hooks configuration and the vendored "
              "forge.py change only through forge commands, or by the owner outside this session. If this change is "
              "needed, ask the owner.", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


def cmd_hook_session(a):
    hook_input()
    print(RULES_TEXT)
    try:
        if state_path(a.project):
            st = load(a.project)
            caps = st["capabilities"]
            n = lambda s_: sum(1 for c in caps.values() if c["status"] == s_)
            holds = sum(1 for h in st["holds"] if h["status"] == "open")
            lg = Path(a.project) / ".forge" / "last-gate.json"
            last = "none yet"
            if lg.exists():
                last = json.loads(lg.read_text()).get("result", "?")
            print(f"Forge project '{st['name']}': {len(caps)} capabilities ({n('passed')} passed, {n('blocked')} blocked), "
                  f"{holds} open hold(s), last gate: {last}.")
            for x in tamper_problems(a.project):
                print(f"INTEGRITY: {x}")
            nxt = next((k for k, c in caps.items() if c["status"] not in ("passed", "blocked")), None)
            print(f"Next: {nxt} — {caps[nxt]['statement']}" if nxt else "Roadmap clear: run gate.")
        else:
            print("No forge project here yet: `python3 .forge/forge.py adopt` sets one up.")
    except BaseException as e:
        print(f"(forge status unavailable: {e})")


# ------------------------------------------------------------- adopt / install / doctor
_TEST_FILE_RE = re.compile(r"(^|/)(?:tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(?:py|go)$|\.(?:test|spec)\.[cm]?[jt]sx?$|Test\.java$|_spec\.rb$")
_SUITE_SKIP = {"node_modules", ".git", "venv", ".venv", "target", "dist", "build", "__pycache__", ".forge", "sources", ".tox"}


def detect_test_commands(root):
    root, out = Path(root), []
    pj = root / "package.json"
    if pj.exists():
        try:
            if (json.loads(pj.read_text()).get("scripts") or {}).get("test"):
                out.append(("package.json scripts.test", "npm test"))
        except ValueError:
            pass
    if (root / "Cargo.toml").exists():
        out.append(("Cargo.toml", "cargo test"))
    if (root / "go.mod").exists():
        out.append(("go.mod", "go test ./..."))
    mk = root / "Makefile"
    if mk.exists() and re.search(r"^test:", mk.read_text(errors="replace"), re.M):
        out.append(("Makefile target test", "make test"))
    if (root / "pom.xml").exists():
        out.append(("pom.xml", "mvn -q test"))
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        out.append(("build.gradle", "./gradlew test"))
    if any(root.glob("**/test_*.py")) or any(root.glob("**/*_test.py")) or (root / "pytest.ini").exists():
        out.append(("python tests", "python3 -m pytest -q"))
    return out


def discover_suite(root):
    root, found = Path(root), []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SUITE_SKIP]
        for f in files:
            rel = (Path(dirpath) / f).relative_to(root).as_posix()
            if _TEST_FILE_RE.search(rel) and not f.endswith(".pyc"):
                found.append(rel)
                if len(found) >= 5000:
                    return sorted(found)
    return sorted(found)


def cmd_adopt(a):
    """Put an EXISTING repo under forge: state in .forge/, no build/ directory assumed."""
    root = Path(a.project)
    if not root.is_dir():
        die(f"{root} is not a directory")
    if state_path(root):
        die(f"{root} is already under forge")
    key = require_owner_key_for_strict() if a.strict else None
    st = new_state(a.name or root.resolve().name, a.idea, a.builder, a.require_audit, a.strict, a.build_dir or [])
    if key:
        register_owner(st, key)
    save(root, st, cmd="adopt", detail=f"adopted {st['name']}", create=".forge/forge.json")
    found = detect_test_commands(root)
    print(f"Adopted {root} (builder: {a.builder}; strict: {a.strict}). State: .forge/forge.json")
    print("Detected test commands:" if found else "No test command detected: bind one explicitly with cap-bind.")
    for label, cmd in found:
        print(f"  {cmd}   ({label})")
    if a.baseline_suite:
        files = discover_suite(root)
        cmd = a.suite_cmd or (found[0][1] if found else None)
        if not files or not cmd:
            die("--baseline-suite needs test files and a command: pass --suite-cmd '<command>' if none was detected.")
        st = load(root)
        fmap = {f: sha(root / f) for f in files}
        st["capabilities"]["C1"] = {
            "statement": "The existing test suite keeps passing (regression guard for behaviour that already exists)",
            "origin": "observed", "evidence": f"{len(files)} existing test file(s)", "status": "open", "retrofit": True,
            "runs": [], "test": {"file": "(existing suite)", "specs": [], "files": fmap, "cmd": cmd, "bound_at": now(),
                                 "sha256": hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted(fmap.items())).encode()).hexdigest(),
                                 "build_files_at_bind": []}}
        save(root, st, cmd="adopt", detail=f"baseline suite: {len(files)} files, cmd {cmd}")
        print(f"C1 bound to the existing suite ({len(files)} files) with: {cmd}")
    print("Next: `python3 forge.py install` to wire hooks, the AGENTS.md/CLAUDE.md block and CI.")


WORKFLOW_YML = """\
name: forge-gate
# The gate runs the BASE branch's copy of forge.py, so a pull request cannot change the rules it is judged by.
# Required settings (GitHub): make "forge-gate" a required status check, and require CODEOWNERS review for
# .forge/ and .github/workflows/ (the workflow file itself is part of what a pull request can change).
on:
  pull_request:
  push:
    branches: [main, master]
permissions:
  contents: read
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Trusted copy of forge from the base
        id: base
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then BASE="origin/${{ github.base_ref }}"; else BASE="${{ github.event.before }}"; fi
          git worktree add ../forge-base "$BASE" || { echo "::error::cannot check out the base to compare against"; exit 1; }
          if [ -f ../forge-base/.forge/forge.py ]; then echo "present=yes" >> "$GITHUB_OUTPUT"; else
            echo "::notice::forge is not on the base branch yet (first install): gate skipped"; echo "present=no" >> "$GITHUB_OUTPUT"; fi
      - name: Verify signatures and history (holds the keys, runs no project code)
        if: steps.base.outputs.present == 'yes'
        env:
          FORGE_OWNER_KEY: ${{ secrets.FORGE_OWNER_KEY }}
          FORGE_AUDITOR_KEY: ${{ secrets.FORGE_AUDITOR_KEY }}
        run: python3 ../forge-base/.forge/forge.py signatures -p . --baseline ../forge-base
      - name: Gate (runs the project's tests; holds no keys)
        if: steps.base.outputs.present == 'yes'
        run: python3 ../forge-base/.forge/forge.py gate -p . --baseline ../forge-base
"""

PRE_PUSH = """#!/bin/sh
# Convenience only: an agent can bypass local hooks with --no-verify. CI is the enforcement.
exec python3 .forge/forge.py gate -p .
"""

GITIGNORE_LINES = [".forge/last-gate.json", ".forge/hook-state.json", ".forge/lock", ".forge/*.tmp", "forge.json.*.tmp"]
BLOCK_RE = re.compile(r"<!-- forge:begin[^>]*-->.*?<!-- forge:end -->\n?", re.S)
HOOK_EVENTS = {"SessionStart": (None, "hook-session"),
               "PreToolUse": ("Edit|Write|MultiEdit|NotebookEdit|Bash", "hook-pretool"),
               "Stop": (None, "hook-stop")}


def hook_command(sub):
    return f'python3 "$CLAUDE_PROJECT_DIR/.forge/forge.py" {sub} -p "$CLAUDE_PROJECT_DIR"'


def doc_block():
    return f"<!-- forge:begin v{VERSION.split('.')[0]} -->\n{RULES_TEXT}<!-- forge:end -->\n"


def with_block(text):
    blk = doc_block()
    if BLOCK_RE.search(text):
        return BLOCK_RE.sub(lambda m: blk, text, count=1)
    return text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + blk


def settings_has_hook(settings, ev, sub):
    groups = (settings.get("hooks") or {}).get(ev) if isinstance(settings.get("hooks"), dict) else None
    for grp in groups or []:
        for h in (grp.get("hooks") or []) if isinstance(grp, dict) else []:
            c = h.get("command", "") if isinstance(h, dict) else ""
            if "forge.py" in c and sub in c:
                return True
    return False


def cmd_install(a):
    """Wire forge into a repo: vendored forge.py (+ pin), Claude Code hooks, the AGENTS.md /
    CLAUDE.md block, a CI workflow, .gitignore. Idempotent. Never overwrites invalid JSON."""
    root = Path(a.project).resolve()
    if not root.is_dir():
        die(f"{root} is not a directory")
    me = Path(__file__).resolve()
    dry = a.dry_run
    sp = root / ".claude" / "settings.json"
    settings = {}
    if sp.exists():
        try:
            settings = json.loads(sp.read_text() or "{}")
        except ValueError:
            die(f"{sp} is not valid JSON; refusing to touch it. Fix it, then re-run install.")
        if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
            die(f"{sp} has an unexpected shape; refusing to touch it.")
    for ev in HOOK_EVENTS:
        if not isinstance(settings.get("hooks", {}).get(ev, []), list):
            die(f"{sp}: hooks.{ev} is not a list; refusing to touch it.")
    changed = []

    def put(path, text, mode=None):
        path = Path(path)
        rel = path.relative_to(root)
        if path.exists() and path.read_text() == text:
            return
        changed.append(str(rel))
        if dry:
            print(f"would write {rel}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mode:
            path.chmod(mode)
        print(f"wrote {rel}")

    dest = root / ".forge" / "forge.py"
    if me != dest.resolve():
        put(dest, me.read_text(), 0o755)
    text = dest.read_text() if dest.exists() else me.read_text()
    put(root / ".forge" / "forge.pin", hashlib.sha256(text.encode()).hexdigest() + "\n")
    refs = me.parent / "references"
    if refs.is_dir() and refs.resolve() != (root / ".forge" / "references").resolve():
        for f in sorted(refs.iterdir()):
            if f.is_file():
                put(root / ".forge" / "references" / f.name, f.read_text(errors="replace"))
    hooks = settings.setdefault("hooks", {})
    for ev, (matcher, sub) in HOOK_EVENTS.items():
        cmd = hook_command(sub)
        lst = hooks.setdefault(ev, [])
        if not any(isinstance(h, dict) and h.get("command") == cmd for g in lst if isinstance(g, dict) for h in g.get("hooks", [])):
            entry = {"hooks": [{"type": "command", "command": cmd}]}
            if matcher:
                entry = {"matcher": matcher, **entry}
            lst.append(entry)
    put(sp, json.dumps(settings, indent=2) + "\n")
    for doc in ("CLAUDE.md", "AGENTS.md"):
        p_ = root / doc
        put(p_, with_block(p_.read_text() if p_.exists() else ""))
    ci = a.ci if a.ci != "auto" else ("github" if (root / ".github").is_dir() else "none")
    if ci == "github":
        put(root / ".github" / "workflows" / "forge-gate.yml", WORKFLOW_YML)
    gi = root / ".gitignore"
    have = gi.read_text().splitlines() if gi.exists() else []
    missing = [l for l in GITIGNORE_LINES if l not in have]
    if missing:
        put(gi, "\n".join(have + missing) + "\n")
    if a.git_hooks:
        put(root / ".githooks" / "pre-push", PRE_PUSH, 0o755)
        if not dry:
            subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", ".githooks"], capture_output=True)
    print(("Dry run: " if dry else "") + (f"{len(changed)} file(s) " + ("would change." if dry else "changed.") if changed else "already up to date."))
    if not state_path(root):
        print("No forge project here yet: run `python3 .forge/forge.py adopt` (add --strict for signed owner/auditor decisions).")
    print("Owner to-do: make 'forge-gate' a required status check; add CODEOWNERS for .forge/ and .github/workflows/; "
          "for strict projects store FORGE_OWNER_KEY and FORGE_AUDITOR_KEY as CI secrets (never in an agent's environment).")


def cmd_doctor(a):
    root = Path(a.project).resolve()
    bad = 0

    def line(level, msg):
        nonlocal bad
        bad += level == "FAIL"
        print(f"  {level:<4} {msg}")

    print(f"forge doctor — {root}")
    sp = state_path(root)
    if not sp:
        line("FAIL", "no forge project here (run: python3 .forge/forge.py adopt)")
    else:
        try:
            st = load(root)
            line("ok", f"state readable: {sp.relative_to(root)} ({len(st['capabilities'])} capabilities, strict: {st['strict']})")
            tp = tamper_problems(root)
            line("FAIL", tp[0]) if tp else line("ok", "event log chain intact and matches the state")
            if st["strict"]:
                line("ok", "owner key registered") if st["keys"].get("owner") else line("FAIL", "strict project without a registered owner key")
                line("ok", "auditor key registered") if st["keys"].get("auditor") else line("warn", "no auditor key registered yet (forge keys-register)")
        except SystemExit:
            line("FAIL", "forge state is corrupt")
    vend, pin = root / ".forge" / "forge.py", root / ".forge" / "forge.pin"
    if not vend.exists():
        line("FAIL", "vendored .forge/forge.py missing (run: install)")
    elif not pin.exists() or sha(vend) != pin.read_text().strip():
        line("FAIL", "vendored forge.py does not match .forge/forge.pin (edited after install, or pin missing)")
    else:
        line("ok", "vendored forge.py matches its pin")
    settings, sp2 = {}, root / ".claude" / "settings.json"
    try:
        settings = json.loads(sp2.read_text()) if sp2.exists() else {}
    except ValueError:
        line("FAIL", ".claude/settings.json is not valid JSON")
    for ev, (_, sub) in HOOK_EVENTS.items():
        line("ok", f"{ev} hook registered") if settings_has_hook(settings, ev, sub) else line("FAIL", f"{ev} hook missing from .claude/settings.json")
    for doc in ("CLAUDE.md", "AGENTS.md"):
        p_ = root / doc
        line("ok", f"{doc} carries the forge block") if p_.exists() and BLOCK_RE.search(p_.read_text()) else line("FAIL", f"{doc} is missing the forge block")
    line("ok", "CI workflow present") if (root / ".github" / "workflows" / "forge-gate.yml").exists() else line("warn", "no CI workflow: enforcement then relies on local hooks, which an agent session can work around")
    held = [v for v in KEY_ENV.values() if os.environ.get(v)]
    if held:
        line("warn", f"{', '.join(held)} is set in THIS environment: do not run an agent session here")
    print("doctor: " + ("PROBLEMS FOUND" if bad else "ok"))
    sys.exit(2 if bad else 0)


# ------------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(prog="forge", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"forge {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def proj(p):
        p.add_argument("-p", "--project", default=".", help="project directory (default .)")
        return p

    s = sub.add_parser("init"); s.add_argument("name"); s.add_argument("--idea"); s.add_argument("--dir", default=".")
    s.add_argument("--builder", default="builder"); s.add_argument("--require-audit", action="store_true")
    s.add_argument("--strict", action="store_true", help="signed owner/auditor decisions (needs FORGE_OWNER_KEY)")
    s.set_defaults(f=cmd_init)
    s = proj(sub.add_parser("adopt")); s.add_argument("--name"); s.add_argument("--idea", default="")
    s.add_argument("--builder", default="builder"); s.add_argument("--require-audit", action="store_true")
    s.add_argument("--strict", action="store_true"); s.add_argument("--build-dir", action="append")
    s.add_argument("--baseline-suite", action="store_true"); s.add_argument("--suite-cmd"); s.set_defaults(f=cmd_adopt)
    s = proj(sub.add_parser("install")); s.add_argument("--ci", choices=("auto", "github", "none"), default="auto")
    s.add_argument("--git-hooks", action="store_true"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_install)
    s = proj(sub.add_parser("doctor")); s.set_defaults(f=cmd_doctor)
    s = proj(sub.add_parser("cap-add")); s.add_argument("statement"); s.add_argument("--origin", default="intent")
    s.add_argument("--evidence"); s.add_argument("--retrofit", action="store_true", help="the code already exists"); s.set_defaults(f=cmd_cap_add)
    s = proj(sub.add_parser("cap-block")); s.add_argument("id"); s.add_argument("reason")
    s.add_argument("--tried", action="append"); s.set_defaults(f=cmd_cap_block)
    s = proj(sub.add_parser("cap-bind")); s.add_argument("id")
    s.add_argument("--file", required=True, action="append", help="file, directory or glob; repeatable")
    s.add_argument("--cmd", required=True); s.add_argument("--reason"); s.set_defaults(f=cmd_cap_bind)
    s = proj(sub.add_parser("source-add")); s.add_argument("capability"); s.add_argument("repo")
    s.add_argument("--taken"); s.add_argument("--accept-risk"); s.set_defaults(f=cmd_source_add)
    s = proj(sub.add_parser("source-accept")); s.add_argument("repo"); s.add_argument("--decision", required=True)
    s.set_defaults(f=cmd_source_accept)
    s = proj(sub.add_parser("source-remove")); s.add_argument("repo"); s.add_argument("--reason", required=True)
    s.set_defaults(f=cmd_source_remove)
    s = proj(sub.add_parser("mutate")); s.add_argument("--mutations", required=True); s.add_argument("--by", default="builder")
    s.add_argument("--timeout", type=int, default=120); s.set_defaults(f=cmd_mutate)
    s = proj(sub.add_parser("gap")); s.add_argument("text"); s.set_defaults(f=cmd_gap)
    s = proj(sub.add_parser("source-update")); s.add_argument("repo"); s.add_argument("--taken", required=True)
    s.set_defaults(f=cmd_source_update)
    s = proj(sub.add_parser("source-recheck")); s.set_defaults(f=cmd_source_recheck)
    s = proj(sub.add_parser("hold-add")); s.add_argument("kind", choices=HOLD_KINDS); s.add_argument("text")
    s.add_argument("--owner", required=True); s.add_argument("--impact", default=""); s.add_argument("--cause", default="")
    s.add_argument("--tried", action="append"); s.set_defaults(f=cmd_hold_add)
    s = proj(sub.add_parser("hold-resolve")); s.add_argument("id"); s.add_argument("--by", required=True)
    s.add_argument("--decision", required=True); s.set_defaults(f=cmd_hold_resolve)
    s = proj(sub.add_parser("audit-set")); s.add_argument("category", choices=list(AUDIT_CATEGORIES))
    s.add_argument("--status", required=True, choices=("checked", "inapplicable")); s.add_argument("--by", required=True)
    s.add_argument("--note", required=True); s.add_argument("--evidence")
    s.add_argument("--severity", choices=("none", "low", "medium", "high", "critical"), default="none")
    s.set_defaults(f=cmd_audit_set)
    s = proj(sub.add_parser("ack")); s.add_argument("kind", choices=ACK_KINDS); s.add_argument("ref")
    s.add_argument("--note", required=True); s.add_argument("--by", default="owner"); s.set_defaults(f=cmd_ack)
    s = proj(sub.add_parser("reseal")); s.add_argument("--reason", required=True); s.set_defaults(f=cmd_reseal)
    s = proj(sub.add_parser("keys-register")); s.add_argument("--role", required=True, choices=("auditor",))
    s.add_argument("--fingerprint", required=True); s.set_defaults(f=cmd_keys_register)
    s = proj(sub.add_parser("signatures")); s.add_argument("--baseline"); s.set_defaults(f=cmd_signatures)
    s = sub.add_parser("keygen"); s.set_defaults(f=cmd_keygen)
    for name, fn in (("verify", cmd_verify), ("gate", cmd_gate), ("report", cmd_report)):
        s = proj(sub.add_parser(name)); s.add_argument("--timeout", type=int, default=120)
        if name != "verify":
            s.add_argument("--baseline", help="trusted checkout (e.g. the base branch) to compare history against")
        if name == "gate":
            s.add_argument("--strict", action="store_true", help="require signed decisions and an intact event log")
        s.set_defaults(f=fn)
    for name, fn in (("status", cmd_status), ("next", cmd_next), ("manifest", cmd_manifest)):
        s = proj(sub.add_parser(name)); s.set_defaults(f=fn)
    for name, fn in (("hook-stop", cmd_hook_stop), ("hook-pretool", cmd_hook_pretool), ("hook-session", cmd_hook_session)):
        s = proj(sub.add_parser(name)); s.set_defaults(f=fn)
    s = sub.add_parser("license"); s.add_argument("dir"); s.set_defaults(f=cmd_license)

    a = ap.parse_args()
    unlocked = {"init", "adopt", "install", "doctor", "keygen", "license", "hook-stop", "hook-pretool", "hook-session"}
    if hasattr(a, "project") and a.cmd not in unlocked and state_path(a.project):
        with Lock(a.project):
            a.f(a)
    else:
        a.f(a)


if __name__ == "__main__":
    main()
