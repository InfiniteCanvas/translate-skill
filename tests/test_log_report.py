"""Tests for lib/logreport.py and the `translate logs` reader.

The report is the human-readable face of a chapter's trace logs, and it has
two readers with different contracts:

  write_run_report (chapter_end)  metadata only, bounded to the current
    invocation's tier-1 file
  write_full_report (`logs --report`, and --report --io)  every retained run,
    and with io=True one <details> block per call that actually carried a body

What matters here is that it never lies and never raises:
- model output is HTML-escaped, so a reply containing </details> or a fenced
  block cannot corrupt the very structure the report asserts on
- token totals are summed from the llm_call lines, never invented
- a truncated final line (a crash mid-append) is skipped, not raised
- every writer path returns quietly instead of propagating

Hermetic sandboxes under tempfile.TemporaryDirectory(). Self-contained
PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_log_report.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "requests>=2.31"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import logger, logreport

PASSED = 0
FAILED: list[str] = []

RID = "20260101-000000-translate-4242"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def reset() -> None:
    logger._run_path = None


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def seed(root: Path, stem: str = "CHAPTER_0001",
         chapter_field: str | None = None, with_bodies: bool = True,
         truncated: bool = False) -> Path:
    """One complete run: a tier-1 timeline, the chapter's tier-2 model IO,
    and both index files."""
    chapter = chapter_field if chapter_field is not None else f"{stem}.md"
    tier1 = root / "logs" / f"run-{RID}.jsonl"
    lines = [
        {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID,
         "event": "run_start", "command": "translate"},
        {"ts": "2026-01-01T00:00:01.000+00:00", "run_id": RID,
         "event": "chapter_start", "chapter": chapter, "number": 1,
         "lines": 3, "resume_stage": None, "resume_chunks": 0, "force": False},
        {"ts": "2026-01-01T00:00:02.000+00:00", "run_id": RID,
         "event": "stage", "chapter": chapter, "stage": "TRAN|SLATE",
         "phase": "begin", "attempt": 1},
        {"ts": "2026-01-01T00:00:03.000+00:00", "run_id": RID,
         "event": "llm_call", "chapter": chapter, "job": "translator",
         "model": "m1", "usage": {"prompt_tokens": 100,
                                  "completion_tokens": 20,
                                  "total_tokens": 120},
         "finish_reason": "stop", "elapsed_s": 1.5, "error": None},
        {"ts": "2026-01-01T00:00:04.000+00:00", "run_id": RID,
         "event": "llm_call", "chapter": chapter, "job": "reviewer",
         "model": "m2", "candidate": 1, "candidates": 2,
         "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                   "total_tokens": 6},
         "finish_reason": "stop", "elapsed_s": 0.5, "error": None},
        {"ts": "2026-01-01T00:00:05.000+00:00", "run_id": RID,
         "event": "llm_call", "chapter": chapter, "job": "translator",
         "model": "m1", "usage": None, "finish_reason": None,
         "elapsed_s": 0.0, "error": "boom"},
        {"ts": "2026-01-01T00:00:06.000+00:00", "run_id": RID,
         "event": "stage", "chapter": chapter, "stage": "TRAN|SLATE",
         "phase": "end", "attempt": 1, "elapsed_s": 4.0},
        {"ts": "2026-01-01T00:00:07.000+00:00", "run_id": RID,
         "event": "gate", "chapter": chapter, "stage": "VALIDATE",
         "verdict": "fail", "reasons": ["a", "b", "</details>", "|injected|"]},
        {"ts": "2026-01-01T00:00:08.000+00:00", "run_id": RID,
         "event": "chapter_end", "chapter": chapter, "number": 1,
         "outcome": "needs-review", "attempts": 2,
         "stages_run": ["TRANSLATE", "VALIDATE"], "calls": 3,
         "tokens": {"translator": 120}, "elapsed_s": 7.0},
    ]
    write_lf(tier1, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines)
             + ('{"ts": "2026-01-01T00:00:09.000+00:00", "event": "ch'
                if truncated else ""))
    tier2 = root / "logs" / "chapters" / stem / f"run-{RID}.jsonl"
    body_lines = []
    if with_bodies:
        body_lines = [
            {"ts": "2026-01-01T00:00:02.500+00:00", "run_id": RID,
             "event": "llm_request", "chapter": chapter, "job": "translator",
             "call_id": "c1", "model": "m1", "prompt": "P <b>one</b>"},
            {"ts": "2026-01-01T00:00:03.000+00:00", "run_id": RID,
             "event": "llm_response", "chapter": chapter, "job": "translator",
             "call_id": "c1", "model": "m1",
             "response": "R1 </details>\n```\nfenced\n```", "prompt": "P"},
            {"ts": "2026-01-01T00:00:05.500+00:00", "run_id": RID,
             "event": "llm_request", "chapter": chapter, "job": "reviewer",
             "call_id": "c2", "model": "m2", "prompt": "P2"},
            {"ts": "2026-01-01T00:00:05.900+00:00", "run_id": RID,
             "event": "llm_response", "chapter": chapter, "job": "reviewer",
             "call_id": "c2", "model": "m2", "response": "R2",
             "prompt_chars": 2},
        ]
    write_lf(tier2, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                             for r in body_lines))
    write_lf(root / "logs" / "chapters" / stem / "index.jsonl",
             json.dumps({"ts": "2026-01-01T00:00:01.000+00:00", "run_id": RID,
                         "phase": "open", "command": "translate",
                         "file": chapter, "number": 1}) + "\n"
             + json.dumps({"ts": "2026-01-01T00:00:08.000+00:00",
                           "run_id": RID, "phase": "close",
                           "outcome": "needs-review"}) + "\n")
    write_lf(root / "logs" / "project" / "index.jsonl",
             json.dumps({"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID,
                         "phase": "open", "command": "translate"}) + "\n")
    return tier1


def report_of(root: Path, stem: str = "CHAPTER_0001") -> str:
    path = root / "logs" / "chapters" / stem / "report.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def case_1_run_report() -> None:
    """chapter_end's report: header, stage timeline, gate verdicts, calls --
    and NO model bodies."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root)
        reset()
        logger.log_event(root, {"event": "chapter_end",
                                "chapter": "CHAPTER_0001.md", "number": 1,
                                "outcome": "needs-review", "attempts": 2,
                                "stages_run": ["TRANSLATE"], "calls": 1,
                                "tokens": {}, "elapsed_s": 1.0})
        logreport.write_run_report(root, "CHAPTER_0001.md", 1, RID)
        text = report_of(root)
        check("1a report: the header names the chapter and the outcome",
              "- chapter: CHAPTER_0001" in text
              and "- outcome: needs-review" in text, f"{text[:200]}")
        check("1b report: the header names the run it covers",
              RID in text, "")
        check("1c report: the stage timeline is a table with the stage rows",
              "| TRAN\\|SLATE |" in text and "4.0" in text, "")
        check("1d report: gate reasons appear verbatim",
              "- a" in text and "- b" in text, "")
        check("1e report: the call table lists every job",
              all(job in text for job in ("translator", "reviewer")), "")
        check("1f report: the metadata-only form carries NO bodies",
              "<details>" not in text and "R1 " not in text, "")
        check("1g report: a body-looking reason is still escaped, not dropped",
              "injected" in text, "")


def case_2_totals() -> None:
    """Token totals are summed from the llm_call lines, never invented, and a
    usage-less failed call contributes zero."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root)
        path = logreport.write_full_report(root, "CHAPTER_0001")
        text = report_of(root)
        check("2a totals: the report was written", path is not None
              and path.is_file(), f"{path}")
        check("2b totals: prompt+completion are summed from the calls",
              "105 prompt + 21 completion" in text,
              f"{[l for l in text.splitlines() if 'Totals' in l]}")
        check("2c totals: the call count includes the failed call",
              "3 call(s)" in text,
              f"{[l for l in text.splitlines() if 'Totals' in l]}")


def case_3_escaping() -> None:
    """Model output is HTML-escaped, so a reply containing </details> or a
    fenced block cannot break the structure the report asserts on."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root)
        logreport.write_full_report(root, "CHAPTER_0001", io=True)
        text = report_of(root)
        check("3a escape: a </details> inside a response is escaped",
              "&lt;/details&gt;" in text, "")
        check("3b escape: the raw closing tag appears only as the real ones "
              "(one per details block, never inside a body)",
              text.count("</details>") == text.count("<details>") == 2,
              f"close={text.count('</details>')} open={text.count('<details>')}")
        check("3c escape: <b> in a prompt is escaped",
              "&lt;b&gt;one&lt;/b&gt;" in text, "")
        check("3d escape: a pipe in a TABLE CELL is backslash-escaped so it "
              "cannot open a new column",
              "TRAN\\|SLATE" in text, "")
        check("3e escape: a pipe in a bullet-list reason is left alone (a "
              "pipe cannot break a list item)",
              "|injected|" in text, "")


def case_4_bodies() -> None:
    """io=True emits one <details> block per call that actually carried a
    body; a body-stripped call stays metadata-only."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root)
        logreport.write_full_report(root, "CHAPTER_0001", io=True)
        text = report_of(root)
        check("4a bodies: both stored bodies appear",
              "R1 " in text and "R2" in text, "")
        check("4a2 bodies: the prompt appears alongside its response, escaped",
              "P &lt;b&gt;one&lt;/b&gt;" in text, "")
        check("4b bodies: one details block per call that carried a response",
              text.count("<details>") == 2, f"{text.count('<details>')}")
        with tempfile.TemporaryDirectory() as td2:
            root2 = Path(td2)
            seed(root2, with_bodies=False)
            logreport.write_full_report(root2, "CHAPTER_0001", io=True)
            text2 = report_of(root2)
            check("4c bodies: with no stored bodies there are no details blocks",
                  "<details>" not in text2
                  and "no call carried a stored" in text2, "")


def case_5_never_raises() -> None:
    """A truncated final line (a crash mid-append) is skipped, and a bucket
    that cannot be read is a warn, not an exception."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root, truncated=True)
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            path = logreport.write_full_report(root, "CHAPTER_0001")
        text = report_of(root)
        check("5a tolerant: a truncated line did not stop the report",
              path is not None and "- chapter: CHAPTER_0001" in text,
              f"{path}")
        with tempfile.TemporaryDirectory() as td2:
            empty = Path(td2)
            quiet = io.StringIO()
            with contextlib.redirect_stdout(quiet):
                out = logreport.write_full_report(empty, "CHAPTER_9999")
            check("5b tolerant: an empty chapter warns and returns None",
                  out is None and "no retained run files" in quiet.getvalue(),
                  f"{out} {quiet.getvalue()!r}")


def case_6_empty_and_header_only() -> None:
    """With log_orchestration off there is no tier-1 file, so chapter_end
    still writes a report -- header-only, never crashing."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_lf(root / "config.json", json.dumps({"log_orchestration": False}))
        reset()
        logger.log_event(root, {"event": "chunk", "chapter": "CHAPTER_0001.md"})
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            logreport.write_run_report(root, "CHAPTER_0001.md", 1, RID)
        text = report_of(root)
        check("6a header-only: the report still exists",
              "- chapter: CHAPTER_0001" in text, f"{text[:200]}")
        check("6b header-only: it says so rather than claiming an outcome",
              "(no chapter_end recorded)" in text
              and "no stage events" in text, "")
        check("6c header-only: no tier-1 run file was written either",
              not list((root / "logs").glob("run-*.jsonl")),
              f"{[p.name for p in (root / 'logs').glob('run-*.jsonl')]}")


def case_7_stem_and_file() -> None:
    """The writer accepts either the filename or the bare stem, and filters
    on both forms, so a caller holding only one still works."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root, chapter_field="CHAPTER_0001")
        logreport.write_run_report(root, "CHAPTER_0001", 1, RID)
        by_stem = report_of(root)
        seed(root, chapter_field="CHAPTER_0001.md")
        logreport.write_run_report(root, "CHAPTER_0001.md", 1, RID)
        by_file = report_of(root)
        check("7a stem/file: both spellings produce the same populated report",
              "- outcome:" in by_stem and "| TRAN\\|SLATE |" in by_stem
              and by_stem.count("| TRAN\\|SLATE |") == by_file.count("| TRAN\\|SLATE |")
              and by_stem.count("translator") == by_file.count("translator"),
              f"stem={len(by_stem)} file={len(by_file)}")


def case_8_stale_runs() -> None:
    """Index lines whose run files retention has removed are marked, never
    silently dropped -- an open without a close is the crash signal."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root)
        old = "20251231-235959-translate-1"
        index = root / "logs" / "chapters" / "CHAPTER_0001" / "index.jsonl"
        with index.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"ts": "2025-12-31T23:59:59.000+00:00",
                                 "run_id": old, "phase": "open"}) + "\n")
        stale = logreport.stale_run_ids(root, "CHAPTER_0001")
        check("8a stale: a run with index lines but no files is stale",
              stale == {old}, f"{stale}")
        logreport.write_full_report(root, "CHAPTER_0001")
        text = report_of(root)
        check("8b stale: the report marks it rather than dropping it",
              "Pruned runs" in text and old in text, "")
        closed = "20251231-235958-translate-2"
        with index.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"ts": "2025-12-31T23:59:58.000+00:00",
                                 "run_id": closed, "phase": "open"}) + "\n")
            fh.write(json.dumps({"ts": "2025-12-31T23:59:59.000+00:00",
                                 "run_id": closed, "phase": "close"}) + "\n")
        stale = logreport.stale_run_ids(root, "CHAPTER_0001")
        check("8c stale: a run that was pruned AFTER closing is not stale",
              closed not in stale, f"{stale}")


def case_9_run_files() -> None:
    """run_files is a directory scan of run-*.jsonl only: index.jsonl and
    report.md are never run files."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed(root)
        logreport.write_full_report(root, "CHAPTER_0001")
        found = logreport.run_files(root, "CHAPTER_0001")
        check("9a run files: only run-*.jsonl is returned",
              [p.name for p in found] == [f"run-{RID}.jsonl"],
              f"{[p.name for p in found]}")
        check("9b run files: report.md and index.jsonl are excluded",
              all(p.name != "report.md" for p in found), "")
        write_lf(root / "logs" / "chapters" / "CHAPTER_0001" / "llm-old.jsonl", "{}")
        found = logreport.run_files(root, "CHAPTER_0001")
        check("9c run files: a legacy llm-*.jsonl is not a run file",
              "llm-old.jsonl" not in [p.name for p in found],
              f"{[p.name for p in found]}")
        _ = os


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_run_report()
    case_2_totals()
    case_3_escaping()
    case_4_bodies()
    case_5_never_raises()
    case_6_empty_and_header_only()
    case_7_stem_and_file()
    case_8_stale_runs()
    case_9_run_files()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
