#!/usr/bin/env python3
"""Verify a public plugin's signed entitlements file (ORG-PLAN-413 B3P).

Runs in the PUBLIC plugin's own pull-request CI. The private parent signs what each
plugin is entitled to (`entitlements.json` + `entitlements.json.sig`, an SSH signature
in the namespace `stromy-entitlements`); this verifier checks the signature against a
public key list the plugin's PR cannot alter, then checks `configured <= entitled`.

Standard library only; needs `ssh-keygen` (present on every hosted runner).

Checks, in order. ANY failure exits 1 and the workflow concludes `failure`, never
`neutral` or `skipped` (GitHub counts both as success):

  1. both files exist at the PR head, `entitlements.json` parses as schema 1 and its
     `repo` equals the PR's repository
  2. the signature verifies under a pinned key (namespace and identity fixed)
  3. freshness: head `seq` >= the base branch's `seq` (no rollback) and `generated_at`
     is at most 45 days old (the writer re-signs at least every 30 days, so a stalled
     writer fails closed instead of freezing stale entitlements)
  4. configured <= entitled: every MCP server in `.agents/mcp.json` is in `entitled`
     (an unused entitlement is valid)
  5. protected paths: the PR edits none of the caller workflow; the entitlements file
     and its signature change only together and with a higher `seq`

`executed=<n>` is printed last; zero executed checks is itself a failure.

Usage:
  verify_entitlements.py --head DIR --base DIR --repo OWNER/NAME --allowed-signers FILE
                         --changed-files FILE [--now ISO8601] [--max-age-days 45]
  --base DIR may be absent or empty on the first PR that adds the files (bootstrap).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess  # noqa: S404 - fixed ssh-keygen argv, no shell
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAMESPACE = "stromy-entitlements"
IDENTITY = "stromy-entitlements"
ENTITLEMENTS = "entitlements.json"
SIGNATURE = "entitlements.json.sig"
MCP_CONFIG = ".agents/mcp.json"
CALLER = ".github/workflows/entitlement-contract.yml"
SCHEMA = 1
MAX_BYTES = 256 * 1024
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")


class Failure(Exception):
    """A check that failed. The message is the whole explanation."""


def _read(path: Path, what: str, root: Path) -> bytes:
    """Read a regular file inside its checkout.

    The PR head is untrusted data. A symlink (the file itself, or any directory on the way
    to it) that leaves the checkout, and an oversize file, are refused."""
    if path.is_symlink():
        raise Failure(f"{what}: {path.name} is a symlink - refused")
    if not path.is_file():
        raise Failure(f"{what}: {path.name} is missing")
    if not path.resolve().is_relative_to(root.resolve()):
        raise Failure(f"{what}: {path.name} resolves outside the checkout - refused")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise Failure(f"{what}: {path.name} is {size} bytes (limit {MAX_BYTES})")
    return path.read_bytes()


def parse(raw: bytes, what: str) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Failure(f"{what}: not valid JSON ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise Failure(f"{what}: schema {data.get('schema') if isinstance(data, dict) else data!r} is not {SCHEMA}")
    for key, kind in (("repo", str), ("audience", str), ("seq", int), ("generated_at", str),
                      ("parent_sha", str), ("entitled", list)):
        if not isinstance(data.get(key), kind) or isinstance(data.get(key), bool):
            raise Failure(f"{what}: field {key!r} is missing or has the wrong type")
    if data["seq"] < 1:
        raise Failure(f"{what}: seq must be >= 1")
    if not all(isinstance(x, str) and _SERVER.fullmatch(x) for x in data["entitled"]):
        raise Failure(f"{what}: entitled must be a list of server names")
    return data


def parse_time(value: str, what: str) -> datetime:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Failure(f"{what}: generated_at {value!r} is not an ISO 8601 time") from exc
    if when.tzinfo is None:
        raise Failure(f"{what}: generated_at has no timezone")
    return when.astimezone(timezone.utc)


def verify_signature(allowed_signers: Path, payload: bytes, signature: Path) -> None:
    """ssh-keygen -Y verify under the pinned key list, fixed identity and namespace."""
    if not allowed_signers.is_file() or not any(
        line.strip() and not line.lstrip().startswith("#") for line in allowed_signers.read_text().splitlines()
    ):
        raise Failure("verifier unavailable: allowed_signers holds no key (the check cannot pass)")
    try:
        proc = subprocess.run(  # noqa: S603, S607 - fixed argv
            ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", IDENTITY, "-n", NAMESPACE,
             "-s", str(signature)],
            input=payload, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f"verifier unavailable: ssh-keygen could not run ({exc.__class__.__name__})") from exc
    if proc.returncode != 0:
        raise Failure("signature does not verify under a pinned key "
                      f"({proc.stderr.decode('utf-8', 'replace').strip()[:160] or 'ssh-keygen failed'})")


def configured_servers(head: Path) -> set[str]:
    raw = _read(head / MCP_CONFIG, "configuration", head)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Failure(f"configuration: {MCP_CONFIG} is not valid JSON") from exc
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise Failure(f"configuration: {MCP_CONFIG} has no `servers` object")
    return set(servers)


def base_seq(base: Path | None) -> int:
    """The base branch's `seq`; 0 only when the base carries no entitlements file at all (bootstrap)."""
    if base is None or not (base / ENTITLEMENTS).exists():
        return 0
    return parse(_read(base / ENTITLEMENTS, "base", base), "base")["seq"]


def check(head: Path, base: Path | None, repo: str, allowed_signers: Path, changed: list[str],
          now: datetime, max_age: timedelta) -> tuple[int, list[str]]:
    """Run every check; returns (executed, problems). `executed` counts the checks that ran to a verdict."""
    problems: list[str] = []
    executed = 0

    def step(fn) -> object:
        nonlocal executed
        try:
            out = fn()
        except Failure as exc:
            problems.append(str(exc))
            executed += 1
            return None
        executed += 1
        return out

    if not _REPO.fullmatch(repo):
        return 0, [f"--repo {repo!r} is not OWNER/NAME"]

    # 1. the files, their shape, their repository
    raw = step(lambda: _read(head / ENTITLEMENTS, "head", head))
    sig_path = head / SIGNATURE
    sig_ok = step(lambda: _read(sig_path, "head", head)) is not None
    data = None
    if raw is not None:
        data = step(lambda: parse(raw, "head"))
        if data is not None and data["repo"] != repo:
            problems.append(f"head: entitlements are for {data['repo']!r}, not {repo!r}")

    # 2. the signature
    if raw is not None and sig_ok:
        step(lambda: verify_signature(allowed_signers, raw, sig_path))

    # 3. freshness against the base
    if data is not None:
        def fresh() -> None:
            floor = base_seq(base)
            if data["seq"] < floor:
                raise Failure(f"freshness: seq {data['seq']} is lower than the base's {floor} (rollback)")
            age = now - parse_time(data["generated_at"], "head")
            if age > max_age:
                raise Failure(f"freshness: generated {age.days} days ago (limit {max_age.days}); the writer has stalled")
            if age < -timedelta(minutes=10):
                raise Failure("freshness: generated_at is in the future")
        step(fresh)

        # 4. configured <= entitled
        def subset() -> None:
            extra = sorted(configured_servers(head) - set(data["entitled"]))
            if extra:
                raise Failure(f"configured but not entitled: {', '.join(extra)}")
        step(subset)

    # 5. protected paths
    def protected() -> None:
        if CALLER in changed:
            raise Failure(f"protected path: {CALLER} is edited by this PR (operators change it with ruleset bypass)")
        moved = {p for p in changed if p in (ENTITLEMENTS, SIGNATURE)}
        if moved and moved != {ENTITLEMENTS, SIGNATURE}:
            raise Failure("protected path: entitlements.json and its signature must change together")
        if moved and data is not None and data["seq"] <= base_seq(base):
            raise Failure(f"protected path: entitlements changed without a higher seq ({data['seq']} <= {base_seq(base)})")
    step(protected)
    return executed, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--head", type=Path, required=True)
    ap.add_argument("--base", type=Path)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--allowed-signers", type=Path, required=True)
    ap.add_argument("--changed-files", type=Path, required=True)
    ap.add_argument("--now", help="ISO 8601; defaults to the current time")
    ap.add_argument("--max-age-days", type=int, default=45)
    args = ap.parse_args(argv)
    try:
        changed = [ln.strip() for ln in args.changed_files.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as exc:
        print(f"FAIL  changed-files list unreadable: {exc}")
        print("executed=0")
        return 1
    now = parse_time(args.now, "--now") if args.now else datetime.now(timezone.utc)
    base = args.base if args.base and args.base.is_dir() else None
    executed, problems = check(args.head, base, args.repo, args.allowed_signers, changed, now,
                               timedelta(days=args.max_age_days))
    for problem in problems:
        print(f"FAIL  {problem}")
    if not problems and executed:
        print(f"PASS  entitlements for {args.repo} verified")
    print(f"executed={executed}")
    if executed == 0:
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
