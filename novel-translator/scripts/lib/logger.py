"""Per-invocation JSONL trace logs with retention: logs/llm-* runs.

Each CLI process writes one file per project it logs for --
llm-YYYYMMDD-HHMMSS-<command>-<pid>.jsonl, decided at that project's first
logged event and re-pointed whenever log_event sees a different project_dir
(two projects in one process never append into each other's logs/); every
LLM call lands there with the full prompt, raw response, finish_reason,
usage, sampling params, and elapsed time, alongside the pipeline's
stage/gate events. This is the debugging ground truth -- the console output
is a summary, the log is what actually happened.

At each run file's first write the project's logs/llm-*.jsonl (including
files from the old daily scheme) are pruned to the newest config
log_llm_keep_runs entries by modification time. Logging must never break
the pipeline: all failures are swallowed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Plain package import (no cycle: config imports nothing from lib).
from lib import config

# The current run's JSONL file and the resolved project_dir it belongs to:
# log_event re-points both whenever it sees a different project_dir (a second
# project logged in-process must not append into the first project's logs/).
# Setting _run_path = None forces the next event to open a fresh run.
_run_path: Path | None = None
_run_project: Path | None = None

# log_event is called from worker threads too (the consensus fan-out logs
# candidate calls concurrently), and its check-then-act on
# _run_path/_run_project plus the append write are not thread-safe: without
# serialization two threads could race past the run-file check and interleave
# partial lines into one file. One lock held for the whole body.
_LOG_LOCK = threading.Lock()


def _command_tag() -> str:
    """Best-effort CLI subcommand for the run filename (e.g. 'translate').

    Skips `--project DIR` (both `--project DIR` and `--project=DIR` forms) so
    a project directory's name is never mistaken for the command -- fix.py
    and autobuild.py always spawn children with `--project DIR` first. Other
    flag values are indistinguishable from positionals without a real parse,
    hence best-effort."""
    skip_value = False
    for arg in sys.argv[1:]:
        if skip_value:  # the directory consumed by a preceding --project
            skip_value = False
            continue
        if arg == "--project":
            skip_value = True
            continue
        if arg.startswith("--project="):
            continue
        if not arg.startswith("-"):
            tag = re.sub(r"[^a-z0-9-]+", "", arg.lower()) or "run"
            return tag[:24]
    return "run"


def _keep_count(project_dir: Path) -> int:
    try:
        cfg = json.loads((project_dir / "config.json").read_text(encoding="utf-8-sig"))
        return max(0, int(cfg.get("log_llm_keep_runs",
                                  config.DEFAULTS["log_llm_keep_runs"])))
    except (OSError, ValueError, TypeError):
        return config.DEFAULTS["log_llm_keep_runs"]


def _prune(base: Path, keep: int) -> None:
    runs = sorted(base.glob("llm-*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in runs[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def log_event(project_dir: Path | str, event: dict[str, Any]) -> None:
    """Append one event to this invocation's per-project JSONL log (best
    effort): a different resolved project_dir re-points the run file, and
    clearing `_run_path` (None) forces the next event to start a fresh run."""
    global _run_path, _run_project
    with _LOG_LOCK:
        try:
            proj = Path(project_dir).resolve()
            if _run_path is None or _run_project != proj:
                base = proj / "logs"
                base.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                _run_path = base / f"llm-{stamp}-{_command_tag()}-{os.getpid()}.jsonl"
                _run_project = proj
                _run_path.touch()  # occupy a retention slot before pruning
                _prune(base, _keep_count(proj))
            entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **event}
            with _run_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
