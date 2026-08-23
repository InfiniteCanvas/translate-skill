"""Background epub rebuilds after each translated chapter.

The pipeline triggers a build subprocess after every chapter that reaches
status "translated". Only one build runs at a time (the epub is written
non-atomically to a single export path, so overlapping builds would corrupt
it); triggers arriving while a build runs just set a pending flag. finalize()
waits out the running build and, when anything is pending, runs one final
synchronous build so the finished epub always includes every chapter.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import IO

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "translate.py"


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
        """Interrupt path: kill the running child; never spawn another."""
        self._pending = None
        if self._proc is not None:
            self._proc.kill()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
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
            )
        except BaseException:
            fh.close()
            raise
        self._log_fh = fh
        self._reason = reason

    def _reap(self, wait: bool = False) -> None:
        if self._proc is None:
            return
        if wait:
            self._proc.wait()
        elif self._proc.poll() is None:
            return  # still running
        code = self._proc.returncode
        self._close_log()
        if code == 0:
            print(f"[epub-auto] build ok (after {self._reason})")
        else:
            print(f"[warn] epub auto-build failed, exit {code} (after {self._reason}) - see logs/epub-build.log")
        self._proc = None
        self._reason = ""

    def _close_log(self) -> None:
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None
