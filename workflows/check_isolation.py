#!/usr/bin/env python3
"""Record and verify that the main working tree has not been contaminated.

The verification loop's central promise is that nothing experimental ever
reaches the main working tree: implementation happens in an isolated checkout
and only a verified branch is merged back. An agent that writes to the main
tree breaks that promise silently -- the files look like ordinary edits, they
survive until the merge, and the gate in the checkout keeps passing on a tree
nobody is looking at.

This guard turns that promise into something checkable. Snapshot the main
tree's HEAD and its porcelain status scoped to the source paths, then compare
against that snapshot at a phase boundary.

    python workflows/check_isolation.py --record
    python workflows/check_isolation.py --verify

Scope is deliberately `novel-translator/` and `tests/` only. The orchestrator
legitimately writes PLAN-*.md, AUDIT-*.md and workflows/ in the main tree, so
scanning everything would report those as contamination every phase.

Exit codes: 0 clean, 1 contamination found, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

WATCHED = ("novel-translator", "tests")
DEFAULT_BASELINE = Path.home() / ".nt-isolation-baseline.json"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def snapshot(repo: Path) -> dict:
    """HEAD plus the porcelain status of the watched paths."""
    return {
        "repo": str(repo),
        "head": _git(repo, "rev-parse", "HEAD").strip(),
        "status": sorted(
            line for line in _git(repo, "status", "--porcelain", "--", *WATCHED).splitlines()
            if line.strip()
        ),
    }


def record(repo: Path, path: Path) -> int:
    snap = snapshot(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    print(f"[ok] recorded isolation baseline -> {path}")
    print(f"     HEAD   {snap['head'][:12]}")
    print(f"     source {'clean' if not snap['status'] else str(len(snap['status'])) + ' pre-existing change(s)'}")
    return 0


def verify(repo: Path, path: Path) -> int:
    if not path.is_file():
        print(f"[FAIL] no baseline at {path} - run with --record first")
        return 1
    base = json.loads(path.read_text(encoding="utf-8"))
    now = snapshot(repo)

    if base["head"] != now["head"]:
        print(f"[FAIL] main tree HEAD moved: {base['head'][:12]} -> {now['head'][:12]}")
        print("       the main branch must not advance while a phase is in flight")
        return 1

    added = sorted(set(now["status"]) - set(base["status"]))
    if not added:
        print(f"[ok] main tree uncontaminated (HEAD {now['head'][:12]}, source {len(base['status'])} pre-existing)")
        return 0

    print(f"[FAIL] {len(added)} unexpected change(s) in the main working tree:")
    for line in added:
        print(f"       {line}")
    print("       an agent wrote outside the checkout. Restore them, and verify")
    print("       the checkout branch holds the authoritative version first:")
    print("         git -C <checkout> diff --stat")
    print("         git -C <main> restore <paths>")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    here = Path(__file__).resolve().parent.parent
    parser.add_argument("--repo", type=Path, default=here,
                        help="main working tree (default: this repository)")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                        help=f"baseline file (default: {DEFAULT_BASELINE})")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true", help="snapshot the main tree now")
    mode.add_argument("--verify", action="store_true", help="compare against the snapshot")
    args = parser.parse_args(argv)
    return record(args.repo, args.baseline) if args.record else verify(args.repo, args.baseline)


if __name__ == "__main__":
    sys.exit(main())