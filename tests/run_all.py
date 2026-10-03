"""Run every tests/test_*.py as a subprocess and print a PASS/FAIL summary.

Each test script is a self-contained PASS/FAIL program (module docstring up
top, exit 0 = all its checks passed, 1 = any failed); this runner executes
each in a fresh interpreter via sys.executable, in sorted name order. The
test scripts are CWD-independent (they locate lib/ relative to __file__),
so the working directory only affects where a "." --project would resolve
-- running from tests/ keeps that deterministic. Output is captured and
printed only for failing scripts so the summary stays readable.

Exit code alone cannot tell a real pass from a script whose main() never
ran, so each script's own summary line ("N passed, M failed") is parsed
and a script reporting zero checks is failed here. The check count is
also totaled, so the summary reports assertions rather than scripts.

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

import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# The house summary line every test script prints at the end of main().
SUMMARY_RE = re.compile(r"^\s*(\d+)\s+passed,\s+(\d+)\s+failed", re.MULTILINE)


def _checks(output: str) -> int | None:
    """Checks the script reported, or None when it printed no summary line
    (a crash before main()'s tail, or a script that does not follow the
    house shape). None is treated as a failure by the caller.

    The LAST summary line wins: a script that echoes a captured child's
    output can print the pattern more than once, and summing every match
    would report more checks than the script ran."""
    matches = SUMMARY_RE.findall(output or "")
    if not matches:
        return None
    return int(matches[-1][0])


def main() -> int:
    scripts = sorted(
        p for p in TESTS_DIR.glob("test_*.py")
        if p.name != Path(__file__).name  # never run itself
    )
    if not scripts:
        print(f"no test scripts found in {TESTS_DIR}")
        return 1

    results: list[tuple[str, int, int]] = []
    for script in scripts:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(TESTS_DIR),
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        checks = _checks(output)
        # Exit 0 with no reported checks means main() never ran: PASSing on
        # the exit code alone would let a gutted script report green.
        code = proc.returncode if checks else 1
        results.append((script.name, code, checks or 0))
        print(f"{'PASS' if code == 0 else 'FAIL'}  {script.name}")
        if code != 0:
            if checks is None and proc.returncode == 0:
                print(f"----- {script.name} reported no check summary -----")
            if output.strip():
                print(f"----- {script.name} output -----")
                print(output.rstrip())

    passed = sum(1 for _name, code, _checks_run in results if code == 0)
    failed = len(results) - passed
    total = sum(checks_run for _name, _code, checks_run in results)
    print(f"\n{passed} passed, {failed} failed "
          f"({len(results)} script(s), {total} check(s))")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
