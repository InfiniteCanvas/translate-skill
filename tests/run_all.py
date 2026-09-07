"""Run every tests/test_*.py as a subprocess and print a PASS/FAIL summary.

Each test script is a self-contained PASS/FAIL program (module docstring up
top, exit 0 = all its checks passed, 1 = any failed); this runner executes
each in a fresh interpreter via sys.executable, in sorted name order. The
test scripts are CWD-independent (they locate lib/ relative to __file__),
so the working directory only affects where a "." --project would resolve
-- running from tests/ keeps that deterministic. Output is captured and
printed only for failing scripts so the summary stays readable.

Stdlib only, no pytest. The inline dependencies below are the union the
test scripts themselves import (pyyaml, requests, ebooklib, pillow): uv
materializes them for this interpreter, which the spawned children inherit.
Run from anywhere:

    uv run tests/run_all.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    scripts = sorted(
        p for p in TESTS_DIR.glob("test_*.py")
        if p.name != Path(__file__).name  # never run itself
    )
    if not scripts:
        print(f"no test scripts found in {TESTS_DIR}")
        return 1

    results: list[tuple[str, int]] = []
    for script in scripts:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(TESTS_DIR),
        )
        results.append((script.name, proc.returncode))
        print(f"{'PASS' if proc.returncode == 0 else 'FAIL'}  {script.name}")
        if proc.returncode != 0:
            output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
            if output:
                print(f"----- {script.name} output -----")
                print(output)

    passed = sum(1 for _name, code in results if code == 0)
    failed = len(results) - passed
    print(f"\n{passed} passed, {failed} failed ({len(results)} script(s))")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
