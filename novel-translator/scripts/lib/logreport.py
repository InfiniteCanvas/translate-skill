"""report.md: the human-readable face of a chapter's trace logs.

Two writers, by design:

  chapter_end calls write_run_report() on the pipeline's critical path. It
    reads ONLY the current run's tier-1 file, filtered to this run_id and this
    chapter, and writes metadata: header, stage timeline, gate verdicts and
    the call table. No prompt or response bodies, no scan of the chapter's
    whole history -- bounded work per chapter.

  translate logs --report calls write_full_report() on demand. It scans the
    chapter directory's run-*.jsonl files (a directory scan, not the index,
    because the recap backfill writes a run file into the predecessor's
    directory that the predecessor's index never records) and, with io=True,
    wraps every call that actually carried bodies in <details>.

Both HTML-escape model output before interpolating it: a reply containing
</details> or a fenced block would otherwise corrupt exactly the structure the
report asserts on.

Inherits logger's never-raise contract: any failure prints one [warn] and
leaves the previous report in place, so a reporting bug can never take the
pipeline down or destroy the report it meant to refresh.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Iterator

from lib import logger, project

REPORT_NAME = "report.md"
INDEX_NAME = "index.jsonl"


def _esc(value: Any) -> str:
    """HTML-escape model output. None becomes an empty cell, not 'None'."""
    return html.escape("" if value is None else str(value), quote=False)


def _cell(value: Any) -> str:
    """A markdown TABLE cell: escaped, with pipes backslash-escaped so a value
    containing one cannot open a new column."""
    return _esc(value).replace("|", "\\|")


def _read_jsonl(path: Path) -> Iterator[dict]:
    """Every parseable line of one run file. A truncated final line (a crash
    mid-append) is skipped rather than raised."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def run_files(project_dir: Path, stem: str) -> list[Path]:
    """Every retained run file for one chapter, oldest first. index.jsonl and
    report.md are not run files and never appear."""
    directory = logger.bucket_dir(project_dir, stem)
    try:
        found = list(directory.glob("run-*.jsonl"))
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name)


def stale_run_ids(project_dir: Path, stem: str) -> set[str]:
    """Run ids the index records but whose files retention has since removed.
    Their index lines are kept and marked, never silently dropped: an open
    line with no close is the crash signal, so the history must survive."""
    index = logger.bucket_dir(project_dir, stem) / INDEX_NAME
    closed: set[str] = set()
    seen: set[str] = set()
    for row in _read_jsonl(index):
        run_id = row.get("run_id")
        if not isinstance(run_id, str):
            continue
        seen.add(run_id)
        if row.get("phase") == "close":
            closed.add(run_id)
    retained = {p.name[len("run-"):-len(".jsonl")] for p in run_files(project_dir, stem)}
    return {r for r in seen - retained if r not in closed}


def _usage_tokens(usage: Any) -> tuple[int, int]:
    """(prompt, completion) token counts. A missing or malformed usage
    contributes zeros -- the table must never invent numbers."""
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (prompt if isinstance(prompt, int) else 0,
            completion if isinstance(completion, int) else 0)


def _header(stem: str, number: Any, rows: list[dict],
            run_ids: list[str]) -> list[str]:
    start = next((r for r in rows if r.get("event") == "chapter_start"), None)
    end = next((r for r in rows if r.get("event") == "chapter_end"), None)
    outcome = (end or {}).get("outcome") or "(no chapter_end recorded)"
    attempts = (end or {}).get("attempts", (start or {}).get("attempts", 0))
    lines = [f"- chapter: {stem}", f"- number: {number}"]
    if start is not None:
        lines.append(f"- source lines: {start.get('lines', '?')}")
    lines += [
        f"- outcome: {outcome}",
        f"- attempts: {attempts}",
        f"- runs covered: {len(run_ids) if run_ids else 1}"
        + (f" ({', '.join(run_ids)})" if run_ids else ""),
    ]
    return lines


def _stages(rows: list[dict]) -> list[str]:
    """Stage timeline in order, collapsing each stage's begin/end pair into
    one row. A stage with a begin but no end (a crash inside it) is shown
    explicitly as such rather than dropped."""
    order: list[str] = []
    spans: dict[str, dict] = {}
    for row in rows:
        if row.get("event") != "stage":
            continue
        stage = str(row.get("stage"))
        if stage not in spans:
            order.append(stage)
            spans[stage] = {"attempt": row.get("attempt"), "start": None, "elapsed": None}
        if row.get("phase") == "begin":
            spans[stage]["start"] = row.get("ts")
        else:
            spans[stage]["elapsed"] = row.get("elapsed_s")
    if not order:
        return ["_no stage events recorded_"]
    lines = ["| stage | attempt | elapsed_s | began |",
             "|---|---|---|---|"]
    for stage in order:
        span = spans[stage]
        elapsed = ("-" if span["elapsed"] is None else span["elapsed"])
        began = span["start"] or "-"
        if span["elapsed"] is None:
            began = f"{began} (did not end - see outcome)"
        lines.append(f"| {_cell(stage)} | {_cell(span['attempt'])} | "
                     f"{_cell(elapsed)} | {_cell(began)} |")
    return lines


def _gates(rows: list[dict]) -> list[str]:
    """Every gate verdict with EVERY reason verbatim -- never the truncated
    list attempt_failed carries."""
    gates = [r for r in rows if r.get("event") == "gate"]
    if not gates:
        return ["_no gate events recorded_"]
    lines: list[str] = []
    for gate in gates:
        lines.append(f"- **{_esc(gate.get('stage'))}** -> "
                     f"{_esc(gate.get('verdict'))}")
        reasons = gate.get("reasons")
        if isinstance(reasons, list) and reasons:
            lines.extend(f"  - {_esc(reason)}" for reason in reasons)
        else:
            lines.append("  - _no reasons recorded_")
    return lines


def _calls(rows: list[dict]) -> list[str]:
    """The per-call table. Token totals are summed from the llm_call lines
    themselves, so they cannot drift from the JSONL."""
    calls = [r for r in rows if r.get("event") == "llm_call"]
    if not calls:
        return ["_no model calls recorded_"]
    lines = ["| job | model | candidate | prompt_tok | completion_tok | "
             "elapsed_s | finish | error |",
             "|---|---|---|---|---|---|---|---|"]
    prompt_total = completion_total = 0
    for call in calls:
        prompt_tokens, completion = _usage_tokens(call.get("usage"))
        prompt_total += prompt_tokens
        completion_total += completion
        candidate = call.get("candidate")
        label = (f"{candidate}/{call.get('candidates')}"
                 if candidate is not None else
                 (f"for {call.get('consensus_for')}"
                  if call.get("consensus_for") else "-"))
        error = call.get("error")
        lines.append(
            f"| {_cell(call.get('job'))} | {_cell(call.get('model'))} | "
            f"{_cell(label)} | {prompt_tokens} | {completion} | "
            f"{_cell(call.get('elapsed_s'))} | {_cell(call.get('finish_reason'))} | "
            f"{_cell('-') if not error else 'yes'} |")
    lines.append("")
    lines.append(f"**Totals:** {len(calls)} call(s), {prompt_total} prompt + "
                 f"{completion_total} completion tokens.")
    return lines


def _details(entries: list[dict]) -> list[str]:
    """One <details> block per call that actually carried bodies. Calls
    logged with log_prompt_bodies off have prompt_chars/response_chars only
    and appear metadata-only -- the report never invents a body it does not
    have.

    Bodies are HTML-escaped even though they sit inside a fenced block: a
    renderer closes <details> on the first `</details>` it sees, fenced or
    not, so a literal closing tag in a model reply would truncate exactly
    the structure this report asserts on. Escaped, the markdown renders the
    original characters back."""
    lines: list[str] = []
    for index, entry in enumerate(entries, start=1):
        if entry.get("event") != "llm_response":
            continue
        response = entry.get("response")
        if not isinstance(response, str):
            continue
        request = next((e for e in reversed(entries[:index])
                        if e.get("event") == "llm_request"
                        and e.get("call_id") == entry.get("call_id")), None)
        lines.append("<details>")
        lines.append(f"<summary>call {index}: {_esc(entry.get('job'))} / "
                     f"{_esc(entry.get('model'))}</summary>")
        if request is not None and isinstance(request.get("prompt"), str):
            lines.append("")
            lines.append("**Prompt**")
            lines.append("")
            lines.append("```")
            lines.append(_esc(request["prompt"]))
            lines.append("```")
        lines.append("")
        lines.append("**Response**")
        lines.append("")
        lines.append("```")
        lines.append(_esc(response))
        lines.append("```")
        lines.append("")
        lines.append(f"finish_reason: `{_esc(entry.get('finish_reason'))}`")
        lines.append("")
        lines.append("</details>")
    return lines


def _render(stem: str, number: Any, rows: list[dict], run_ids: list[str],
            entries: list[dict], with_io: bool) -> str:
    lines = [f"# {stem}", "", "## Header", ""]
    lines += _header(stem, number, rows, run_ids)
    lines += ["", "## Stage timeline", ""] + _stages(rows)
    lines += ["", "## Gate verdicts", ""] + _gates(rows)
    lines += ["", "## Calls", ""] + _calls(rows)
    if with_io:
        lines += ["", "## Model exchanges", ""]
        details = _details(entries)
        lines += details or ["_no call carried a stored prompt/response body "
                             "(log_prompt_bodies was off, or log_llm was off)_"]
    return "\n".join(lines) + "\n"


def tier1_rows(project_dir: Path, stem: str, run_id: str | None
               ) -> tuple[list[dict], list[str]]:
    """This chapter's ORCHESTRATION rows -- read from the tier-1 ROOT bucket,
    filtered to this chapter.

    The stage timeline, gate verdicts and call table all live in the tier-1
    file (llm_call is tier 1 by design), so this is the report's spine. With
    run_id the read is bounded to the current invocation (the chapter_end
    path); with None every retained root run is scanned, which is the
    --report path. With log_orchestration off there is no root file and this
    returns nothing, which is exactly the header-only report the caller
    writes."""
    base = project_dir / "logs"
    wanted = run_id or None
    rows: list[dict] = []
    covered: list[str] = []
    try:
        files = sorted(base.glob("run-*.jsonl"))
    except OSError:
        return [], []
    for path in files:
        rid = path.name[len("run-"):-len(".jsonl")]
        if wanted is not None and rid != wanted:
            continue
        picked = [r for r in _read_jsonl(path)
                  if r.get("chapter") in (stem, f"{stem}.md")]
        if picked:
            covered.append(rid)
            rows.extend(picked)
    return rows, covered


def _tier2_entries(project_dir: Path, stem: str, run_ids: list[str]) -> list[dict]:
    """Every retained tier-2 line for this chapter.

    A DIRECTORY SCAN, not the index: the recap backfill writes a run file
    into the predecessor's directory that the predecessor's index never
    records, so the index alone undercounts what the chapter actually saw."""
    entries: list[dict] = []
    for path in run_files(project_dir, stem):
        rid = path.name[len("run-"):-len(".jsonl")]
        if run_ids and rid not in run_ids:
            continue
        entries.extend(_read_jsonl(path))
    return entries


def write_run_report(project_dir: Path, file: str, number: Any,
                     run_id: str) -> None:
    """The metadata-only report chapter_end writes. Never raises."""
    try:
        stem = Path(file).stem
        rows, covered = tier1_rows(project_dir, stem, run_id or None)
        project.atomic_write_text(
            logger.bucket_dir(project_dir, stem) / REPORT_NAME,
            _render(stem, number, rows, covered, [], False), "\n")
    except Exception as exc:
        print(f"[warn] report for {file} not written: {type(exc).__name__}: {exc}")


def write_full_report(project_dir: Path, stem: str, number: Any = None,
                      io: bool = False) -> Path | None:
    """The on-demand report `translate logs --report` writes: every retained
    run, and with io=True every call that carried a body. Returns the path,
    or None when nothing could be written."""
    try:
        rows, covered = tier1_rows(project_dir, stem, None)
        entries = _tier2_entries(project_dir, stem, covered)
        if not rows and not entries:
            print(f"[warn] no retained run files for {stem} - report not written")
            return None
        stale = stale_run_ids(project_dir, stem)
        text = _render(stem, number, rows, covered, entries, io)
        if stale:
            text += ("\n## Pruned runs\n\nThese runs are recorded in "
                     f"{INDEX_NAME} but their files were removed by retention: "
                     + ", ".join(sorted(stale)) + "\n")
        path = logger.bucket_dir(project_dir, stem) / REPORT_NAME
        project.atomic_write_text(path, text, "\n")
        return path
    except Exception as exc:
        print(f"[warn] report for {stem} not written: {type(exc).__name__}: {exc}")
        return None
