"""Tests for autobuild.AutoBuildScheduler._reap and the reap timeout.

The reaper bounds its blocking wait at autobuild._REAP_TIMEOUT (360s: the
build child's slowest step, epubcheck, self-bounds at 300s, so a child
still running past that plus a margin is hung for another reason). On
TimeoutExpired the child AND its process subtree are killed (the tree kill
-- taskkill /T on Windows, killpg on POSIX -- so the build child's docker
grandchild cannot outlive the reaper) and the stall is reported with the
exact "[warn] epub auto-build stalled, killed after Ns (after <reason>)"
line; a child that exited 0 reports "[epub-auto] build ok", a non-zero
exit the failed warn, and a still-running child is left alone by the
non-blocking reap. The blocking wait polls at _FINALIZE_POLL_S granularity
and, once the cumulative wait crosses _FINALIZE_WARN_S, reports exactly
once "[warn] epub auto-build finalize waited Ns for the builder to exit"
while continuing to the existing bound. After every reap the scheduler is
idle again (_proc cleared).

Hermetic: no docker, no epubcheck, no real build script. The scheduler's
_proc is pointed directly at plain `sys.executable -c` children (a
long-sleep process for the stall, quick exits for the ok/failed branches)
and at fake proc objects for the finalize wait loop; _reap never touches
the log handle unless _spawn set one (it stays None here), and
_REAP_TIMEOUT is temporarily swapped to 0.5s so the kill branch runs in
test time. The POSIX children are spawned with start_new_session=True
(mirroring _SPAWN_EXTRA) so the real killpg tree kill cannot take the test
process down with them; the Windows taskkill tree-kill mechanics themselves
are not unit-testable without real process trees and are only covered
indirectly (the child really dies in case 2). trigger/_spawn/finalize's
spawn half are NOT exercised: spawning shells out to the real translate.py
build-epub against a full project, which is integration territory covered
by the epub suite.

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
    """A detached child running `python -c CODE` with quiet stdio, in its
    own session on POSIX (mirroring _SPAWN_EXTRA) so the real killpg tree
    kill can never take the test process down with it."""
    extra = {} if sys.platform == "win32" else {"start_new_session": True}
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **extra,
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


class FakeProc:
    """A Popen stand-in for the finalize wait loop: the first `timeouts`
    wait() calls raise TimeoutExpired, then it exits 0. Records the wait
    timeouts it saw and whether a kill was requested; after a kill it
    reports exit 1 (what a force-killed child would return)."""

    def __init__(self, timeouts: int):
        self.remaining = timeouts
        self.returncode = None
        self.killed = False
        self.timeouts: list[float] = []

    def wait(self, timeout=None):
        if self.killed:
            self.returncode = 1
            return 1
        if self.remaining > 0:
            self.remaining -= 1
            self.timeouts.append(timeout)
            raise subprocess.TimeoutExpired("build-epub", timeout)
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def case_4_finalize_warn() -> None:
    """finalize()'s wait loop reports once when the build outlasts
    _FINALIZE_WARN_S and then keeps waiting: with the warn bound swapped to
    2s (poll 0.1s) and a fake child that times out 25 waits before exiting
    0, exactly one "[warn] epub auto-build finalize waited 2s for the
    builder to exit" line precedes the "[epub-auto] build ok" line, no kill
    happens, and the scheduler ends idle. Pure fake-proc unit: every fake
    wait returns instantly, so nothing really waits."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        fake = FakeProc(timeouts=25)
        sched._proc = fake
        sched._reason = "translate Chapter_0002.md"
        orig_warn, orig_poll = autobuild._FINALIZE_WARN_S, autobuild._FINALIZE_POLL_S
        autobuild._FINALIZE_WARN_S = 2
        autobuild._FINALIZE_POLL_S = 0.1
        try:
            _r, out, exc = capture(sched.finalize)
        finally:
            autobuild._FINALIZE_WARN_S = orig_warn
            autobuild._FINALIZE_POLL_S = orig_poll
        check("4a finalize-warn: returns without raising", exc is None,
              f"exc={exc!r}")
        check("4b finalize-warn: exactly one wait line, naming the seconds",
              out.count("finalize waited") == 1
              and "[warn] epub auto-build finalize waited 2s for the "
                  "builder to exit" in out, f"out={out!r}")
        check("4c finalize-warn: the wait line precedes the build-ok line",
              out.index("finalize waited") < out.index("[epub-auto] build ok"),
              f"out={out!r}")
        check("4d finalize-warn: build ok reported after the fake child exits",
              out.endswith("[epub-auto] build ok (after translate "
                           "Chapter_0002.md)\n"), f"out={out!r}")
        check("4e finalize-warn: every wait bounded at the poll interval",
              fake.timeouts and all(t == 0.1 for t in fake.timeouts),
              f"timeouts={fake.timeouts!r}")
        check("4f finalize-warn: the child was never killed",
              not fake.killed and fake.returncode == 0,
              f"killed={fake.killed} rc={fake.returncode!r}")
        check("4g finalize-warn: scheduler idle again",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")


def case_5_finalize_stall_kill() -> None:
    """The finalize loop's kill branch, hermetic: a fake child that never
    exits under a 0.3s swapped _REAP_TIMEOUT gets the TREE kill (autobuild.
    _kill_tree swapped for a recorder -- the real taskkill/killpg paths
    cannot run against a fake pid, and the Windows tree-kill mechanics are
    only covered indirectly by case 2's real child), the exact stall warn
    prints, and the scheduler ends idle."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        fake = FakeProc(timeouts=10 ** 9)  # never exits on its own
        sched._proc = fake
        sched._reason = "translate Chapter_0003.md"
        killed: list[object] = []
        orig_kill = autobuild._kill_tree
        autobuild._kill_tree = killed.append
        orig_timeout, orig_poll = autobuild._REAP_TIMEOUT, autobuild._FINALIZE_POLL_S
        autobuild._REAP_TIMEOUT = 0.3
        autobuild._FINALIZE_POLL_S = 0.1
        try:
            _r, out, exc = capture(sched.finalize)
        finally:
            autobuild._kill_tree = orig_kill
            autobuild._REAP_TIMEOUT = orig_timeout
            autobuild._FINALIZE_POLL_S = orig_poll
        check("5a stall-kill: returns without raising", exc is None,
              f"exc={exc!r}")
        check("5b stall-kill: the tree kill ran exactly once on the child",
              killed == [fake], f"killed={killed!r}")
        check("5c stall-kill: exact stalled warn",
              out == "[warn] epub auto-build stalled, killed after 0.3s "
                     "(after translate Chapter_0003.md) - see "
                     "logs/epub-build.log\n", f"out={out!r}")
        check("5d stall-kill: no finalize-wait line before the stall",
              "finalize waited" not in out, f"out={out!r}")
        check("5e stall-kill: scheduler idle again",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_timeout_constant()
    case_2_stall_killed()
    case_3_reap_branches()
    case_4_finalize_warn()
    case_5_finalize_stall_kill()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
