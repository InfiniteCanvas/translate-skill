"""Tests for autobuild.AutoBuildScheduler._reap and the reap timeout.

The reaper bounds its blocking wait at autobuild._REAP_TIMEOUT (360s: the
build child's slowest step, epubcheck, self-bounds at 300s, so a child
still running past that plus a margin is hung for another reason). On
TimeoutExpired the child AND its process subtree are killed (the tree kill
-- taskkill /T on Windows, killpg on POSIX -- so the build child's docker
CLI grandchild cannot outlive the reaper; the daemon-side epubcheck
container is NOT our child -- docker --rm reaps it when it exits) and the
stall is reported with the
exact "[warn] epub auto-build stalled, killed after Ns (after <reason>)"
line; a child that exited 0 reports "[epub-auto] build ok", a non-zero
exit the failed warn, and a still-running child is left alone by the
non-blocking reap. The blocking wait polls at _FINALIZE_POLL_S granularity
and, once the cumulative wait crosses _FINALIZE_WARN_S, reports exactly
once "[warn] epub auto-build finalize waited Ns for the builder to exit"
while continuing to the existing bound. After every reap the scheduler is
idle again (_proc cleared).

The kill path itself is covered hermetically, with the kill tools swapped
for fakes (no real taskkill/killpg invoked directly): a kill tool that
fails (taskkill missing, killpg refusing) warns instead of raising --
abort() runs inside the pipeline's KeyboardInterrupt handler and must
never turn the clean Ctrl-C (exit 130) into a traceback; a builder that
survives the kill (the post-kill wait keeps timing out) prints the
survived warn and still reports the interrupt/stall; on a confirmed death
the killed child's pid-named export/<name>.epub.<pid>.tmp is swept (a tmp
with any other pid suffix is never touched) while a survivor's tmp is
left alone; and the round-2 subprocess-hardening kwargs are pinned
(taskkill with capture_output=True, stdin=DEVNULL, timeout=60; POSIX
killpg SIGKILL to the child's own group).

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
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
        sched._reason = "translate CHAPTER_0001.md"
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
                     "(after translate CHAPTER_0001.md) - see "
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
    reports exit 1 (what a force-killed child would return). Carries a fake
    pid for _kill_and_reap's tmp-sweep glob."""

    def __init__(self, timeouts: int):
        self.remaining = timeouts
        self.returncode = None
        self.killed = False
        self.timeouts: list[float] = []
        self.pid = 424242

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
        sched._reason = "translate CHAPTER_0002.md"
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
                           "CHAPTER_0002.md)\n"), f"out={out!r}")
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
        sched._reason = "translate CHAPTER_0003.md"
        killed: list[object] = []

        def fake_tree_kill(proc):
            killed.append(proc)
            proc.killed = True  # the tree kill "worked": the child reports dead

        orig_kill = autobuild._kill_tree
        autobuild._kill_tree = fake_tree_kill
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
                     "(after translate CHAPTER_0003.md) - see "
                     "logs/epub-build.log\n", f"out={out!r}")
        check("5d stall-kill: no finalize-wait line before the stall",
              "finalize waited" not in out, f"out={out!r}")
        check("5e stall-kill: scheduler idle again",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")


def case_6_kill_tool_failure() -> None:
    """abort() with a failing kill tool, hermetic: the tool raising
    (taskkill missing from PATH on win32, killpg refusing on POSIX) prints
    the exact "failed to kill builder" warn AND the "[warn] epub auto-build
    interrupted" line, in that order, and returns without raising -- abort
    runs inside the pipeline's KeyboardInterrupt handler, and a raise here
    would replace the clean Ctrl-C (exit 130) with a traceback.
    _kill_tree's module-level tool binding (subprocess on win32, os on
    POSIX) is swapped for a raiser per the file's attribute-swap
    convention; the child is a fake that reports dead so the post-kill
    wait never blocks."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        fake = FakeProc(timeouts=0)
        sched._proc = fake
        sched._reason = "translate CHAPTER_0004.md"
        if sys.platform == "win32":
            orig_binding = autobuild.subprocess

            def raise_run(*args, **kwargs):
                raise FileNotFoundError(2, "taskkill not on PATH")

            autobuild.subprocess = SimpleNamespace(
                run=raise_run,
                DEVNULL=orig_binding.DEVNULL,
                TimeoutExpired=orig_binding.TimeoutExpired,
            )
            tool = "taskkill"
        else:
            orig_binding = autobuild.os

            def raise_getpgid(pid):
                raise PermissionError(1, "Operation not permitted")

            autobuild.os = SimpleNamespace(getpgid=raise_getpgid)
            tool = "killpg"
        try:
            _r, out, exc = capture(sched.abort)
        finally:
            if sys.platform == "win32":
                autobuild.subprocess = orig_binding
            else:
                autobuild.os = orig_binding
        warn = (f"[warn] epub auto-build: failed to kill builder "
                f"({tool}: ")
        check("6a kill-tool: abort returns without raising", exc is None,
              f"exc={exc!r}")
        check(f"6b kill-tool: exact kill-failed warn ({tool})",
              warn in out, f"out={out!r}")
        check("6c kill-tool: the interrupted warn still prints",
              "[warn] epub auto-build interrupted" in out, f"out={out!r}")
        check("6d kill-tool: the kill-failed warn precedes the interrupt",
              0 <= out.index(warn) < out.index("[warn] epub auto-build "
                                               "interrupted"),
              f"out={out!r}")
        check("6e kill-tool: scheduler idle again",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")


def case_7_survivor_abort() -> None:
    """A builder that survives the kill (the post-kill wait keeps raising
    TimeoutExpired): abort() prints the exact survived warn and still
    prints the interrupted warn, clears _proc, and returns without
    raising -- a kill that did not stick must be reported, not read as
    done. _kill_tree is swapped for a recorder (no real taskkill/killpg
    invoked) and the fake child never dies."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        fake = FakeProc(timeouts=10 ** 9)  # never exits, even after a kill
        sched._proc = fake
        sched._reason = "translate CHAPTER_0005.md"
        killed: list[object] = []
        orig_kill = autobuild._kill_tree
        autobuild._kill_tree = killed.append
        try:
            _r, out, exc = capture(sched.abort)
        finally:
            autobuild._kill_tree = orig_kill
        check("7a survivor: abort returns without raising", exc is None,
              f"exc={exc!r}")
        check("7b survivor: the tree kill ran once on the child",
              killed == [fake], f"killed={killed!r}")
        check("7c survivor: exact survived warn",
              "[warn] epub auto-build builder survived the kill - it may "
              "still be running" in out, f"out={out!r}")
        check("7d survivor: the interrupted warn still prints",
              "[warn] epub auto-build interrupted" in out, f"out={out!r}")
        check("7e survivor: _proc cleared despite the survivor",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")
        check("7f survivor: no tmp sweep on the survivor path",
              "stale epub temp" not in out, f"out={out!r}")


def case_8_stall_tmp_sweep() -> None:
    """The stall arm's pid-scoped tmp sweep: with a planted
    export/<name>.epub.<childpid>.tmp (the tmp a hard kill orphans, named
    after the build child's pid per epub.py), the confirmed-dead kill
    removes exactly that file, prints the exact removed-warn, and leaves a
    tmp with a DIFFERENT pid suffix alone (it belongs to another process).
    The stall warn itself is unchanged; the kill recorder marks the fake
    dead so the sweep path runs."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        fake = FakeProc(timeouts=10 ** 9)
        sched._proc = fake
        sched._reason = "translate CHAPTER_0006.md"
        export = Path(td) / "export"
        export.mkdir()
        own = export / f"atomic.epub.{fake.pid}.tmp"
        other = export / "atomic.epub.987654.tmp"
        own.write_bytes(b"x")
        other.write_bytes(b"x")
        killed: list[object] = []

        def fake_tree_kill(proc):
            killed.append(proc)
            proc.killed = True  # the tree kill "worked": the child reports dead

        orig_kill = autobuild._kill_tree
        autobuild._kill_tree = fake_tree_kill
        orig_timeout, orig_poll = autobuild._REAP_TIMEOUT, autobuild._FINALIZE_POLL_S
        autobuild._REAP_TIMEOUT = 0.3
        autobuild._FINALIZE_POLL_S = 0.1
        try:
            _r, out, exc = capture(sched.finalize)
        finally:
            autobuild._kill_tree = orig_kill
            autobuild._REAP_TIMEOUT = orig_timeout
            autobuild._FINALIZE_POLL_S = orig_poll
        check("8a stall-sweep: finalize returns without raising", exc is None,
              f"exc={exc!r}")
        check("8b stall-sweep: the killed child's own-pid tmp was removed",
              not own.exists(), f"own exists={own.exists()}")
        check("8c stall-sweep: a different-pid tmp is never touched",
              other.exists(), f"other exists={other.exists()}")
        check("8d stall-sweep: exact output (removed-warn then stall warn)",
              out == "[warn] removed 1 stale epub temp file(s) left by the "
                     "killed build\n"
                     "[warn] epub auto-build stalled, killed after 0.3s "
                     "(after translate CHAPTER_0006.md) - see "
                     "logs/epub-build.log\n", f"out={out!r}")
        check("8e stall-sweep: scheduler idle again",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")


def case_9_survivor_keeps_tmp() -> None:
    """The wait-times-out path must NOT sweep: with the same planted
    own-pid tmp as case 8 but a builder that survives the kill, the tmp
    stays (the builder may still be writing through it) and no removed-warn
    prints; the survived and stall warns still do. Same fakes as case 8,
    minus the kill recorder marking the child dead."""
    with tempfile.TemporaryDirectory() as td:
        sched = make_scheduler(td)
        fake = FakeProc(timeouts=10 ** 9)  # never exits, even after a kill
        sched._proc = fake
        sched._reason = "translate CHAPTER_0007.md"
        export = Path(td) / "export"
        export.mkdir()
        own = export / f"atomic.epub.{fake.pid}.tmp"
        own.write_bytes(b"x")
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
        check("9a no-sweep: finalize returns without raising", exc is None,
              f"exc={exc!r}")
        check("9b no-sweep: the planted own-pid tmp survives",
              own.exists(), f"own exists={own.exists()}")
        check("9c no-sweep: no removed-warn on the survivor path",
              "stale epub temp" not in out, f"out={out!r}")
        check("9d no-sweep: exact output (survived warn then stall warn)",
              out == "[warn] epub auto-build builder survived the kill - "
                     "it may still be running\n"
                     "[warn] epub auto-build stalled, killed after 0.3s "
                     "(after translate CHAPTER_0007.md) - see "
                     "logs/epub-build.log\n", f"out={out!r}")
        check("9e no-sweep: scheduler idle again",
              sched._proc is None and sched._reason == "",
              f"proc={sched._proc!r} reason={sched._reason!r}")


def case_10_kill_tool_kwargs() -> None:
    """The kill-tool invocation is pinned (round-2 subprocess hardening the
    fakes used to substitute silently): the win32 taskkill runs with
    capture_output=True, stdin=DEVNULL and timeout=60; the POSIX tree kill
    is SIGKILL to the child's own process group. The module's subprocess/os
    bindings are swapped for recorders, per the file's attribute-swap
    convention."""
    fake = FakeProc(timeouts=0)
    if sys.platform == "win32":
        calls: list[tuple] = []

        def record_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        orig_binding = autobuild.subprocess
        autobuild.subprocess = SimpleNamespace(
            run=record_run,
            DEVNULL=orig_binding.DEVNULL,
            TimeoutExpired=orig_binding.TimeoutExpired,
        )
        try:
            _r, _out, exc = capture(autobuild._kill_tree, fake)
        finally:
            autobuild.subprocess = orig_binding
        kwargs = calls[0][1] if calls else {}
        check("10a kill-kwargs: _kill_tree returns without raising",
              exc is None, f"exc={exc!r}")
        check("10b kill-kwargs: taskkill called once on the child's pid",
              len(calls) == 1
              and calls[0][0] == ["taskkill", "/PID", str(fake.pid), "/T", "/F"],
              f"calls={calls!r}")
        check("10c kill-kwargs: capture_output=True",
              kwargs.get("capture_output") is True, f"kwargs={kwargs!r}")
        check("10d kill-kwargs: stdin=DEVNULL",
              kwargs.get("stdin") is subprocess.DEVNULL, f"kwargs={kwargs!r}")
        check("10e kill-kwargs: timeout=60",
              kwargs.get("timeout") == 60, f"kwargs={kwargs!r}")
    else:
        sigs: list[tuple[int, object]] = []

        def record_killpg(pgid, sig):
            sigs.append((pgid, sig))

        orig_os = autobuild.os
        autobuild.os = SimpleNamespace(
            getpgid=lambda pid: pid + 1000,  # the child leads its own group
            killpg=record_killpg,
        )
        try:
            _r, _out, exc = capture(autobuild._kill_tree, fake)
        finally:
            autobuild.os = orig_os
        check("10a kill-kwargs: _kill_tree returns without raising",
              exc is None, f"exc={exc!r}")
        check("10b kill-kwargs: killpg SIGKILLs the child's own group",
              sigs == [(fake.pid + 1000, signal.SIGKILL)], f"sigs={sigs!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_timeout_constant()
    case_2_stall_killed()
    case_3_reap_branches()
    case_4_finalize_warn()
    case_5_finalize_stall_kill()
    case_6_kill_tool_failure()
    case_7_survivor_abort()
    case_8_stall_tmp_sweep()
    case_9_survivor_keeps_tmp()
    case_10_kill_tool_kwargs()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
