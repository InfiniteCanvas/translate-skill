"""report.html: the project-wide graphical face of every trace log.

logreport.py answers "what happened to THIS chapter". This module answers "what
happened to this project" -- where the wall-clock went, where the tokens went,
which chapters failed, and which model served which job -- in one
self-contained page at ``logs/report.html``.

Three writers, by design:

  _run_end calls refresh() on the happy path, so the page is never stale after
    a translate/retry/tn/review/profile run.

  translate logs --html calls write_dashboard() on demand. That is the
    INVESTIGATIVE path: main() hard-exits on KeyboardInterrupt with a fan-out
    live (translate.py:2275-2283), so an interrupted run never reaches
    _run_end and its chapters' index close lines would otherwise sit
    unrendered -- exactly when someone wants to look.

  Both go through the same collect() -> render() -> atomic_write_text path.

TWO artifacts come out of one collect(): the page at ``logs/report.html`` and
the data at ``logs/report.json``. They cannot disagree because there is one
collection pass, and the JSON is the contract tests assert against and the input
a future visualisation would read. The page's CSS and JS live as real files in
``assets/dashboard/`` and are inlined by render(), so the page stays one
portable file that survives being archived or emailed on its own.

The SPINE is the chapter ``index.jsonl`` close lines, not tier 1. Tier 1 is
pruned to ``log_llm_keep_runs`` per project, so a long-lived project can have
NONE of it -- which is exactly the case of the sample project this was built
against, where ``logs/run-*.jsonl`` is empty while every chapter's index is
full. Tier 1 is therefore an optional ENRICHMENT: it adds per-stage durations,
gate verdicts and the per-call table when present, and its absence is stated in
the page rather than rendered as zero-width bars. See PLAN-2026-10-08 §2.2.

Cost control: the spine is ~8 KB of index files. The tier-2 buckets are megabytes
of prompt/response bodies and are read ONLY under --io, behind a byte cap.

Inherits logreport's never-raise contract: any failure prints one [warn] and
leaves the previous page in place. project.atomic_write_text means a failure
mid-render cannot even truncate a good page.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from lib import logger, project

REPORT_NAME = "report.html"
REPORT_JSON_NAME = "report.json"
INDEX_NAME = "index.jsonl"

DASHBOARD_DIR = (Path(__file__).resolve().parent.parent.parent
                 / "assets" / "dashboard")


_NUM_RE = re.compile(r"^CHAPTER_([0-9]{4})$")

_EPUB_HEAD_RE = re.compile(
    r"^=== epub build after (?P<file>.+?) \| (?P<ts>.+?) ===\s*$")

_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[/\\](?:[\w.\-]+[/\\])+[\w.\-]*")

_OUTCOME_OK = {"translated", "completed", "ok"}
_OUTCOME_WARN = {"needs-review", "findings", "degraded"}
_OUTCOME_BAD = {"crashed", "failed", "error"}


def _rows(path: Path) -> Iterator[dict]:
    """Every parseable line of one JSONL file.

    A truncated final line is skipped, not raised: index.jsonl is the crash
    signal and a half-written line is what a killed run leaves behind, so
    refusing to read the file would hide exactly the evidence we want.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
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


def _esc(value: Any) -> str:
    """HTML-escape for both text nodes and attribute values.

    quote=True matters: chapter ids, reasons and model names all land inside
    attributes here, and unescaped quotes would break the attribute out.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Any) -> int | float | None:
    """A number, or None. Never coerces a missing value to 0 -- a missing
    number must render as "unknown", not as a real measurement of zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _fmt_duration(seconds: Any) -> str:
    """Seconds as a compact human duration, or '-' when unknown."""
    value = _num(seconds)
    if value is None or value < 0:
        return "-"
    total = int(round(value))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _fmt_count(value: Any) -> str:
    """A token/call count with thousands separators, or '-' when unknown."""
    value = _num(value)
    if value is None:
        return "-"
    return f"{int(value):,}"


def _sort_key(stem: str) -> tuple[int, int, str]:
    """Numeric order for real CHAPTER_NNNN stems; everything else after them,
    alphabetically. A total order, so sorting never raises and never drops a
    bucket -- a pre-migration project's logs still render."""
    match = _NUM_RE.match(stem)
    return (0, int(match.group(1)), "") if match else (1, 0, stem)


def _usage_tokens(usage: Any) -> tuple[int, int]:
    """(prompt, completion). Mirrors logreport._usage_tokens so the dashboard
    and report.md can never disagree about one call."""
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (prompt if isinstance(prompt, int) else 0,
            completion if isinstance(completion, int) else 0)


def _tier1(project_dir: Path) -> list[dict]:
    """Every retained root (tier 1) row. Empty when retention has removed all
    of them, which is a supported state, not an error."""
    rows: list[dict] = []
    try:
        files = sorted((project_dir / "logs").glob("run-*.jsonl"))
    except OSError:
        return []
    for path in files:
        rows.extend(_rows(path))
    return rows


def _chapter_buckets(project_dir: Path) -> list[tuple[str, Path]]:
    """(stem, bucket dir) for every chapter that has logs, numerically
    ordered. A directory with no index.jsonl still counts: tier-2 files can
    exist there and are the only record of the run."""
    base = project_dir / "logs" / "chapters"
    found: list[tuple[str, Path]] = []
    try:
        entries = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return []
    for directory in entries:
        found.append((directory.name, directory))
    return sorted(found, key=lambda item: _sort_key(item[0]))


def _index_runs(bucket: Path) -> list[dict]:
    """One record per chapter-run, in the order the opens appear.

    Each open is paired with the close of the SAME run_id -- pairing on order
    alone would mislabel a run that crashed mid-append. An open with no close
    is the crash signal and is kept as an unclosed run, never discarded.
    """
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    order: list[str] = []
    for row in _rows(bucket / INDEX_NAME):
        run_id = row.get("run_id")
        if not isinstance(run_id, str):
            continue
        phase = row.get("phase")
        if phase == "open":
            if run_id not in opens:
                order.append(run_id)
            opens[run_id] = row
        elif phase == "close":
            closes[run_id] = row
    runs: list[dict] = []
    for run_id in order:
        start = opens[run_id]
        end = closes.get(run_id)
        tokens = end.get("tokens") if end else None
        runs.append({
            "run_id": run_id,
            "command": start.get("command"),
            "started": start.get("ts"),
            "closed": end is not None,
            "outcome": (end or {}).get("outcome"),
            "attempts": (end or {}).get("attempts"),
            "stages": (end or {}).get("stages"),
            "calls": (end or {}).get("calls"),
            "tokens": tokens if isinstance(tokens, dict) else {},
            "elapsed_s": (end or {}).get("elapsed_s"),
            "run_file": (bucket / f"run-{run_id}.jsonl").is_file(),
        })
    return runs


def _stage_spans(tier1: list[dict], stem: str, run_id: str | None
                 ) -> list[dict]:
    """Stage begin/end pairs for one chapter, keyed on (stage, attempt).

    DELIBERATELY DIVERGES from logreport._stages, which keys on stage name
    alone and so keeps only the LAST attempt's elapsed_s. A stacked bar must
    account for every second the run spent, so a retried stage contributes one
    segment per attempt. That is why this page and a chapter's report.md can
    legitimately disagree on a retried stage -- the bar is the sum, the
    report's table is the final attempt.

    `ended` is DERIVED, not read: pipeline.py:1249-1252 emits
    {event, chapter, stage, phase, attempt, elapsed_s} and the end timestamp is
    the event's own `ts`, injected by logger.log_event. A begin with no end is a
    crash inside the stage and is kept, marked, never dropped.
    """
    order: list[tuple[str, Any]] = []
    spans: dict[tuple[str, Any], dict] = {}
    for row in tier1:
        if row.get("event") != "stage" or row.get("chapter") not in (
                stem, f"{stem}.md"):
            continue
        if run_id is not None and row.get("run_id") != run_id:
            continue
        key = (str(row.get("stage")), row.get("attempt"))
        if key not in spans:
            order.append(key)
            spans[key] = {"stage": key[0], "attempt": key[1], "began": None,
                          "elapsed_s": None, "ended": False}
        if row.get("phase") == "begin":
            spans[key]["began"] = row.get("ts")
        else:
            spans[key]["elapsed_s"] = row.get("elapsed_s")
            spans[key]["ended"] = True
    return [spans[key] for key in order]


def _gates(tier1: list[dict], stem: str) -> list[dict]:
    out: list[dict] = []
    for row in tier1:
        if row.get("event") != "gate" or row.get("chapter") not in (
                stem, f"{stem}.md"):
            continue
        reasons = row.get("reasons")
        out.append({"stage": row.get("stage"), "verdict": row.get("verdict"),
                    "reasons": [str(r) for r in reasons]
                    if isinstance(reasons, list) else []})
    return out


def _latest_run_file(bucket: Path, runs: list[dict]
                     ) -> tuple[Path | None, str]:
    """(file, how-it-was-chosen) for the ONE run this chapter reports on.

    MUST agree with what the ledger bar reports: ``collect`` treats
    ``runs[-1]`` -- the most recently OPENED run in index.jsonl -- as the
    chapter's latest run, and every figure, outcome and elapsed_s comes from
    that run's close line. The call ledger therefore reads that same run.

    An earlier draft of this module preferred the last CLOSE line, which is
    wrong: with log_chapter_keep_runs: 3 a closed older run's file coexists
    with a newer run's file, so preferring the close line picks the OLDER run
    and the page shows one run's bar beside another run's calls -- exactly the
    inconsistency this function exists to prevent.

    Falls back to the newest file on disk when that run has none (it opened,
    then died before writing a body), and to (None, "none") when the bucket
    holds no run file at all. run_id is ``YYYYMMDD-HHMMSS-cmd-pid``, so
    lexicographic file order is chronological; two runs in the same second tie
    -break on pid, which is exactly why the index outranks the filename.
    """
    files: list[Path] = []
    try:
        files = sorted(bucket.glob("run-*.jsonl"))
    except OSError:
        return None, "none"
    if runs:
        latest_id = runs[-1].get("run_id")
        if isinstance(latest_id, str):
            wanted = bucket / f"run-{latest_id}.jsonl"
            if wanted.is_file():
                return wanted, "index"
    if files:
        return files[-1], "newest-file (index run has no body)"
    return None, "none"


def _strip_fence(text: str) -> str:
    """Unwrap a ```json fenced block. Returns the input unchanged if it is not
    a fence.

    5 of the sample's 116 newest responses are fenced, and a plain
    ``json.loads`` on those raises -- silently degrading a whole glossary term
    set to raw text. Fence handling is therefore required, not cosmetic.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    parts = stripped.split("```")
    if len(parts) < 2:
        return stripped
    inner = parts[1]
    if inner[:4].lower() == "json":
        inner = inner[4:]
    return inner.strip()


def _parse_body(body: Any) -> tuple[Any, str]:
    """(parsed, how) -- ``how`` is the shape id from the registry below.

    Dispatch is on the PARSED KEY SET, never on ``job`` or ``consensus_for``.
    One job name runs several schemas (``glossary`` alone drives expand ->
    {terms}, merge -> a flat object, cleanup -> {decisions}), and
    ``consensus_for`` records only the job name, so neither can identify the
    task. The registry is data, and anything it does not match falls through to
    the generic renderer -- a schema added later must degrade to "readable",
    not to "raw text".
    """
    if not isinstance(body, str) or not body:
        return None, "text"
    try:
        parsed = json.loads(body)
    except ValueError:
        try:
            parsed = json.loads(_strip_fence(body))
        except ValueError:
            return None, "text"

    if isinstance(parsed, dict):
        keys = set(parsed)
        for required, shape in _SHAPE_REGISTRY:
            if required <= keys:
                return parsed, shape
        if keys == _MERGE_KEYS:
            return parsed, "merge"
    return parsed, "generic"


_SHAPE_REGISTRY: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"title", "lines"}), "lines"),
    (frozenset({"terms"}), "terms"),
    (frozenset({"decisions"}), "cleanup"),
    (frozenset({"verdict", "reasons"}), "verdict"),
    (frozenset({"notes"}), "notes"),
    (frozenset({"recap"}), "recap"),
    (frozenset({"style_summary", "background"}), "profile"),
)
_MERGE_KEYS = frozenset({"source", "translation", "definition", "category"})


def _json_array_of_i_t(text: str) -> list | None:
    """The last balanced JSON array of ``{i, t}`` objects inside ``text``.

    A regex CANNOT do this: novel prose contains [, ], { and } inside the t
    strings, which desyncs any bracket-counting pattern -- the obvious
    ``\\[(?:\\{[^{}]*\\}|...)*\\]`` fails on 20/20 real translator prompts. So
    walk forward from each '[' with a counter that respects string literals and
    backslash escapes, accept the first balanced span that json.loads into a
    list of {i, t}, and keep the LAST one: the source block is the final array
    in the prompt.

    Deliberately marker-free. The array lives under ``### Source Data`` in
    assets/templates/translation.md, but keying on that heading is exactly what
    broke a first attempt (it also matches ``### Task`` at line 7, which grabs
    the wrong bracket, 30/30). Being independent of the template text means an
    edit that moves a heading cannot silently break this.
    """
    found: list | None = None
    total = len(text)
    for start, ch in enumerate(text):
        if ch != "[":
            continue
        depth = 0
        in_str = False
        esc = False
        for k in range(start, total):
            c = text[k]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start:k + 1])
                    except ValueError:
                        break
                    if (isinstance(candidate, list) and candidate
                            and isinstance(candidate[0], dict)
                            and "i" in candidate[0] and "t" in candidate[0]):
                        found = candidate
                    break
    return found


def _collect_calls(project_dir: Path, chapters: list[dict]
                   ) -> tuple[list[dict], list[dict], list[str]]:
    """(calls, unpaired_events, warnings) from the LATEST run of every chapter.

    One authoritative pass, replacing the old _calls_from_tier2 /
    _collect_io pair. Those two disagreed: the first read files[-1] while the
    second walked EVERY retained file, so the index table described one run and
    the body list showed three (measured 116 vs 135 responses on the sample).
    The separate 200 KB cap then dropped 84 of 131 bodies, oldest-run-first,
    because it accumulated in ascending file order -- which is what made the
    page look like it was "missing" logs.

    Requests and responses are paired on ``call_id``. A call with a response
    and no request, or vice versa, is KEPT and flagged (``paired``), never
    discarded: both are real states a killed run leaves behind.

    ``unpaired_events`` collects ``result`` / ``chunk`` rows, which carry no
    call_id and are not calls (42 and 10 respectively in the sample).
    """
    calls: list[dict] = []
    unpaired: list[dict] = []
    warnings: list[str] = []
    for chapter in chapters:
        stem = chapter["stem"]
        bucket = logger.bucket_dir(project_dir, stem)
        path, basis = _latest_run_file(bucket, chapter.get("runs") or [])
        if path is None:
            chapter["call_source"] = basis
            continue
        chapter["call_source"] = basis
        requests: dict[str, dict] = {}
        responses: dict[str, dict] = {}
        order: list[str] = []
        seen: set[str] = set()
        for row in _rows(path):
            event = row.get("event")
            if event in ("llm_request", "llm_response"):
                raw_id = row.get("call_id")
                if isinstance(raw_id, str) and raw_id:
                    call_id = raw_id
                else:
                    call_id = f"~{event}:{row.get('ts') or ''}:{len(order)}"
                if event == "llm_request":
                    requests[call_id] = row
                else:
                    responses[call_id] = row
                if call_id not in seen:
                    seen.add(call_id)
                    order.append(call_id)
            elif event in ("result", "chunk", "feedback"):
                unpaired.append({
                    "stem": stem, "event": event,
                    "run_id": row.get("run_id"),
                    "kind": row.get("kind"),
                    "ts": row.get("ts"),
                    "detail": {k: v for k, v in row.items()
                               if k not in ("event", "run_id", "ts", "chapter",
                                            "call_id", "kind")},
                })
        for call_id in order:
            req = requests.get(call_id)
            resp = responses.get(call_id)
            src = resp if resp is not None else req
            if src is None:
                continue
            usage = src.get("usage") if resp is not None else None
            prompt_tok, completion_tok = _usage_tokens(usage)
            reasoning = None
            if isinstance(usage, dict):
                details = usage.get("completion_tokens_details")
                if isinstance(details, dict) and isinstance(
                        details.get("reasoning_tokens"), int):
                    reasoning = details["reasoning_tokens"]
            body = src.get("response")
            parsed, shape = _parse_body(body)

            prompt_text = None
            if req is not None:
                raw_prompt = req.get("prompt")
                if isinstance(raw_prompt, str):
                    prompt_text = raw_prompt
                elif isinstance(raw_prompt, list):
                    prompt_text = "\n".join(str(x) for x in raw_prompt)

            calls.append({
                "call_id": call_id,
                "run_id": src.get("run_id"),
                "chapter": stem,
                "job": src.get("job"),
                "model": src.get("model"),
                "url": src.get("url"),
                "candidate": src.get("candidate"),
                "candidates": src.get("candidates"),
                "consensus_for": src.get("consensus_for"),
                "ts": src.get("ts"),
                "elapsed_s": src.get("elapsed_s"),
                "finish_reason": src.get("finish_reason"),
                "error": bool(src.get("error")),
                "prompt_tok": prompt_tok,
                "completion_tok": completion_tok,
                "reasoning_tok": reasoning,
                "params": req.get("params") if req is not None else None,
                "prompt": prompt_text,
                "body": body if isinstance(body, str) else None,
                "parsed": parsed,
                "shape": shape,
                "paired": req is not None and resp is not None,
                "source_lines": (_json_array_of_i_t(prompt_text)
                                 if shape == "lines" and prompt_text else None),
            })
    missing = [c["stem"] for c in chapters
               if c.get("call_source") not in ("index",)
               and c.get("call_source") != "none" and not calls]
    if missing:
        warnings.append(
            f"{len(missing)} chapter bucket(s) fall back to the newest run file "
            f"on disk because their latest index run has no body: "
            f"{', '.join(missing[:6])}")
    return calls, unpaired, warnings


def _calls(tier1: list[dict], stem: str) -> list[dict]:
    """Tier 1's per-call rows -- the FALLBACK for a chapter whose tier-2 bucket
    has none, i.e. one written while `log_llm` was false (tier-2 body events
    are the only thing that gate removes, and `result`/`chunk`/`feedback` do
    not carry call metadata)."""
    out: list[dict] = []
    for row in tier1:
        if row.get("event") != "llm_call" or row.get("chapter") not in (
                stem, f"{stem}.md"):
            continue
        prompt, completion = _usage_tokens(row.get("usage"))
        candidate = row.get("candidate")
        label = (f"{candidate}/{row.get('candidates')}"
                 if candidate is not None else
                 (f"for {row.get('consensus_for')}"
                  if row.get("consensus_for") else None))
        out.append({
            "run_id": row.get("run_id"), "job": row.get("job"),
            "model": row.get("model"), "candidate": label,
            "prompt_tok": prompt, "completion_tok": completion,
            "elapsed_s": row.get("elapsed_s"),
            "finish_reason": row.get("finish_reason"),
            "error": bool(row.get("error")),
        })
    return out


def _degraded(tier1: list[dict], stem: str) -> list[dict]:
    return [{"where": r.get("where"), "reason": r.get("reason")}
            for r in tier1
            if r.get("event") == "degraded" and r.get("chapter") in (
                stem, f"{stem}.md")]


def _project_runs(project_dir: Path) -> list[dict]:
    """Run-level lifecycle from the project bucket's index."""
    opens: dict[str, dict] = {}
    order: list[str] = []
    closes: dict[str, dict] = {}
    for row in _rows(project_dir / "logs" / "project" / INDEX_NAME):
        run_id = row.get("run_id")
        if not isinstance(run_id, str):
            continue
        if row.get("phase") == "open":
            if run_id not in opens:
                order.append(run_id)
            opens[run_id] = row
        elif row.get("phase") == "close":
            closes[run_id] = row
    return [{"run_id": r, "command": opens[r].get("command"),
             "started": opens[r].get("ts"),
             "closed": r in closes,
             "outcome": closes.get(r, {}).get("outcome")}
            for r in order]


def _epub(project_dir: Path) -> tuple[dict[str, dict], int]:
    """Latest build per chapter, keyed on the case-folded STEM, plus the
    number of trailing lines skipped because a build was mid-write.

    Two measured facts force this shape (PLAN-2026-10-08 §2.3a):
      * the header carries a filename whose casing changed with v010 -- the
        sample log spans `Chapter_0001.md` and `CHAPTER_0012.md` -- so the
        join must be case-insensitive or every pre-migration build is orphaned;
      * a background build child appends to the file concurrently
        (autobuild.py:135-144), so a half-written final block is skipped and
        counted, never parsed.
    """
    path = project_dir / "logs" / "epub-build.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, 0
    torn = 1 if text and not text.endswith("\n") else 0
    latest: dict[str, dict] = {}
    for entry in _epub_blocks(text.splitlines()):
        latest[entry["key"]] = entry
    return latest, torn


def _epub_blocks(lines: list[str]) -> Iterator[dict]:
    """One record per completed build block, in file order. A block with no
    [ok]/[warn]/[FAIL] marker never completes and is dropped by the caller."""
    current: dict | None = None
    for line in lines:
        head = _EPUB_HEAD_RE.match(line)
        if head:
            current = {"key": Path(head.group("file")).stem.casefold(),
                       "ts": head.group("ts"), "file": head.group("file"),
                       "ok": None, "detail": ""}
            continue
        if current is None:
            continue
        if current["ok"] is None:
            if "[ok]" in line:
                current["ok"] = True
            elif "[warn]" in line or "[FAIL]" in line:
                current["ok"] = False
                current["detail"] = _PATH_RE.sub("<path>", line.strip())[:200]
        if current["ok"] is not None:
            yield current
            current = None


def _meta(project_dir: Path) -> dict:
    """Masthead facts. Every field is optional -- a project with no
    novel_info.json still gets a page."""
    out: dict[str, Any] = {"title": None, "subtitle": None,
                           "source_lang": None, "target_lang": None}
    try:
        info = json.loads(
            (project_dir / "novel_info.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        info = None
    if isinstance(info, dict):
        out["title"] = info.get("title_translated") or info.get("title")
        out["subtitle"] = info.get("author")
        out["source_lang"] = info.get("source_lang")
        out["target_lang"] = info.get("target_lang")
    if not out["source_lang"] or not out["target_lang"]:
        raw = {}
        try:
            raw = json.loads(
                (project_dir / "config.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            raw = {}
        out["source_lang"] = out["source_lang"] or raw.get("source_lang")
        out["target_lang"] = out["target_lang"] or raw.get("target_lang")
    return out


def collect(project_dir: Path) -> dict:
    """Assemble every log into one plain dict. Read-only, never raises.

    The returned shape is the contract render() consumes; the two are written
    together and must change together.
    """
    project_dir = Path(project_dir)
    tier1 = _tier1(project_dir)
    epub, torn = _epub(project_dir)
    try:
        manifest = project.load_manifest(project_dir)
    except (OSError, ValueError, TypeError):
        manifest = []
    if not isinstance(manifest, list):
        manifest = []
    manifest_numbers = {int(e.get("number", 0)) for e in manifest
                        if isinstance(e, dict)}

    chapters: list[dict] = []
    for stem, bucket in _chapter_buckets(project_dir):
        runs = _index_runs(bucket)
        latest = runs[-1] if runs else None
        latest_id = latest["run_id"] if latest else None
        match = _NUM_RE.match(stem)
        number = int(match.group(1)) if match else None
        entry = {
            "stem": stem,
            "number": number,
            "runs": runs,
            "outcome": (latest or {}).get("outcome"),
            "closed": bool(runs) and all(r["closed"] for r in runs),
            "elapsed_s": (latest or {}).get("elapsed_s"),
            "calls": (latest or {}).get("calls"),
            "tokens": (latest or {}).get("tokens") or {},
            "attempts": (latest or {}).get("attempts"),
            "stages": (latest or {}).get("stages"),
            "stage_spans": _stage_spans(tier1, stem, latest_id),
            "gates": _gates(tier1, stem),
            "degraded": _degraded(tier1, stem),
            "run_file": (latest or {}).get("run_file", False),
            "in_manifest": number in manifest_numbers if number is not None
            else False,
            "epub": epub.get(stem.casefold()),
        }
        chapters.append(entry)

    calls, unpaired_events, call_warnings = _collect_calls(project_dir, chapters)
    used_tier1_calls = False
    by_chapter: dict[str, list[dict]] = {}
    for call in calls:
        by_chapter.setdefault(str(call["chapter"]), []).append(call)
    for chapter in chapters:
        own = by_chapter.get(chapter["stem"])
        if own:
            chapter["calls_detail"] = own
        else:
            tier1_rows = _calls(tier1, chapter["stem"])
            chapter["calls_detail"] = tier1_rows
            used_tier1_calls = used_tier1_calls or bool(tier1_rows)

    call_tokens_by_job: dict[str, int] = {}
    tokens_by_model: dict[str, dict[str, int]] = {}
    calls_by_job: dict[str, int] = {}
    call_tokens = 0
    call_count = 0
    rows_to_roll = calls or [c for ch in chapters
                             for c in ch["calls_detail"]]
    for call in rows_to_roll:
        call_count += 1
        job = str(call.get("job") or "?")
        calls_by_job[job] = calls_by_job.get(job, 0) + 1
        used = int(call["prompt_tok"]) + int(call["completion_tok"])
        call_tokens_by_job[job] = call_tokens_by_job.get(job, 0) + used
        call_tokens += used
        model = str(call.get("model") or "?")
        tokens_by_model.setdefault(model, {})
        tokens_by_model[model][job] = tokens_by_model[model].get(job, 0) + used

    index_tokens_by_job: dict[str, int] = {}
    index_calls = 0
    index_tokens = 0
    for chapter in chapters:
        for run in chapter["runs"]:
            if not run["closed"]:
                continue
            if isinstance(run["calls"], int):
                index_calls += run["calls"]
            for job, used in (run["tokens"] or {}).items():
                if not isinstance(used, int):
                    continue
                index_tokens_by_job[str(job)] = (
                    index_tokens_by_job.get(str(job), 0) + used)
                index_tokens += used

    from_calls = call_count > 0
    tokens_by_job = call_tokens_by_job if from_calls else index_tokens_by_job
    total_calls = call_count if from_calls else index_calls
    total_tokens = call_tokens if from_calls else index_tokens
    call_source = ("per-call rows (tier 2)" if calls
                   else "per-call rows (tier 1)" if used_tier1_calls
                   else "index close lines")

    run_end_tokens = 0
    for row in tier1:
        if row.get("event") != "run_end":
            continue
        tokens = row.get("tokens")
        if isinstance(tokens, dict):
            run_end_tokens += sum(v for v in tokens.values()
                                  if isinstance(v, int))
    chapterless_tokens = (max(0, run_end_tokens - index_tokens)
                          if tier1 else None)

    outcomes: dict[str, int] = {}
    unclosed: list[str] = []
    seen_unclosed: set[str] = set()
    for chapter in chapters:
        outcome = chapter["outcome"] or "unknown"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        for run in chapter["runs"]:
            if not run["closed"] and run["run_id"] not in seen_unclosed:
                seen_unclosed.add(run["run_id"])
                unclosed.append(f"{chapter['stem']} / {run['run_id']}")
    for run in _project_runs(project_dir):
        if not run["closed"] and run["run_id"] not in seen_unclosed:
            seen_unclosed.add(run["run_id"])
            unclosed.append(f"(no chapter) / {run['run_id']}")

    gaps: list[str] = []
    if not tier1:
        gaps.append(
            "orchestration tier unavailable - no logs/run-*.jsonl retained, so "
            "stage durations and gate verdicts are omitted. Per-chapter calls, "
            "the per-model split and every total below still come from the "
            "chapters' own tier-2 buckets and index.jsonl.")
        gaps.append(
            "spend OUTSIDE any chapter (profile / review / tn re-check) is "
            "recorded only in the orchestration tier and is NOT in any total "
            "on this page.")
    elif chapterless_tokens:
        gaps.append(
            f"{chapterless_tokens:,} tokens were spent outside any chapter "
            f"(profile / review / tn). Those invocations record nothing in a "
            f"chapter index, so they are excluded from the per-job bars and "
            f"shown separately under Health.")
    if not chapters:
        gaps.append("no chapter buckets under logs/chapters/ - nothing has "
                    "been translated in this project yet.")
    legacy = [c["stem"] for c in chapters if c["number"] is None]
    if legacy:
        gaps.append(f"{len(legacy)} bucket(s) predate the CHAPTER_NNNN naming "
                    f"and are listed last: {', '.join(legacy[:8])}")
    orphans = [c["stem"] for c in chapters if not c["in_manifest"]]
    if orphans:
        gaps.append(f"{len(orphans)} bucket(s) have no matching chapter in "
                    f"chapters.json: {', '.join(orphans[:8])}")
    if torn:
        gaps.append("epub-build.log ended mid-block (a build is running) - "
                    "that block was skipped.")
    if not (project_dir / "logs" / "epub-build.log").is_file():
        gaps.append("no epub-build.log - builds never ran, or "
                    "auto_build_epub is off.")

    elapsed = [c["elapsed_s"] for c in chapters
               if isinstance(c["elapsed_s"], (int, float))]
    return {
        "meta": {**_meta(project_dir),
                 "generated_at": datetime.now(timezone.utc)
                 .astimezone().isoformat(timespec="seconds"),
                 "project_dir": str(project_dir)},
        "chapters": chapters,
        "runs": _project_runs(project_dir),
        "tokens_by_job": tokens_by_job,
        "tokens_by_model": tokens_by_model,
        "calls_by_job": calls_by_job,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "token_source": call_source,
        "call_source": call_source,
        "elapsed_total": sum(elapsed) if elapsed else None,
        "elapsed_max": max(elapsed) if elapsed else None,
        "outcomes": outcomes,
        "unclosed": unclosed,
        "chapterless_tokens": chapterless_tokens,
        "gaps": gaps + call_warnings,
        "calls": calls,
        "unpaired_events": unpaired_events,
        "bodies_available": sum(1 for c in calls if c["body"]),
        "has_tier1": bool(tier1),
    }


def _asset(name: str) -> str:
    """One shipped asset under assets/dashboard/, verbatim.

    report.css and report.js are real files rather than string literals in this
    module, so a linter or formatter can see them; render() inlines them back,
    so logs/report.html stays ONE self-contained portable page -- no <link> and
    no <script src>, and a single report can still be archived or emailed.

    Read with the default newline handling on purpose: a checkout that lands
    CRLF still yields the LF text the page has always carried.
    """
    return (DASHBOARD_DIR / name).read_text(encoding="utf-8")


_STAGE_COLOURS = ("#5b7fb9", "#7b8fd4", "#6ba3b8", "#7fb89b",
                  "#a8c48a", "#c9a86b", "#b08aa0")


def _stamp(outcome: str | None, closed: bool) -> tuple[str, str]:
    """(css class, label) for one chapter's outcome."""
    if outcome is None and not closed:
        return "s-bad", "unclosed"
    if outcome is None:
        return "s-none", "unknown"
    if outcome in _OUTCOME_OK:
        return "s-ok", outcome
    if outcome in _OUTCOME_WARN:
        return "s-warn", outcome
    if outcome in _OUTCOME_BAD:
        return "s-bad", outcome
    return "s-none", outcome


def _stage_colours(stage_names: list[str]) -> dict[str, str]:
    return {name: _STAGE_COLOURS[i % len(_STAGE_COLOURS)]
            for i, name in enumerate(stage_names)}


def _figure(key: str, value: str, note: str = "") -> str:
    note_html = f'<p class="figure-n">{_esc(note)}</p>' if note else ""
    return (f'<div class="figure"><p class="figure-k">{_esc(key)}</p>'
            f'<p class="figure-v">{_esc(value)}</p>{note_html}</div>')


def _masthead(data: dict) -> str:
    meta = data.get("meta", {})
    chapters = data.get("chapters", [])
    title = meta.get("title") or "Translation log"
    lang = " -> ".join(x for x in (meta.get("source_lang"),
                                   meta.get("target_lang")) if x)
    eyebrow = "translation trace" + (f" / {lang}" if lang else "")
    sub_bits = []
    if meta.get("subtitle"):
        sub_bits.append(_esc(meta["subtitle"]))
    if chapters:
        sub_bits.append(_esc(f"{len(chapters)} chapter bucket(s)"))
    if data.get("unclosed"):
        sub_bits.append(_esc(f"{len(data['unclosed'])} unclosed run(s)"))
    figures = [
        _figure("runs", str(len(data.get("runs", [])))),
        _figure("chapters", str(len(chapters))),
        _figure("model calls", _fmt_count(data.get("total_calls")),
                data.get("call_source", "")),
        _figure("tokens", _fmt_count(data.get("total_tokens")),
                data.get("token_source", "")),
        _figure("wall clock", _fmt_duration(data.get("elapsed_total")),
                "sum of latest run per chapter"),
    ]
    return (f'<header class="mast"><p class="mast-eyebrow">{_esc(eyebrow)}</p>'
            f'<h1 class="mast-title">{_esc(title)}</h1>'
            f'<p class="mast-sub">{" &middot; ".join(sub_bits)}</p>'
            f'<div class="mast-figures">{"".join(figures)}</div></header>')


def _ledger(data: dict) -> str:
    """THE page. One proportional bar per chapter: stacked by stage, tinted by
    outcome, so the column of bars reads as a batch timeline at a glance."""
    chapters = data.get("chapters", [])
    if not chapters:
        return ('<p class="empty">No chapter buckets under '
                '<code>logs/chapters/</code> yet.</p>')
    scale = data.get("elapsed_max")
    names: list[str] = []
    for chapter in chapters:
        for span in chapter["stage_spans"]:
            if span["stage"] not in names:
                names.append(span["stage"])
    colours = _stage_colours(names)

    rows: list[str] = []
    for chapter in chapters:
        cls, label = _stamp(chapter.get("outcome"),
                            chapter.get("closed", False))
        stem = chapter["stem"]
        earlier = len(chapter.get("runs") or []) - 1
        prior = (f'<span class="stamp s-none" title="earlier runs, newest '
                 f'first">+{earlier} earlier</span>') if earlier > 0 else ""
        elapsed = chapter.get("elapsed_s")
        segments: list[str] = []
        accounted = 0.0
        for span in chapter["stage_spans"]:
            seconds = _num(span.get("elapsed_s"))
            if seconds is None or not scale:
                continue
            width = min(100.0, seconds / scale * 100.0)
            accounted += seconds
            segments.append(
                f'<i class="bar-seg" style="width:{width:.3f}%;'
                f'background:{colours.get(span["stage"], "#667089")}" '
                f'title="{_esc(span["stage"])}"></i>')
        if not segments and isinstance(elapsed, (int, float)) and scale:
            width = min(100.0, elapsed / scale * 100.0)
            segments.append(f'<i class="bar-remainder" style="width:{width:.3f}%"></i>')
            accounted = float(elapsed)
        elif segments and isinstance(elapsed, (int, float)):
            rest = max(0.0, float(elapsed) - accounted)
            if scale and rest > 0:
                segments.append('<i class="bar-remainder" '
                                f'style="width:{min(100.0, rest / scale * 100.0):.3f}%"></i>')
        bar = "".join(segments) or '<i class="bar-empty"></i>'

        tip = _tip_text(chapter)
        flags = []
        if not chapter.get("in_manifest"):
            flags.append("not in manifest")
        if not chapter.get("run_file"):
            flags.append("run file pruned")
        if chapter.get("degraded"):
            flags.append(f"{len(chapter['degraded'])} degraded")
        tip_line = tip + ("\n" + ", ".join(flags) if flags else "")

        rows.append(
            f'<details class="lrow" data-outcome="{_esc(label)}">'
            f'<summary>'
            f'<span class="lid"><span class="lid-n">{_esc(stem)}</span>'
            f'<span class="stamp {cls}">{_esc(label)}</span>{prior}</span>'
            f'<span class="tipwrap"><span class="bar">{bar}</span>'
            f'<span class="tip">{_esc(tip_line)}</span></span>'
            f'<span class="lfig"><span class="lfig-v">'
            f'{_esc(_fmt_duration(elapsed))}</span>'
            f'<span class="lfig-k">wall</span></span>'
            f'<span class="lfig"><span class="lfig-v">'
            f'{_esc(_fmt_count(chapter.get("calls")))}</span>'
            f'<span class="lfig-k">calls</span></span>'
            f'<span class="lfig"><span class="lfig-v">'
            f'{_esc(_fmt_count(_chapter_tokens(chapter)))}</span>'
            f'<span class="lfig-k">tokens</span></span>'
            f'</summary>'
            f'{_ledger_body(chapter)}</details>')
    legend = ""
    if names:
        legend = ('<div class="legend">'
                  + "".join(f'<span class="legend-i">'
                            f'<i style="background:{colours[name]}"></i>'
                            f'{_esc(name)}</span>' for name in names)
                  + '<span class="legend-i"><i class="rem"></i>unclaimed</span>'
                  '</div>')
    return (f'<div class="ledger">{"".join(rows)}</div>'
            f'{legend}{_chips(data)}')


def _chapter_tokens(chapter: dict) -> int | None:
    total = chapter.get("tokens")
    if not isinstance(total, dict) or not total:
        return None
    used = [v for v in total.values() if isinstance(v, int)]
    return sum(used) if used else None


def _tip_text(chapter: dict) -> str:
    lines = [f"{chapter['stem']}"]
    if not chapter["stage_spans"]:
        lines.append("stage detail unavailable (no tier-1 run retained)")
    else:
        for span in chapter["stage_spans"]:
            mark = "" if span["ended"] else "  (did not end)"
            lines.append(f"  {span['stage']:<16}"
                         f"{_fmt_duration(span['elapsed_s'])}{mark}")
    return "\n".join(lines)


def _ledger_body(chapter: dict) -> str:
    parts: list[str] = []
    runs = chapter.get("runs") or []
    if runs:
        rows = "".join(
            f'<li><b>{_esc(r["started"] or r["run_id"])}</b>'
            f'<span>{_esc(r["command"] or "-")} &middot; '
            f'{_esc(_stamp(r.get("outcome"), r["closed"])[1])} &middot; '
            f'{_esc(_fmt_duration(r.get("elapsed_s")))} &middot; '
            f'{_esc(_fmt_count(r.get("calls")))} calls &middot; '
            f'{_esc(_fmt_count(_run_tokens(r)))} tok'
            f'{"" if r["closed"] else " &middot; DID NOT CLOSE"}</span></li>'
            for r in reversed(runs))
        parts.append(f'<h4>Runs ({len(runs)})</h4><ul class="kv">{rows}</ul>')

    gates = chapter.get("gates") or []
    if gates:
        rows = "".join(
            f'<li><b>{_esc(g.get("stage") or "-")}</b>'
            f'<span>{_esc(g.get("verdict") or "-")}'
            + (" &middot; " + _esc("; ".join(g["reasons"])) if g["reasons"] else "")
            + "</span></li>" for g in gates)
        parts.append(f'<h4>Gate verdicts ({len(gates)})</h4>'
                     f'<ul class="kv">{rows}</ul>')

    degraded = chapter.get("degraded") or []
    if degraded:
        rows = "".join(
            f'<li><b>{_esc(d.get("where") or "-")}</b>'
            f'<span>{_esc(d.get("reason") or "")}</span></li>'
            for d in degraded)
        parts.append(f'<h4>Degradations ({len(degraded)})</h4>'
                     f'<ul class="kv">{rows}</ul>')

    calls = chapter.get("calls_detail") or []
    if calls:
        rows = "".join(
            f'<li><b>{_esc(c.get("job") or "-")}</b>'
            f'<span>{_esc(c.get("model") or "-")}'
            f'{" / " + _esc(c["candidate"]) if c.get("candidate") else ""} &middot; '
            f'{_esc(_fmt_count(c["prompt_tok"]))}+{_esc(_fmt_count(c["completion_tok"]))}'
            f' tok &middot; {_esc(_fmt_duration(c.get("elapsed_s")))}'
            f'{" &middot; ERROR" if c.get("error") else ""}</span></li>'
            for c in calls)
        parts.append(f'<h4>Model calls ({len(calls)})</h4>'
                     f'<ul class="kv">{rows}</ul>')
    else:
        parts.append('<h4>Model calls</h4><ul class="kv"><li><b>calls</b>'
                     '<span>not recorded &mdash; no per-call log retained for '
                     'this chapter (log_llm off when it ran, or its retention '
                     'window has since emptied every bucket)</span></li></ul>')

    epub = chapter.get("epub")
    if epub:
        state = "ok" if epub.get("ok") else "failed"
        extra = f' ({epub["builds"]} builds)' if epub.get("builds", 1) > 1 else ""
        parts.append(
            f'<h4>EPUB</h4><ul class="kv"><li><b>{_esc(state)}{extra}</b>'
            f'<span>{_esc(epub.get("ts") or "")}'
            f'{" &middot; " + _esc(epub["detail"]) if epub.get("detail") else ""}'
            f'</span></li></ul>')
    return f'<div class="lbody">{"".join(parts)}</div>'


def _run_tokens(run: dict) -> int | None:
    tokens = run.get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        return None
    used = [v for v in tokens.values() if isinstance(v, int)]
    return sum(used) if used else None


def _chips(data: dict) -> str:
    counts: dict[str, int] = {}
    for chapter in data.get("chapters", []):
        label = _stamp(chapter.get("outcome"),
                       chapter.get("closed", False))[1]
        counts[label] = counts.get(label, 0) + 1
    if len(counts) < 2:
        return ""
    buttons = ['<button class="chip" type="button" data-filter="all" '
               'aria-pressed="true">all</button>']
    for label in sorted(counts):
        buttons.append(
            f'<button class="chip" type="button" '
            f'data-filter="{_esc(label)}" aria-pressed="false">'
            f'{_esc(label)} ({counts[label]})</button>')
    return ('<div class="chips" role="group" aria-label="Filter chapters by '
            f'outcome">{"".join(buttons)}</div>')


def _token_table(data: dict) -> str:
    jobs = data.get("tokens_by_job") or {}
    if not jobs:
        return ('<p class="empty">No tokens recorded. The orchestration tier '
                'holds per-call usage; when retention has removed it, per-job '
                'totals come from each chapter&#39;s index close line.</p>')
    top = max(jobs.values()) or 1
    rows = []
    for job, used in sorted(jobs.items(), key=lambda kv: -kv[1]):
        pct = used / top * 100
        rows.append(
            f'<tr><td>{_esc(job)}</td>'
            f'<td class="bar-cell"><span class="mbar">'
            f'<i style="width:{pct:.2f}%"></i></span></td>'
            f'<td class="v">{_esc(_fmt_count(used))}</td></tr>')
    return ('<table class="tbl"><thead><tr><th>job</th><th>share</th>'
            f'<th style="text-align:right">tokens</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _model_table(data: dict) -> str:
    models = data.get("tokens_by_model") or {}
    if not models:
        return ('<p class="empty">No per-model breakdown available &mdash; it '
                'is derived from the orchestration tier&#39;s per-call rows, '
                'which retention may have removed.</p>')
    rows = []
    for model in sorted(models):
        by_job = models[model]
        total = sum(by_job.values())
        detail = ", ".join(f"{j} {_fmt_count(v)}"
                           for j, v in sorted(by_job.items(), key=lambda kv: -kv[1]))
        rows.append(f'<tr><td>{_esc(model)}</td><td>{_esc(detail)}</td>'
                    f'<td class="v">{_esc(_fmt_count(total))}</td></tr>')
    return ('<table class="tbl"><thead><tr><th>model</th><th>by job</th>'
            f'<th style="text-align:right">tokens</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _health(data: dict) -> str:
    outcomes = data.get("outcomes") or {}
    unclosed = data.get("unclosed") or []
    parts: list[str] = []
    chapterless = data.get("chapterless_tokens")
    if chapterless:
        parts.append(
            '<p class="sec-note" style="margin:0 0 1rem">Spend outside any '
            f'chapter: <strong style="color:var(--ink)">'
            f'{_esc(_fmt_count(chapterless))}</strong> tokens from invocations '
            'that record no chapter index line (profile, review, tn '
            're-check). Measured as the orchestration tier&#39;s run_end total '
            'minus the chapter close lines.</p>')
    elif chapterless is None and data.get("chapters"):
        parts.append(
            '<p class="sec-note" style="margin:0 0 1rem">Spend outside any '
            'chapter is <strong style="color:var(--ink)">unknown</strong>: it '
            'is recorded only in the orchestration tier, which is not '
            'retained here.</p>')
    if unclosed:
        rows = "".join(f'<li><b>{_esc(u.split(" / ")[-1])}</b>'
                       f'<span>{_esc(u.split(" / ")[0])}</span></li>'
                       for u in unclosed)
        parts.append(
            '<h4 style="font-family:var(--mono);font-size:.75rem;'
            'letter-spacing:.12em;text-transform:uppercase;color:var(--cinnabar);'
            'margin:0 0 .5rem">'
            f'Unclosed runs ({len(unclosed)}) &mdash; these invocations died '
            'mid-flight</h4>'
            f'<ul class="kv">{rows}</ul>')
    if outcomes:
        rows = "".join(
            f'<tr><td>{_esc(k)}</td>'
            f'<td class="v">{_esc(_fmt_count(v))}</td></tr>'
            for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1]))
        parts.append('<table class="tbl"><thead><tr><th>outcome</th>'
                     '<th style="text-align:right">chapters</th></tr></thead>'
                     f'<tbody>{"".join(rows)}</tbody></table>')
    if not parts:
        parts.append('<p class="empty">Nothing recorded yet.</p>')
    return "".join(parts)


def _runs_table(data: dict) -> str:
    runs = data.get("runs") or []
    if not runs:
        return ('<p class="empty">No run lifecycle recorded &mdash; the '
                'project bucket has no <code>index.jsonl</code> open lines.</p>')
    rows = []
    for run in runs:
        cls, label = _stamp(run.get("outcome"), run["closed"])
        rows.append(
            f'<tr><td>{_esc(run["started"] or "-")}</td>'
            f'<td>{_esc(run.get("command") or "-")}</td>'
            f'<td class="stamp {cls}">{_esc(label)}</td>'
            f'<td>{_esc(run["run_id"])}</td></tr>')
    return ('<table class="tbl"><thead><tr><th>started</th><th>command</th>'
            '<th>outcome</th><th>run id</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _encode_blob(records: list[dict]) -> str:
    """JSON for the <script type="application/json"> body store.

    ⚠ DO NOT html.escape this. <script> is a *rawtext* element: the HTML parser
    never decodes character references inside it, so textContent hands
    &quot; back LITERALLY and JSON.parse fails at position 1. Every response
    body contains a quote, so that breaks 116/116 records -- the whole section.

    The only character that can terminate a script element is '<', and \\u003c
    is a valid JSON string escape, so replacing it is lossless: a body holding
    a literal close tag round-trips back to that exact string. Verified in
    Chrome against both encodings (probe-artifacts/blob-escape-browser-test3.html).

    Note that Python's html.unescape is NOT a faithful stand-in for what the
    browser does here -- simulating with it passes green for the broken
    encoding, which is exactly how this bug survives review.
    """
    return json.dumps(records, ensure_ascii=False,
                      default=str).replace("<", "\\u003c")


def key_task(call: dict) -> Any:
    """The ARBITRATED task a call belongs to.

    A candidate call carries its own task in ``job``; a consensus call carries
    the task it arbitrates in ``consensus_for`` and reports ``job="consensus"``.
    Grouping on ``job`` alone therefore splits one fan-out in two, and the
    consensus column silently drops out of the side-by-side matrix. A consensus
    call with no ``consensus_for`` is not part of any group but its own.
    """
    return call.get("consensus_for") or call.get("job")


def _ledger_blob(data: dict) -> list[dict]:
    """The per-call records the browser needs to render a body on click.

    ``parsed`` ships instead of ``body`` when the body parsed -- it is what the
    shape renderers actually read, and shipping both would double ~0.4 MB for
    nothing. The raw ``body`` is kept only when parsing FAILED, which is the one
    case that needs it.

    ``source_lines`` is identical across every sibling of one fan-out group --
    the same source array is recovered from each candidate's prompt -- so it is
    stored ONCE per group and the browser reads it off the first sibling.
    Measured, that alone was ~2.6 MB of the 7 MB page.

    The group key is the ARBITRATED task, ``consensus_for or job``. Keying on
    ``job`` alone would put a translator's candidates under "translator" and its
    consensus under "consensus", so the consensus column would silently drop out
    of the side-by-side matrix it belongs in.
    """
    out: list[dict] = []
    seen_source: set[tuple[str, str]] = set()
    for call in data.get("calls") or []:
        record = {
            "i": call["call_id"],
            "chapter": call["chapter"],
            "job": call.get("job"),
            "model": call.get("model"),
            "candidate": call.get("candidate"),
            "candidates": call.get("candidates"),
            "consensus_for": call.get("consensus_for"),
            "task": call.get("consensus_for") or call.get("job"),
            "shape": call.get("shape"),
            "paired": call.get("paired"),
            "params": call.get("params"),
            "prompt": call.get("prompt"),
        }
        key = (str(call["chapter"]), str(key_task(call)))
        if call.get("source_lines") and key not in seen_source:
            seen_source.add(key)
            record["source_lines"] = call["source_lines"]
        elif call.get("source_lines"):
            record["source_ref"] = True
        if call.get("parsed") is not None:
            record["parsed"] = call["parsed"]
        else:
            record["body"] = call.get("body")
        out.append(record)
    return out


def _call_ledger(data: dict) -> str:
    """Every call of every chapter's latest run, as a searchable index.

    The index is always complete and always uncapped -- ~200 B per row, so the
    whole table is tens of KB regardless of how large the bodies are. Bodies
    are NOT in these rows: they live in the JSON blob and are built on click,
    which is why the page stays responsive at any log volume.
    """
    calls = data.get("calls") or []
    parts: list[str] = []
    fallback = [c["stem"] for c in data.get("chapters") or []
                if c.get("call_source") == "newest-file (index run has no body)"]
    if fallback:
        parts.append(
            f'<p class="sec-note">{len(fallback)} chapter(s) have no body for '
            'their latest run in <code>index.jsonl</code>, so the ledger below '
            'falls back to the newest run file on disk for them: '
            f'{_esc(", ".join(fallback[:8]))}.</p>')

    if not calls:
        tier1_rows = sum(len(c.get("calls_detail") or [])
                         for c in data.get("chapters") or [])
        if tier1_rows:
            parts.append(
                f'<p class="empty">{tier1_rows} call(s) exist but only as '
                'orchestration-tier summaries &mdash; those carry no prompt or '
                'response body, so there is nothing to expand here. Per-call '
                'usage is still listed on each chapter row above.</p>')
        else:
            parts.append(
                '<p class="empty">No per-call records retained. Chapters built '
                'while <code>log_llm</code> was off keep only their '
                '<code>index.jsonl</code> totals, which appear above.</p>')
        return "".join(parts)

    rows: list[str] = []
    for idx, call in enumerate(calls):
        job = str(call.get("job") or "?")
        model = str(call.get("model") or "?")
        tag = ""
        if isinstance(call.get("candidate"), int):
            tag = f'{call["candidate"]}/{call.get("candidates")}'
        elif call.get("consensus_for"):
            tag = f'consensus → {call["consensus_for"]}'
        reasoning = call.get("reasoning_tok")
        reasoning_cell = (f'{_esc(_fmt_count(reasoning))}'
                          if isinstance(reasoning, int)
                          else '<span class="na">&mdash;</span>')
        total = int(call["prompt_tok"]) + int(call["completion_tok"])
        flags = []
        if not call["paired"]:
            flags.append("unpaired")
        if call.get("error"):
            flags.append("error")
        flag_html = "".join(
            f'<span class="flag flag-bad">{_esc(f)}</span>' for f in flags)
        shape = str(call.get("shape") or "text")
        endpoint = str(call.get("url") or "")
        for suffix in ("/chat/completions", "/completions"):
            if endpoint.endswith(suffix):
                endpoint = endpoint[:-len(suffix)]
                break
        rows.append(
            f'<tr class="callrow" data-idx="{idx}" data-job="{_esc(job)}" '
            f'data-shape="{_esc(shape)}" data-chapter="{_esc(call["chapter"])}" '
            f'data-model="{_esc(model)}" data-endpoint="{_esc(endpoint)}" '
            f'data-cid="{_esc(call["call_id"])}" '
            f'tabindex="0">'
            f'<td><button class="callid" type="button" data-open="{idx}" '
            f'aria-expanded="false" '
            f'aria-label="Toggle details for call {call["call_id"]}">'
            f'{_esc(call["call_id"])}</button>{flag_html}</td>'
            f'<td>{_esc(call["chapter"])}</td>'
            f'<td>{_esc(job)}<span class="sub">{_esc(tag)}</span></td>'
            f'<td>{_esc(model)}</td>'
            f'<td class="ep">{_esc(endpoint)}</td>'
            f'<td class="v">{_esc(shape)}</td>'
            f'<td class="v">{_esc(_fmt_count(total))}</td>'
            f'<td class="v">{reasoning_cell}</td>'
            f'<td class="v">{_esc(_fmt_duration(call.get("elapsed_s")))}</td>'
            f'</tr>')
        rows.append(f'<tr id="rowhost{idx}" class="bodyhost" hidden>'
                    f'<td colspan="9"></td></tr>')

    parts.append(
        '<div class="ledger-bar"><input id="callq" type="search" '
        'placeholder="filter by chapter, job, model, endpoint or call id" '
        'aria-label="Filter calls">'
        f'<span class="count">{len(calls)} calls</span>'
        '<button class="chip" type="button" id="expandall" '
        'aria-pressed="false">show all bodies</button></div>')
    parts.append(
        '<table class="tbl ledger"><thead><tr><th>call id</th><th>chapter</th>'
        '<th>job</th><th>model</th><th>endpoint</th><th>shape</th>'
        '<th style="text-align:right">tokens</th>'
        '<th style="text-align:right">reasoning</th>'
        '<th style="text-align:right">elapsed</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>')
    parts.append(_unpaired_events(data))
    return "".join(parts)


def _unpaired_events(data: dict) -> str:
    """result / chunk rows -- real events, but not calls (no call_id)."""
    events = data.get("unpaired_events") or []
    if not events:
        return ""
    rows = []
    for event in events:
        detail = event.get("detail") or {}
        summary = ", ".join(f"{_esc(k)} {_esc(_fmt_count(v))}"
                            if isinstance(v, (int, float))
                            else f"{_esc(k)} {_esc(str(v))[:60]}"
                            for k, v in sorted(detail.items())
                            if v not in (None, [], {}))
        rows.append(
            f'<tr><td>{_esc(event.get("stem") or "-")}</td>'
            f'<td>{_esc(str(event.get("kind") or event.get("event")))}</td>'
            f'<td>{_esc((event.get("ts") or "-")[:19])}</td>'
            f'<td class="det">{summary or "&mdash;"}</td></tr>')
    return (f'<h3 class="sub-h">Stage events ({len(events)}) &mdash; recorded '
            'without a call id, so they are not part of the call ledger</h3>'
            '<table class="tbl"><thead><tr><th>chapter</th><th>kind</th>'
            '<th>when</th><th>detail</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _section(title: str, note: str, body: str) -> str:
    return (f'<section class="sec"><div class="sec-h"><h2 class="sec-t">'
            f'{_esc(title)}</h2><p class="sec-note">{_esc(note)}</p></div>'
            f'<div class="sec-body">{body}</div></section>')


def render(data: dict) -> str:
    """The finished page. No network requests, and no model text outside the
    one JSON blob.

    The stylesheet and the ledger script are read from assets/dashboard/ and
    INLINED here, which is what keeps the page self-contained: an external
    <link> or <script src> would make the artifact depend on a directory that
    travels with it, and an archived or emailed report would render unstyled.

    The blob IS embedded model data, deliberately: on-demand rendering needs it,
    and a local fetch() would need a server. It is emitted as the f-string
    EXPRESSION ``{_bodies_blob}`` -- never pasted into the template text --
    because every body contains { and }, and a ValueError here would be caught
    by write_dashboard's blanket except and silently produce no page at all.
    The browser only ever reads it through JSON.parse, and the encoder
    neutralises the single character that could break out of a script element.
    """
    gaps = data.get("gaps") or []
    gap_html = ("<ul class=\"gaps\">" + "".join(
        f"<li>{_esc(g)}</li>" for g in gaps) + "</ul>") if gaps else ""
    css = _asset("report.css")
    ledger_js = _asset("report.js")
    _bodies_blob = _encode_blob(_ledger_blob(data))
    ledger_note = (
        "One bar per chapter, scaled to the slowest. Segments are pipeline "
        "stages; the pale remainder is time inside the chapter that no stage "
        "claims. Open a row for its runs, gates, calls and epub state."
        if data.get("has_tier1") else
        "One bar per chapter, scaled to the slowest. Segmenting a bar by "
        "stage needs the orchestration tier, and none is retained here, so "
        "each bar is total wall-clock only -- the per-call, token and epub "
        "detail all survive. Open a row for its runs.")
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{_esc(data.get("meta", {}).get("title") or "Translation log")}</title>
<style>{css}</style>
</head><body><div class="wrap">
{_masthead(data)}
{gap_html}
{_section("Chapter ledger", ledger_note, _ledger(data))}
{_section("Where the tokens went", "Rolled up from per-call usage rows; the "
           "source is named under the masthead total.", _token_table(data))}
{_section("Model roster", "Which model served which job, and how much of the "
           "token spend each carried.", _model_table(data))}
{_section("Health", "Outcome distribution and the crash signal.", _health(data))}
{_section("Invocations", "Run lifecycle from the project bucket's index.",
           _runs_table(data))}
{_section("Call ledger",
           "Every model call of every chapter's latest run. Bodies are parsed "
           "on click; nothing is capped and nothing is sampled.", _call_ledger(data))}
<div id="callbodies"></div>
<footer>
Generated {_esc(data.get("meta", {}).get("generated_at", ""))} from
<code>logs/</code> &mdash; a derived view, safe to delete; regenerate with
<code>translate logs --html</code>.
</footer>
</div>
<script type="application/json" id="dl-bodies">{_bodies_blob}</script>
<script>{ledger_js}</script>
<script>
document.querySelectorAll('.chip[data-filter]').forEach(function(b){{
  b.addEventListener('click',function(){{
    var want=b.getAttribute('data-filter');
    document.querySelectorAll('.chip[data-filter]').forEach(function(o){{
      o.setAttribute('aria-pressed',String(o===b));
    }});
    // Unqualified on purpose. A chapter row is a <details>, so a tag-qualified
    // selector matches NOTHING: the filter then silently does nothing while
    // still flipping aria-pressed, which reads as "the button is broken"
    // rather than "the selector is wrong". Test 22 pins this.
    document.querySelectorAll('.lrow').forEach(function(r){{
      r.hidden = want!=='all' && r.getAttribute('data-outcome')!==want;
    }});
  }});
}});
</script>
</body></html>
"""


def _write_page(path: Path, text: str) -> None:
    """Atomic replace with a SHORT retry, on purpose diverging from
    project.atomic_write_text.

    atomic_write_text retries os.replace seven times with 0.1s..3.2s backoff
    (~6.3s total, project.py:166-173) because the pipeline's real destinations
    are chapter files an epub child may be reading. This page is different: the
    dashboard is a derived artifact, and the common cause of a held destination
    is someone VIEWING report.html in a browser. Blocking a finished translate
    run for six seconds and then printing [warn] is the wrong trade, so this
    path gets three short attempts (~0.3s) and gives up quietly.

    Still atomic and still non-destructive: the temp file is only replaced into
    place, so a failure leaves the previous good page exactly as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json(path: Path, data: dict) -> None:
    """collect()'s output as a diffable, testable artifact.

    logs/report.json is the DATA contract behind report.html: the same dict,
    keyed the same way, so a diff between two runs shows what the page would
    have changed about. default=str covers the handful of values that are not
    JSON-native (a Path, a datetime) rather than losing the whole report to a
    TypeError; newline="\\n" keeps the bytes identical on every platform.
    """
    project.atomic_write_text(
        path, json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        newline="\n")


def write_dashboard(project_dir: Path) -> Path | None:
    """Write logs/report.html AND logs/report.json. Returns the page path, or
    None if nothing could be written. Never raises -- a dashboard bug can never
    take a run down or destroy the page it meant to refresh.

    Both artifacts are derived from ONE collect(), so they can never disagree.
    The JSON is written AFTER the page and in its own try: the page is what the
    caller reports on, and a report.json that could not be written must not turn
    a good page into a "[warn] not written".

    There is no io/--html-io argument any more. Embedding every body of every
    chapter's latest run measures ~36 ms against a 981 s chapter, so a second
    mode bought nothing; and the byte cap it replaced deleted 84 of 131 bodies,
    oldest run first, which is what made the page look like it was missing logs.
    """
    try:
        data = collect(project_dir)
        path = Path(project_dir) / "logs" / REPORT_NAME
        _write_page(path, render(data))
    except Exception as exc:
        print(f"[warn] html dashboard not written: {type(exc).__name__}: {exc}")
        return None
    try:
        _write_json(path.with_name(REPORT_JSON_NAME), data)
    except Exception as exc:
        print(f"[warn] html dashboard data not written: "
              f"{type(exc).__name__}: {exc}")
    return path


def refresh(project_dir: Path) -> None:
    """The automatic writer _run_end calls. Silent on success, one [warn] on
    failure -- same contract as write_run_report."""
    write_dashboard(project_dir)
