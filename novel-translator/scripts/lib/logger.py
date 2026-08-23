"""Append-only JSONL trace log per project: logs/llm-YYYYMMDD.jsonl.

Every LLM call is logged with the full prompt, raw response, finish_reason,
usage, sampling params, and elapsed time; the pipeline logs stage/gate
events into the same stream. This is the debugging ground truth — the
console output is a summary, the log is what actually happened.

Logging must never break the pipeline: all failures are swallowed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_event(project_dir: Path | str, event: dict[str, Any]) -> None:
    """Append one event to today's JSONL log file (best effort)."""
    try:
        base = Path(project_dir) / "logs"
        base.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        entry = {"ts": now.isoformat(timespec="milliseconds"), **event}
        path = base / f"llm-{now:%Y%m%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
