"""Per-invocation JSONL trace logs with retention: logs/llm-* runs.

Each CLI process writes one file — llm-YYYYMMDD-HHMMSS-<command>-<pid>.jsonl
— decided once at the process's first logged event; every LLM call lands
there with the full prompt, raw response, finish_reason, usage, sampling
params, and elapsed time, alongside the pipeline's stage/gate events. This
is the debugging ground truth — the console output is a summary, the log is
what actually happened.

At first write the process also prunes logs/llm-*.jsonl (including files
from the old daily scheme) to the newest config log_llm_keep_runs entries
by modification time. Logging must never break the pipeline: all failures
are swallowed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_KEEP = 5
_run_path: Path | None = None


def _command_tag() -> str:
    """Best-effort CLI subcommand for the run filename (e.g. 'translate')."""
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            tag = re.sub(r"[^a-z0-9-]+", "", arg.lower()) or "run"
            return tag[:24]
    return "run"


def _keep_count(project_dir: Path) -> int:
    try:
        cfg = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
        return max(0, int(cfg.get("log_llm_keep_runs", _DEFAULT_KEEP)))
    except (OSError, ValueError, TypeError):
        return _DEFAULT_KEEP


def _prune(base: Path, keep: int) -> None:
    runs = sorted(base.glob("llm-*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in runs[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def log_event(project_dir: Path | str, event: dict[str, Any]) -> None:
    """Append one event to this invocation's JSONL log (best effort)."""
    global _run_path
    try:
        if _run_path is None:
            base = Path(project_dir) / "logs"
            base.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            _run_path = base / f"llm-{stamp}-{_command_tag()}-{os.getpid()}.jsonl"
            _run_path.touch()  # occupy a retention slot before pruning
            _prune(base, _keep_count(Path(project_dir)))
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **event}
        with _run_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
