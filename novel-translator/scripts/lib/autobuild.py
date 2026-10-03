"""Background epub rebuilds after each translated chapter.

The pipeline triggers a build subprocess after every chapter that reaches
status "translated". Only one build runs at a time (epub.build swaps the
epub into its single export path atomically, but builds are still serialized
so concurrent rebuilds never overlap); triggers arriving while a build runs
just set a pending flag. finalize() waits out the running build and, when
anything is pending, runs one final synchronous build so the finished epub
always includes every chapter.

abort() (the pipeline's Ctrl-C path) and finalize()'s stall arm kill the
running builder's process tree; the kill is best-effort and never raises
-- a builder that survives it is warned about, and on a confirmed death
the child's pid-named epub tmp (a hard kill skips epub.py's finally
cleanup) is swept. The tree kill reaches the docker CLI and its children
only: the daemon-side epubcheck container is not our child and may run to
completion (docker --rm reaps it when it exits).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import IO

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "translate.py"

# Bound for the reaper's blocking wait. The build child's slowest step,
# epubcheck, bounds itself at 300s (epub.run_epubcheck) and every other
# step is local file I/O, so a child still running past that plus a margin
# is hung for another reason and gets killed.
_REAP_TIMEOUT = 360

# finalize()'s blocking wait announces itself once after 30s so a long (but
# legitimate) build does not read as a hang; the loop polls at 1s granularity
# and keeps waiting to _REAP_TIMEOUT before killing.
_FINALIZE_WARN_S = 30
_FINALIZE_POLL_S = 1.0

if sys.platform == "win32":
    # Own process group for the build child: taskkill /T walks the tree by
    # pid either way, but the group keeps the child addressable as a unit.
    _SPAWN_EXTRA = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    # Own session/process group: the tree kill is os.killpg on the child's
    # group, which must therefore never be our own group.
    _SPAWN_EXTRA = {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill proc and its whole process subtree. A plain proc.kill() would
    orphan the build child's docker CLI grandchild and skip epub.py's
    finally cleanup; taskkill /T (Windows) and killpg (POSIX) take the whole
    tree down. The tree is the docker CLI and its children only -- the
    daemon-side epubcheck container is not our child and may run to
    completion (docker --rm reaps it when it exits). Never raises: a kill
    tool that itself fails (taskkill missing, its own timeout, a killpg
    error) is warned about instead."""
    if sys.platform == "win32":
        # Guarded so a broken/missing taskkill (or its own timeout) cannot
        # blow up abort()'s KeyboardInterrupt handler with a traceback.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                stdin=subprocess.DEVNULL, timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 - never raise from a kill
            print(f"[warn] epub auto-build: failed to kill builder "
                  f"(taskkill: {exc}) - it may still be running")
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            try:
                proc.kill()  # the pid vanished between getpgid and the signal
            except ProcessLookupError:
                pass
        except OSError as exc:
            print(f"[warn] epub auto-build: failed to kill builder "
                  f"(killpg: {exc}) - it may still be running")


class AutoBuildScheduler:
    """Serialize background `build-epub` subprocesses for one project."""

    def __init__(self, project_dir: Path):
        self._project_dir = Path(project_dir)
        self._pending: str | None = None   # reason of the latest unspawned build
        self._proc: subprocess.Popen | None = None
        self._log_fh: IO[str] | None = None
        self._reason: str = ""             # reason of the RUNNING build

    def trigger(self, reason: str) -> None:
        """Request a build; spawned at the next poll()/finalize()."""
        self._pending = reason

    def poll(self) -> None:
        """Non-blocking: reap a finished child, then spawn if pending+idle."""
        self._reap()
        if self._pending is not None and self._proc is None:
            self._spawn(self._pending)
            self._pending = None

    def finalize(self) -> None:
        """Batch end: wait out the running build; if a build is still pending
        (skipped earlier because one was running), run it synchronously."""
        self._reap(wait=True)
        if self._pending is not None:
            self._spawn(self._pending)
            self._pending = None
            self._reap(wait=True)

    def abort(self) -> None:
        """Interrupt path: kill the running child and its subtree; never
        spawn another, never raise (runs inside the pipeline's
        KeyboardInterrupt handler -- a raise here would replace the clean
        Ctrl-C with a traceback)."""
        self._pending = None
        if self._proc is not None:
            self._kill_and_reap()
            self._close_log()
            self._proc = None
            self._reason = ""
            print("[warn] epub auto-build interrupted")

    # -- internals ---------------------------------------------------------

    def _spawn(self, reason: str) -> None:
        log_dir = self._project_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = (log_dir / "epub-build.log").open("a", encoding="utf-8", errors="replace")
        try:
            stamp = datetime.now().isoformat(timespec="seconds")
            fh.write(f"\n=== epub build after {reason} | {stamp} ===\n")
            fh.flush()
            self._proc = subprocess.Popen(
                [sys.executable, str(_SCRIPT_PATH), "build-epub",
                 "--project", str(self._project_dir)],
                stdout=fh, stderr=subprocess.STDOUT,
                **_SPAWN_EXTRA,
            )
        except BaseException:
            fh.close()
            raise
        self._log_fh = fh
        self._reason = reason

    def _reap(self, wait: bool = False) -> None:
        if self._proc is None:
            return
        stalled = False
        if wait:
            # Poll instead of one blocking wait: after _FINALIZE_WARN_S of
            # silence say so (once), keep waiting to _REAP_TIMEOUT either way.
            waited = 0.0
            warned = False
            while True:
                try:
                    self._proc.wait(timeout=_FINALIZE_POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    waited += _FINALIZE_POLL_S
                    if waited >= _REAP_TIMEOUT:
                        self._kill_and_reap()
                        stalled = True
                        break
                    if not warned and waited >= _FINALIZE_WARN_S:
                        warned = True
                        print(f"[warn] epub auto-build finalize waited "
                              f"{int(waited)}s for the builder to exit")
        elif self._proc.poll() is None:
            return  # still running
        code = self._proc.returncode
        self._close_log()
        if stalled:
            print(
                f"[warn] epub auto-build stalled, killed after {_REAP_TIMEOUT}s "
                f"(after {self._reason}) - see logs/epub-build.log"
            )
        elif code == 0:
            print(f"[epub-auto] build ok (after {self._reason})")
        else:
            print(f"[warn] epub auto-build failed, exit {code} (after {self._reason}) - see logs/epub-build.log")
        self._proc = None
        self._reason = ""

    def _kill_and_reap(self) -> bool:
        """Tree-kill the running child and wait out its death; True only on
        a confirmed death. A kill that did not stick is warned about, never
        raised -- abort() runs inside the pipeline's KeyboardInterrupt
        handler, and a raise here would replace the clean Ctrl-C (exit 130)
        with a traceback. On a confirmed death, sweeps the dead child's own
        epub tmp: the hard kill skips epub.py's finally cleanup, and the tmp
        is named after the child's pid (epub.py's os.getpid() inside the
        child). The pid-scoped glob never touches another process's tmp.

        Callers must still hold the child in _proc (it is cleared only
        after this returns)."""
        proc = self._proc
        _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("[warn] epub auto-build builder survived the kill - "
                  "it may still be running")
            return False
        removed: list[Path] = []
        try:
            for path in sorted((self._project_dir / "export").glob(
                    f"*.epub.{proc.pid}.tmp")):
                path.unlink(missing_ok=True)
                removed.append(path)
        except OSError:
            pass
        if removed:
            print(f"[warn] removed {len(removed)} stale epub temp file(s) "
                  f"left by the killed build")
        return True

    def _close_log(self) -> None:
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None
