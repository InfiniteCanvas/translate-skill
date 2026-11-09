"""Run every tests/test_*.py as a subprocess and print a PASS/FAIL summary.

Each test script is a self-contained PASS/FAIL program (exit 0 = all its
checks passed, 1 = any failed); this runner executes each in a fresh
interpreter via sys.executable, in sorted name order. The runner reads no
docstring, but an integration suite carries one -- stating what the suite
covers and which system behaviour it enforces -- while a unit suite carries
none (see AGENTS.md, "Comments: the code documents itself"). The
test scripts are CWD-independent (they locate lib/ relative to __file__),
so the working directory only affects where a "." --project would resolve
-- running from tests/ keeps that deterministic. Output is captured and
printed only for failing scripts so the summary stays readable.

Children inherit this process's stdin, so a script run under a pipe, a
redirect, or a CI job sees stdin.isatty() == False. A suite that must not
depend on that pins it itself (see test_migrate.patched_confirm); do NOT
"fix" such a suite by forcing stdin here -- that would hard-code one
environment's answer into the runner and hide the very dependency the
test is meant to surface.

Exit code alone cannot tell a real pass from a script whose main() never
ran, so each script's own summary line ("N passed, M failed") is parsed
and a script reporting zero checks is failed here. The check count is
also totaled, so the summary reports assertions rather than scripts.

Stdlib only, no pytest. The inline dependencies below are the union the
test scripts themselves import (pyyaml, requests, ebooklib, pillow, openai,
anthropic, httpx2): uv materializes them for this interpreter, which the
spawned children inherit.
Run from anywhere:

    uv run tests/run_all.py

The run is hermetic against the machine's own config.local.json. Layer 1 of
the overlay chain is looked up by file existence under the skill root, so a
suite whose fixture omits something the maintainer's overlay happens to
supply loads on their machine and fails on everyone else's -- a gate that
reports green for one machine only. Hiding the file for the duration of the
run (restored in a finally) is what makes that impossible: a suite that
depends on ambient layer 1 now fails here too, and a suite that does not
loses nothing, because the suites that exercise the overlay compose it from
a fake skill root inside a sandbox instead. The rename is atomic and
same-directory; a hard kill (not Ctrl-C, which the finally catches) can strand
the overlay at the .runall-hidden name beside it, and the next run recovers it
on entry rather than hiding nothing and leaving the maintainer with no layer 1.
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0", "openai>=1.60", "anthropic>=1.0", "httpx2>=2.0"]
# ///

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = TESTS_DIR.parent / "novel-translator"
LOCAL_CONFIG = SKILL_ROOT / "config.local.json"
HIDDEN_NAME = LOCAL_CONFIG.name + ".runall-hidden"

SUMMARY_RE = re.compile(r"^\s*(\d+)\s+passed,\s+(\d+)\s+failed", re.MULTILINE)


@contextlib.contextmanager
def ambient_overlay_hidden():
    """Move the machine's config.local.json aside for the run, always putting
    it back; a no-op when the machine has none.

    A hard kill (not Ctrl-C, which the finally catches) can strand the overlay
    at the .runall-hidden name, and the next run would then find nothing at
    LOCAL_CONFIG and skip the restore entirely -- leaving the maintainer with no
    layer 1 at all and every command failing with "resolves no base_url". A
    stranded file is therefore recovered on entry, before anything else looks
    for it."""
    hidden = LOCAL_CONFIG.with_name(HIDDEN_NAME)
    if not LOCAL_CONFIG.is_file() and hidden.is_file():
        os.replace(hidden, LOCAL_CONFIG)
        print(f"note: recovered {HIDDEN_NAME} from an interrupted run")
    if not LOCAL_CONFIG.is_file():
        yield
        return
    os.replace(LOCAL_CONFIG, hidden)
    print(f"note: {LOCAL_CONFIG.name} hidden for this run; restored on exit")
    try:
        yield
    finally:
        os.replace(hidden, LOCAL_CONFIG)


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
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    scripts = sorted(
        p for p in TESTS_DIR.glob("test_*.py")
        if p.name != Path(__file__).name
    )
    if not scripts:
        print(f"no test scripts found in {TESTS_DIR}")
        return 1

    results: list[tuple[str, int, int]] = []
    with ambient_overlay_hidden():
        for script in scripts:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                cwd=str(TESTS_DIR),
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            checks = _checks(output)
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
