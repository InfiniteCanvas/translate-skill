"""Two-tier JSONL trace logs: orchestration per invocation, model IO per chapter.

Every event belongs to exactly one tier, and the tiers never mix:

  Tier 1 -- logs/run-<run_id>.jsonl. What happened in this run: run and
    chapter lifecycle, stage transitions, gate verdicts, degradations, and a
    per-call ``llm_call`` metadata summary. It NEVER carries a prompt or a
    response, so the timeline can never grow a body by accident.

  Tier 2 -- logs/chapters/<stem>/run-<run_id>.jsonl. What the models were
    given and said for ONE chapter: llm_request / llm_response plus the
    pipeline's reading of individual model outputs (result / chunk /
    feedback). A tier-2 event with no chapter lands in the project bucket,
    logs/project/run-<run_id>.jsonl (profile and the review passes).

``run_id`` (YYYYMMDD-HHMMSS-<command>-<pid>) is computed ONCE per resolved
project and stored in that project's cache entry, so a tier-1 file opened at
03:12:00 and a chapter's tier-2 file opened at 03:12:01 still join on one
id. Two projects in one process never append into each other's logs/.

Retention prunes each bucket independently -- the root and project buckets to
``log_llm_keep_runs``, every chapter directory to ``log_chapter_keep_runs``
-- by modification time. The prune glob matches the ``run-<id>.jsonl`` name
shape only, so index.jsonl (append-only: it is the crash signal, and being
oldest by mtime it is exactly what "keep the newest N" would delete first),
report.md, epub-build.log and legacy ``llm-*.jsonl`` files are never touched.

Logging must never break the pipeline: every failure -- including a payload
value json cannot serialize -- is swallowed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import config

RUN_PREFIX = "run-"
INDEX_NAME = "index.jsonl"

TIER_2_EVENTS = frozenset({
    "llm_request", "llm_response", "result", "chunk", "feedback",
})

_BODY_EVENTS = frozenset({"llm_request", "llm_response"})
_BODY_FIELDS = (("prompt", "prompt_chars"), ("response", "response_chars"))

_run_path: Path | None = None
_run_project: Path | None = None

_paths: dict[str, "_Run"] = {}

_LOG_LOCK = threading.Lock()


@dataclass
class _Run:
    """One invocation's state for one resolved project."""

    run_id: str
    flags: dict[str, Any]
    tier1: Path
    files: dict[str | None, Path] = field(default_factory=dict)
    counters: dict[str | None, dict[str, Any]] = field(default_factory=dict)


def _command_tag() -> str:
    """Best-effort CLI subcommand for the run filename (e.g. 'translate').

    Skips `--project DIR` (both `--project DIR` and `--project=DIR` forms) so
    a project directory's name is never mistaken for the command -- fix.py
    and autobuild.py always spawn children with `--project DIR` first. Other
    flag values are indistinguishable from positionals without a real parse,
    hence best-effort."""
    skip_value = False
    for arg in sys.argv[1:]:
        if skip_value:
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


def _raw_config(project_dir: Path) -> dict:
    try:
        data = json.loads(
            (project_dir / "config.json").read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _as_count(value: Any, fallback: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _keep_count(project_dir: Path) -> int:
    return _as_count(_raw_config(project_dir).get("log_llm_keep_runs"),
                      config.DEFAULTS["log_llm_keep_runs"])


def _resolve_flags(project_dir: Path) -> dict[str, Any]:
    """All five log flags, read from config.json ONCE per invocation.

    Reading them per event would be a real regression across ~50 events per
    chapter; every emitter and the trace hook reads no config of its own, so
    this is the only place gating flags are resolved."""
    raw = _raw_config(project_dir)
    return {
        "orchestration": bool(raw.get("log_orchestration",
                                      config.DEFAULTS["log_orchestration"])),
        "llm": bool(raw.get("log_llm", config.DEFAULTS["log_llm"])),
        "prompt_bodies": bool(raw.get("log_prompt_bodies",
                                      config.DEFAULTS["log_prompt_bodies"])),
        "keep_runs": _as_count(raw.get("log_llm_keep_runs"),
                               config.DEFAULTS["log_llm_keep_runs"]),
        "chapter_keep_runs": _as_count(raw.get("log_chapter_keep_runs"),
                                       config.DEFAULTS["log_chapter_keep_runs"]),
    }


def _prune(base: Path, keep: int) -> None:
    """Keep the newest `keep` run files in one bucket, by mtime.

    The glob matches the run-<id>.jsonl name shape exactly, so index.jsonl,
    report.md, epub-build.log, the chapters/ tree and legacy llm-*.jsonl
    files are all out of reach."""
    try:
        runs = sorted(base.glob(f"{RUN_PREFIX}*.jsonl"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in runs[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _chapter_key(value: Any) -> str | None:
    """The bucket key for an event's chapter: the chapter file's stem, or
    None for a chapter-less call (the project bucket).

    Every stem reaching the logger came from a CHAPTER_RE match, so it is
    already filesystem-safe -- no sanitizing or hashing, which would be dead
    code. `Path(file).stem` is idempotent, so a caller passing either the
    filename or the stem resolves to the same directory."""
    if value is None or value == "":
        return None
    return Path(str(value)).stem


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _append(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _start_run(proj: Path) -> _Run:
    """Open this project's first run for the invocation: compute the run_id
    once, resolve the flags once, occupy the tier-1 retention slot, prune."""
    base = proj / "logs"
    base.mkdir(parents=True, exist_ok=True)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{_command_tag()}-{os.getpid()}"
    flags = _resolve_flags(proj)
    tier1 = base / f"{RUN_PREFIX}{run_id}.jsonl"
    if flags["orchestration"]:
        tier1.touch()
    _prune(base, flags["keep_runs"])
    run = _Run(run_id=run_id, flags=flags, tier1=tier1)
    _paths[str(proj)] = run
    return run


def _tier2_file(proj: Path, run: _Run, chapter: str | None) -> Path:
    """The run file for one chapter, or logs/project/ for a chapter-less
    call. Opened once per invocation per bucket and pruned to its own
    retention count, independently of every other chapter."""
    cached = run.files.get(chapter)
    if cached is not None and cached.exists():
        return cached
    base = proj / "logs"
    directory = base / "project" if chapter is None else base / "chapters" / chapter
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{RUN_PREFIX}{run.run_id}.jsonl"
    path.touch()
    _prune(directory,
           run.flags["keep_runs"] if chapter is None
           else run.flags["chapter_keep_runs"])
    run.files[chapter] = path
    return path


def _index_path(proj: Path, chapter: str | None) -> Path:
    base = proj / "logs"
    return (base / "project" / INDEX_NAME if chapter is None
            else base / "chapters" / chapter / INDEX_NAME)


def _current(proj: Path) -> _Run:
    """The project's run, opening one if this is its first event."""
    if _run_path is None:
        _paths.clear()
    run = _paths.get(str(proj))
    return _start_run(proj) if run is None else run


def _strip_bodies(event: dict[str, Any]) -> dict[str, Any]:
    """Replace prompt/response with their character counts. The call stays
    fully accountable -- finish_reason, usage, elapsed_s and error are kept
    -- without the text."""
    out = dict(event)
    for body, chars in _BODY_FIELDS:
        if body in out:
            value = out.pop(body)
            out[chars] = len(value) if isinstance(value, str) else 0
    return out


def _count(run: _Run, chapter: str | None, event: dict[str, Any]) -> None:
    """Accumulate one llm_call's usage into the chapter's counters.

    Runs BEFORE gating, so accounting survives log_orchestration: false. A
    failed call has usage: null and contributes one call and zero tokens."""
    slot = run.counters.setdefault(
        chapter, {"calls": 0, "tokens": {}, "started": time.monotonic()})
    slot["calls"] += 1
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return
    total = usage.get("total_tokens")
    if not isinstance(total, int):
        prompt_tokens = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = ((prompt_tokens if isinstance(prompt_tokens, int) else 0)
                 + (completion if isinstance(completion, int) else 0))
    if total:
        job = str(event.get("job") or "")
        slot["tokens"][job] = slot["tokens"].get(job, 0) + total


def log_event(project_dir: Path | str, event: dict[str, Any]) -> None:
    """Append one event to its tier's run file (best effort).

    Each event is routed by name (TIER_2_EVENTS, defaulting to tier 1) and
    gated against this project's flags resolved once per invocation: tier-1
    events require log_orchestration; llm_request/llm_response require
    log_llm, with the bodies stripped when log_prompt_bodies is off.
    result/chunk/feedback are unconditional, so log_llm: false still leaves
    a chapter's tier-2 file with the pipeline's own per-chunk record.

    A different resolved project_dir gets its own run; assigning
    `_run_path = None` forces the next event to start fresh runs for both
    tiers and clears every counter."""
    global _run_path, _run_project
    with _LOG_LOCK:
        try:
            proj = Path(project_dir).resolve()
            run = _current(proj)
            _run_path, _run_project = run.tier1, proj

            name = event.get("event")
            chapter = _chapter_key(event.get("chapter"))
            if name == "llm_call":
                _count(run, chapter, event)

            if name in TIER_2_EVENTS:
                if name in _BODY_EVENTS and not run.flags["llm"]:
                    return
                path = _tier2_file(proj, run, chapter)
                payload = (event if run.flags["prompt_bodies"] or name not in _BODY_EVENTS
                           else _strip_bodies(event))
            else:
                if not run.flags["orchestration"]:
                    return
                path = run.tier1
                payload = event
            _append(path, {"ts": _now(), "run_id": run.run_id, **payload})
        except Exception:
            pass


def index_line(project_dir: Path | str, chapter: str | None, phase: str,
               **fields: Any) -> None:
    """Append one open/close side-record to a bucket's index.jsonl.

    Index lines are the bucket's own metadata and the crash signal (an open
    line with no matching close marks the invocation that died), so they are
    written unconditionally of both gates and are never pruned."""
    global _run_path, _run_project
    with _LOG_LOCK:
        try:
            proj = Path(project_dir).resolve()
            run = _current(proj)
            _run_path, _run_project = run.tier1, proj
            path = _index_path(proj, _chapter_key(chapter))
            path.parent.mkdir(parents=True, exist_ok=True)
            _append(path, {"ts": _now(), "run_id": run.run_id,
                           "phase": phase, **fields})
        except Exception:
            pass


def note_chapter_start(project_dir: Path | str, chapter: str | None) -> None:
    """Seed a chapter's counters and elapsed clock at chapter_start, so
    chapter_end can report elapsed_s even when no llm_call ever lands."""
    with _LOG_LOCK:
        try:
            proj = Path(project_dir).resolve()
            run = _paths.get(str(proj))
            if run is not None:
                run.counters.setdefault(
                    _chapter_key(chapter),
                    {"calls": 0, "tokens": {}, "started": time.monotonic()})
        except Exception:
            pass


def _take(run: _Run, chapter: str | None) -> dict[str, Any]:
    slot = run.counters.pop(chapter, None)
    if not slot:
        return {"calls": 0, "tokens": {}, "elapsed_s": 0.0}
    return {"calls": slot["calls"], "tokens": dict(slot["tokens"]),
            "elapsed_s": round(time.monotonic() - slot["started"], 2)}


def take_chapter_stats(project_dir: Path | str, chapter: str | None) -> dict[str, Any]:
    """Read-and-reset one chapter's calls/tokens/elapsed_s for chapter_end.

    A crash may cut aggregation short; the payload then reports what is
    known, which is why the index close line -- not these numbers -- is the
    reliable crash signal."""
    with _LOG_LOCK:
        run = _paths.get(str(Path(project_dir).resolve()))
        return _take(run, _chapter_key(chapter)) if run is not None else {
            "calls": 0, "tokens": {}, "elapsed_s": 0.0}


def take_run_stats(project_dir: Path | str) -> dict[str, Any]:
    """Read-and-reset the whole invocation's call/token totals for run_end.

    Both return paths carry the SAME keys. The no-run branch used to omit
    `elapsed_s`, which made `_run_end` raise KeyError when it was reached for a
    project that had logged nothing yet (nothing starts a run before _run_end
    today, but the shape must not differ by branch).
    """
    with _LOG_LOCK:
        run = _paths.get(str(Path(project_dir).resolve()))
        if run is None:
            return {"calls": 0, "tokens": {}, "elapsed_s": 0.0}
        calls = 0
        tokens: dict[str, int] = {}
        started = time.monotonic()
        for chapter, slot in list(run.counters.items()):
            calls += slot["calls"]
            for job, total in slot["tokens"].items():
                tokens[job] = tokens.get(job, 0) + total
            if chapter is None:
                started = min(started, slot["started"])
        run.counters.clear()
        return {"calls": calls, "tokens": tokens, "elapsed_s": round(
            time.monotonic() - started, 2)}


def current_run_id(project_dir: Path | str) -> str | None:
    """This project's run_id, or None before its first event. The report
    writer needs it to bound its read to the current invocation."""
    with _LOG_LOCK:
        run = _paths.get(str(Path(project_dir).resolve()))
        return run.run_id if run is not None else None


def bucket_dir(project_dir: Path | str, chapter: str | None) -> Path:
    """The directory holding one bucket's run file, index.jsonl and (for a
    chapter) report.md. The reader side -- `translate logs`, logreport --
    resolves buckets through here so it never re-derives the layout."""
    base = Path(project_dir) / "logs"
    return base if chapter is None else base / "chapters" / _chapter_key(chapter)
