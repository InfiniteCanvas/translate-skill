"""Tests for autobuild.AutoBuildScheduler._reap and the reap timeout.

The reaper bounds its blocking wait at autobuild._REAP_TIMEOUT (360s: the
build child's slowest step, epubcheck, self-bounds at 300s, so a child
still running past that plus a margin is hung for another reason). On
TimeoutExpired the child is killed and the stall reported with the exact
"[warn] epub auto-build stalled, killed after Ns (after <reason>)" line;
a child that exited 0 reports "[epub-auto] build ok", a non-zero exit the
failed warn, and a still-running child is left alone by the non-blocking
reap. After every reap the scheduler is idle again (_proc cleared).

Hermetic: no docker, no epubcheck, no real build script. The scheduler's
_proc is pointed directly at plain `sys.executable -c` children (a
long-sleep process for the stall, quick exits for the ok/failed branches),
_reap never touches the log handle unless _spawn set one (it stays None
here), and _REAP_TIMEOUT is temporarily swapped to 0.5s so the kill branch
runs in test time. trigger/_spawn/finalize's spawn half are NOT exercised:
spawning shells out to the real translate.py build-epub against a full
project, which is integration territory covered by the epub suite.

Self-contained PASS/FAIL script (no pytest). autobuild imports only the
stdlib. Run from anywhere:

    python tests/test_autobuild.py
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import autobuild  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def capture(fn, *args, **kwargs):
    """fn(*args, **kwargs) with stdout captured; returns (result, output,
    exc) -- result is None when the call raised."""
    buf = io.StringIO()
    result = None
    exc: Exception | None = None
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except Exception as caught:  # noqa: BLE001 - the caller asserts on it
        exc = caught
    return result, buf.getvalue(), exc


def spawn(code: str) -> subprocess.Popen:
    """A detached child running `python -c CODE` with quiet stdio."""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def make_scheduler(td: str) -> autobuild.AutoBuildScheduler:
    """A scheduler for a temp project dir with NO child, NO pending reason,
    and NO log handle -- _reap's only inputs are _proc and _reason."""
    sched = autobuild.AutoBuildScheduler(Path(td))
    sched._proc = None
    sched._pending = None
    sched._log_fh = None
    sched._reason = ""
    return sched


def kill_quietly(proc: subprocess.Popen) -> None:
    """Best-effort cleanup so a test's child never outlives the script."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()


# ---------------------------------------------------------------------- cases


def case_1_timeout_constant() -> None:
    """The production bound is 360s (epubcheck's own 300s bound + margin);
    pinned so a casual edit cannot silently remove the reaper's teeth."""
    check("1a constant: _REAP_TIMEOUT is 360", autobuild._REAP_TIMEOUT == 360,
          f"value={autobuild._REAP_TIMEOUT}")


def case_2_stall_killed() -> None:
    """A hung child (30s sleep) under a 0.5s reap timeout: finalize()'s
    blocking wait kills it, prints the exact stall warn naming the timeout
    and the reason, and returns without raising; the child is dead and the
    scheduler is idle again (_proc None, nothing left to reap)."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        proc = spawn("import time; time.sleep(30)")
        sched._proc = proc
        sched._reason = "translate Chapter_0001.md"
        orig_timeout = autobuild._REAP_TIMEOUT
        autobuild._REAP_TIMEOUT = 0.5
        try:
            _r, out, exc = capture(sched.finalize)
        finally:
            autobuild._REAP_TIMEOUT = orig_timeout
        check("2a stall: finalize returns without raising (kill, not crash)",
              exc is None, f"exc={exc!r}")
        check("2b stall: exact stalled warn line",
              out == "[warn] epub auto-build stalled, killed after 0.5s "
                     "(after translate Chapter_0001.md) - see "
                     "logs/epub-build.log\n", f"out={out!r}")
        check("2c stall: the child is dead (poll() is not None)",
              proc.poll() is not None, f"poll={proc.poll()!r}")
        check("2d stall: scheduler idle again (_proc cleared)",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")
        # A reap of the cleared scheduler must stay a silent no-op.
        _r, out2, exc2 = capture(sched._reap, wait=True)
        check("2e stall: reaping an idle scheduler is a silent no-op",
              exc2 is None and out2 == "", f"exc={exc2!r} out={out2!r}")


def case_3_reap_branches() -> None:
    """The non-stall reap branches: a finished exit-0 child reports
    '[epub-auto] build ok', a non-zero exit reports the failed warn, and a
    still-running child is left running by the non-blocking reap (poll()
    path) with the scheduler still holding it."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        ok_proc = spawn("pass")
        ok_proc.wait()
        sched._proc = ok_proc
        sched._reason = "finalize"
        _r, out, exc = capture(sched._reap, wait=True)
        check("3a branch: exit 0 -> build ok line, no raise",
              exc is None
              and out == "[epub-auto] build ok (after finalize)\n",
              f"exc={exc!r} out={out!r}")

        bad_proc = spawn("import sys; sys.exit(3)")
        bad_proc.wait()
        sched._proc = bad_proc
        sched._reason = "finalize"
        _r, out2, exc2 = capture(sched._reap)
        check("3b branch: exit 3 -> failed warn with the code",
              exc2 is None
              and out2 == "[warn] epub auto-build failed, exit 3 "
                         "(after finalize) - see logs/epub-build.log\n",
              f"exc={exc2!r} out={out2!r}")

        live = spawn("import time; time.sleep(30)")
        try:
            sched._proc = live
            sched._reason = "poll"
            _r, out3, exc3 = capture(sched.poll)
            check("3c branch: non-blocking reap leaves a live child running",
                  exc3 is None and out3 == ""
                  and sched._proc is live and live.poll() is None,
                  f"exc={exc3!r} out={out3!r} proc={sched._proc!r}")
        finally:
            kill_quietly(live)


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_timeout_constant()
    case_2_stall_killed()
    case_3_reap_branches()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
