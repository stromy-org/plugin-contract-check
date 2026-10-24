#!/usr/bin/env python3
"""Refuse a pull_request_target verdict for a PR revision other than the live one."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _sha(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()


def validate(event: dict, current: dict, head_sha: str, base_sha: str) -> list[str]:
    problems: list[str] = []
    event_pr = event.get("pull_request") or {}
    event_repo = (event.get("repository") or {}).get("full_name")
    current_repo = (current.get("base") or {}).get("repo") or {}
    for label, actual, expected in (
        ("PR number", current.get("number"), event.get("number")),
        ("repository", current_repo.get("full_name"), event_repo),
        ("head SHA", (current.get("head") or {}).get("sha"), (event_pr.get("head") or {}).get("sha")),
        ("head repository", ((current.get("head") or {}).get("repo") or {}).get("full_name"),
         ((event_pr.get("head") or {}).get("repo") or {}).get("full_name")),
        ("base SHA", (current.get("base") or {}).get("sha"), (event_pr.get("base") or {}).get("sha")),
        ("checked-out head", head_sha, (event_pr.get("head") or {}).get("sha")),
        ("checked-out base", base_sha, (event_pr.get("base") or {}).get("sha")),
    ):
        if not expected or actual != expected:
            problems.append(f"{label} differs from the event or checkout")
    if current.get("state") != "open":
        problems.append("PR is no longer open")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        event = json.loads(args.event.read_text())
        current = json.loads(args.current.read_text())
        problems = validate(event, current, _sha(args.head), _sha(args.base))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"FAIL PR revision unavailable: {exc.__class__.__name__}")
        return 1
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print("PASS live PR revision matches the event and checked-out trees")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
