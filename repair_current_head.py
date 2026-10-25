#!/usr/bin/env python3
"""Rerun one cancelled/timed-out entitlement check on an open PR's current head.

This runs inside the public plugin, on its default branch, with only that repo's
Actions(write) GITHUB_TOKEN. A missing run cannot be created by rerun; the parent
fleet reconcile reports it as a fail-closed owner issue instead.
"""
from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 - fixed gh commands, no shell
import sys
from typing import Callable

WORKFLOW = "entitlement-contract.yml"
PATH = ".github/workflows/entitlement-contract.yml"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
REPAIRABLE = {"cancelled", "timed_out"}


class Refused(Exception):
    pass


def gh(argv: list[str]) -> dict | list:
    proc = subprocess.run(["gh", *argv], capture_output=True, text=True, check=False, timeout=60)  # noqa: S603
    if proc.returncode:
        raise Refused(f"gh {' '.join(argv[:3])} failed: {(proc.stderr or proc.stdout).strip()[:200]}")
    if not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Refused("GitHub returned invalid JSON") from exc


def current_head(repo: str, number: int, api: Callable[[list[str]], dict | list]) -> str:
    data = api(["pr", "view", str(number), "--repo", repo, "--json", "headRefOid,state"])
    if not isinstance(data, dict) or data.get("state") != "OPEN":
        raise Refused("PR closed or current head unreadable")
    head = data.get("headRefOid")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise Refused("PR current head is not a full SHA")
    return head


def repair(repo: str, api: Callable[[list[str]], dict | list] = gh) -> tuple[int, list[str]]:
    if not REPO.fullmatch(repo):
        raise Refused("GITHUB_REPOSITORY is not owner/name")
    prs = api(["pr", "list", "--repo", repo, "--state", "open", "--limit", "1000",
               "--json", "number,headRefOid"])
    if not isinstance(prs, list) or len(prs) >= 1000:
        raise Refused("open PR inventory unreadable or capped")
    repaired = 0
    notes = []
    for pr in prs:
        number, head = pr.get("number"), pr.get("headRefOid")
        if not isinstance(number, int) or not isinstance(head, str) or not SHA.fullmatch(head):
            raise Refused("open PR has invalid number or head")
        result = api(["api", "-X", "GET", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs",
                      "-f", f"head_sha={head}", "-f", "event=pull_request_target", "-f", "per_page=100"])
        if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
            raise Refused(f"run inventory unreadable for PR {number}")
        if not isinstance(result.get("total_count"), int) or result["total_count"] > 100:
            raise Refused(f"run inventory capped for PR {number}")
        runs = [run for run in result["workflow_runs"]
                if run.get("head_sha") == head and run.get("event") == "pull_request_target"
                and run.get("path") == PATH]
        if not runs:
            notes.append(f"PR {number}: no current-head run; parent reconcile must alert the owner")
            continue
        if any(run.get("run_attempt", 0) > 1 for run in runs):
            notes.append(f"PR {number}: this head already had a rerun")
            continue
        latest = max(runs, key=lambda run: (run.get("created_at") or "", run.get("id", 0)))
        if latest.get("conclusion") not in REPAIRABLE:
            notes.append(f"PR {number}: current run {latest.get('conclusion') or latest.get('status')}; no repair")
            continue
        run_id = latest.get("id")
        if not isinstance(run_id, int) or latest.get("run_attempt") != 1:
            raise Refused(f"PR {number}: rerun evidence is invalid")
        if current_head(repo, number, api) != head:
            notes.append(f"PR {number}: head moved before rerun; refused")
            continue
        fresh = api(["api", f"repos/{repo}/actions/runs/{run_id}"])
        if (not isinstance(fresh, dict) or fresh.get("head_sha") != head
                or fresh.get("path") != PATH or fresh.get("event") != "pull_request_target"
                or fresh.get("conclusion") not in REPAIRABLE or fresh.get("run_attempt") != 1):
            notes.append(f"PR {number}: run changed before rerun; refused")
            continue
        api(["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun"])
        repaired += 1
        notes.append(f"PR {number}: reran cancelled/timed-out run {run_id} once for {head}")
    return repaired, notes


def main() -> int:
    try:
        repaired, notes = repair(os.environ.get("GITHUB_REPOSITORY", ""))
    except Refused as exc:
        print(f"repair-current-head: FAIL {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(note)
    print(f"repair-current-head: reran {repaired} current-head run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
