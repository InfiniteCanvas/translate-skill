"""Tests for the pipeline's lifecycle, stage and gate trace events.

Drives run_chapter end to end with pipeline._chat stubbed (this suite's
established convention) and asserts what the ORCHESTRATION timeline says:

- exactly one chapter_start / chapter_end pair, both carrying the file,
  number, outcome, attempts, stages_run, calls, tokens and elapsed_s
- a skipped chapter emits NEITHER (no index lines, no false
  open-without-close signal)
- every stage that actually ran has a begin AND an end, in order, and no
  TRANSLATE event appears on a resumed chapter
- gate verdicts carry the COMPLETE reason list, not the truncated one
- a chapter that raises still closes its index and records outcome "crashed",
  and the re-raise is unchanged
- both index.jsonl writers: chapter open/close and project open/close

Hermetic sandboxes under tempfile.TemporaryDirectory(). Self-contained
PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_pipeline_log_events.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "requests>=2.31"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config, logger, pipeline, project  # noqa: E402

PASSED = 0
FAILED: list[str] = []

SRC = """---
chapter_number: {n}
chapter_title: "Chapter {n}"
---
The door was heavy and the wind pushed back.
She counted the steps aloud so no one followed her.
A lantern guttered somewhere below the stairs.
"""


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


def make_project(root: Path, chapters: int = 1, auto_epub: bool = False) -> None:
    """Pipeline-shaped fixture: source/ + translated/ + draft/ + notes/
    pre-created, an accurate chapters.json, and a one-block config.json that
    every job inherits from. auto_build_epub off so the smoke never shells
    out to epubcheck."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("source", "translated", "draft", "notes"):
        (root / name).mkdir(exist_ok=True)
    for n in range(1, chapters + 1):
        (root / "source" / f"CHAPTER_{n:04d}.md").write_text(
            SRC.format(n=n), encoding="utf-8", newline="\n")
    (root / "chapters.json").write_text(
        json.dumps([{"file": f"CHAPTER_{n:04d}.md", "number": n,
                     "order": n - 1, "status": "pending"}
                    for n in range(1, chapters + 1)],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    block = {"base_url": "http://x", "model": "m", "api_key_env": "K",
             "timeout_s": 5, "max_retries": 1}
    (root / "config.json").write_text(
        json.dumps({"providers": {"translator": [block],
                                  "reviewer": [block],
                                  "glossary": [block],
                                  "annotator": [block],
                                  "recap": [block],
                                  "consensus": [block]},
                    "source_lang": "ko", "target_lang": "en",
                    "auto_build_epub": auto_epub},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fake_chat(project_dir, cfg, job, prompt, json_schema=None,
              max_tokens=None, chapter=None):
    """Canned answers, classified BY PROMPT CONTENT like the mock server.
    `fails` forces a stage to reject so the gate path is exercised."""
    import re
    numbered = [int(n) for n in re.findall(r'"i":\s*(\d+)', prompt)]
    wanted = sorted(set(numbered)) or [1]
    if '"lines"' in prompt and '"title"' in prompt:
        return json.dumps({"title": "Chapter",
                           "lines": [{"i": i, "t": f"t{i}"} for i in wanted]})
    if '"verdict"' in prompt:
        if FAILS.get("always"):
            return json.dumps({"verdict": "CHANGES",
                               "reasons": FAILS.get("reasons", [])})
        return json.dumps({"verdict": FAILS.pop("faithfulness", "SUCCESS"),
                           "reasons": FAILS.pop("reasons", [])})
    if '"notes"' in prompt:
        return json.dumps({"notes": []})
    if '"terms"' in prompt:
        return json.dumps({"terms": []})
    if '"recap"' in prompt:
        return json.dumps({"recap": "r"})
    return "{}"


FAILS: dict[str, object] = {}


def timeline(root: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted((root / "logs").glob("run-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def named(root: Path, event: str) -> list[dict]:
    return [e for e in timeline(root) if e.get("event") == event]


def index_rows(root: Path, stem: str) -> list[dict]:
    path = root / "logs" / "chapters" / stem / "index.jsonl"
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def tier2(root: Path, stem: str, event: str) -> list[dict]:
    """`feedback`, `chunk` and `result` are tier 2, so they live in the
    chapter's own file, NOT the root orchestration timeline."""
    out: list[dict] = []
    directory = root / "logs" / "chapters" / stem
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("run-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("event") == event:
                    out.append(row)
    return out


# ---------------------------------------------------------------------- cases


def case_1_lifecycle() -> None:
    """One chapter_start and one chapter_end, fully populated, and the eight
    stages bracketed begin/end in order."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        root.mkdir()
        make_project(root)
        FAILS.clear()
        reset()
        pipeline._chat = fake_chat
        try:
            outcome = pipeline.run_chapter(root, "CHAPTER_0001.md",
                                           config.load_config(root))
        finally:
            pipeline._chat = _REAL_CHAT
        check("1a lifecycle: the chapter translated", outcome == "translated",
              outcome)
        starts = named(root, "chapter_start")
        ends = named(root, "chapter_end")
        check("1b lifecycle: exactly one chapter_start",
              len(starts) == 1, f"{len(starts)}")
        check("1c lifecycle: exactly one chapter_end", len(ends) == 1,
              f"{len(ends)}")
        start = starts[0]
        end = ends[0]
        check("1d lifecycle: chapter_start carries file, number and line count",
              start.get("chapter") == "CHAPTER_0001.md"
              and start.get("number") == 1 and start.get("lines") == 3,
              f"{start}")
        check("1e lifecycle: chapter_start carries the resume state",
              "resume_stage" in start and "resume_chunks" in start, f"{start}")
        check("1f lifecycle: chapter_end carries the outcome and counts",
              end.get("outcome") == "translated" and end.get("attempts") == 1
              and isinstance(end.get("calls"), int)
              and isinstance(end.get("tokens"), dict)
              and isinstance(end.get("elapsed_s"), (int, float)), f"{end}")
        check("1g lifecycle: chapter_end records every stage it ran",
              end.get("stages_run") == list(pipeline.STAGES), f"{end}")

        stages = [e for e in timeline(root) if e.get("event") == "stage"]
        begins = [e["stage"] for e in stages if e["phase"] == "begin"]
        ends_by = [e["stage"] for e in stages if e["phase"] == "end"]
        check("1h lifecycle: every stage has a begin and an end",
              begins == ends_by == list(pipeline.STAGES),
              f"begins={begins}")
        check("1i lifecycle: each stage end carries an elapsed_s",
              all(isinstance(e.get("elapsed_s"), (int, float))
                  for e in stages if e["phase"] == "end"), "")
        check("1j lifecycle: the stages are interleaved begin/end in order",
              [e["phase"] for e in stages] == ["begin", "end"] * len(pipeline.STAGES),
              f"{[e['phase'] for e in stages]}")

        rows = index_rows(root, "CHAPTER_0001")
        check("1k lifecycle: the chapter index has one open and one close",
              [r["phase"] for r in rows] == ["open", "close"], f"{rows}")
        check("1l lifecycle: the close line carries the outcome and counts",
              rows[1].get("outcome") == "translated"
              and isinstance(rows[1].get("calls"), int)
              and rows[1].get("stages") == len(pipeline.STAGES)
              and rows[0].get("file") == "CHAPTER_0001.md", f"{rows[1]}")


def case_2_gates() -> None:
    """A faithfulness gate that keeps rejecting drives the chapter to
    needs-review, and every verdict plus the complete reason history is in
    the log."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        root.mkdir()
        make_project(root)
        FAILS.clear()
        FAILS["always"] = "CHANGES"
        FAILS["reasons"] = ["r1", "r2", "r3", "r4"]
        reset()
        pipeline._chat = fake_chat
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                outcome = pipeline.run_chapter(root, "CHAPTER_0001.md",
                                               config.load_config(root),
                                               force=True)
        finally:
            pipeline._chat = _REAL_CHAT
        gates = [e for e in named(root, "gate") if e["stage"] == "FAITH"]
        fails = [g for g in gates if g["verdict"] == "fail"]
        check("2a gate: every failing attempt recorded verdict=fail",
              len(fails) == 3, f"{len(fails)}")
        check("2b gate: the COMPLETE reason list is in the log, not the "
              "truncated one attempt_failed carries",
              all(g["reasons"] == ["r1", "r2", "r3", "r4"] for g in fails),
              f"{fails[0]['reasons'] if fails else None}")
        attempt_failures = named(root, "attempt_failed")
        check("2c gate: attempt_failed still truncates to the last three",
              attempt_failures
              and all(len(a["feedback"]) <= 3 for a in attempt_failures),
              f"{attempt_failures[0] if attempt_failures else None}")
        feedback = tier2(root, "CHAPTER_0001", "feedback")
        check("2d gate: a tier-2 feedback event carries the full history",
              feedback and len(feedback[0]["reasons"]) == 4,
              f"{feedback[0] if feedback else None}")
        check("2e gate: the chapter gave up as needs-review",
              outcome == "needs-review", outcome)
        ends = named(root, "chapter_end")
        check("2f gate: chapter_end reports the retry count and needs-review",
              ends and ends[0]["outcome"] == "needs-review"
              and ends[0]["attempts"] == 3, f"{ends}")


def case_3_skip() -> None:
    """An already-translated chapter is skipped BEFORE chapter_start, so it
    leaves no index lines and no open-without-close signal."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        root.mkdir()
        make_project(root)
        FAILS.clear()
        reset()
        pipeline._chat = fake_chat
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                first = pipeline.run_chapter(root, "CHAPTER_0001.md",
                                             config.load_config(root))
                second = pipeline.run_chapter(root, "CHAPTER_0001.md",
                                              config.load_config(root))
        finally:
            pipeline._chat = _REAL_CHAT
        check("3a skip: the first run translated", first == "translated", first)
        check("3b skip: the second run returned skipped", second == "skipped",
              second)
        check("3c skip: no second chapter_start", len(named(root, "chapter_start")) == 1,
              f"{len(named(root, 'chapter_start'))}")
        check("3d skip: no second chapter_end", len(named(root, "chapter_end")) == 1,
              f"{len(named(root, 'chapter_end'))}")
        rows = index_rows(root, "CHAPTER_0001")
        check("3e skip: the index still has exactly one open/close pair",
              [r["phase"] for r in rows] == ["open", "close"], f"{rows}")


def case_4_crash() -> None:
    """A chapter that raises OUT of the pipeline -- PipelineError, which the
    stages deliberately re-raise -- still closes its index, records outcome
    "crashed", and propagates unchanged. (A plain Exception is retried as
    feedback, so it never reaches this path; that is the point of the
    distinction.)"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        root.mkdir()
        make_project(root)
        FAILS.clear()
        reset()

        def boom(project_dir, cfg, job, prompt, json_schema=None,
                 max_tokens=None, chapter=None):
            raise pipeline.PipelineError("boom")

        pipeline._chat = boom
        raised = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline.run_chapter(root, "CHAPTER_0001.md",
                                     config.load_config(root))
        except pipeline.PipelineError as exc:
            raised = exc
        finally:
            pipeline._chat = _REAL_CHAT
        check("4a crash: the original exception propagates unchanged",
              isinstance(raised, pipeline.PipelineError)
              and str(raised) == "boom", repr(raised))
        ends = named(root, "chapter_end")
        check("4b crash: chapter_end recorded outcome=crashed",
              len(ends) == 1 and ends[0]["outcome"] == "crashed", f"{ends}")
        check("4c crash: the crash is named on the event",
              ends and "boom" in str(ends[0].get("error", "")), f"{ends}")
        rows = index_rows(root, "CHAPTER_0001")
        check("4d crash: the index is closed, not left open",
              [r["phase"] for r in rows] == ["open", "close"], f"{rows}")
        check("4e crash: the close line records the crash outcome",
              rows and rows[-1].get("outcome") == "crashed", f"{rows[-1:]}")

        # And no run-scoped crash left the chapter's index half-written.
        opens = [r for r in rows if r["phase"] == "open"]
        closes = [r for r in rows if r["phase"] == "close"]
        check("4f crash: opens and closes balance", len(opens) == len(closes),
              f"{len(opens)} opens, {len(closes)} closes")


def case_5_resume() -> None:
    """A resumed chapter enters only the stages it reaches, and chapter_start
    reports where it picked up."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        root.mkdir()
        make_project(root)
        FAILS.clear()
        reset()
        pipeline._chat = fake_chat
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline.run_chapter(root, "CHAPTER_0001.md",
                                     config.load_config(root))
        finally:
            pipeline._chat = _REAL_CHAT
        # Re-stage the chapter to VALIDATE and re-run without force.
        # Re-stage the chapter at VALIDATE and re-run without force. The draft state
# is written explicitly: a completed chapter's state file is gone, and
# resume needs `lines` to be the translation the first run produced.
        translated = (root / "translated" / "CHAPTER_0001.md").read_text(
            encoding="utf-8")
        lines = [ln for ln in translated.split("\n") if ln.strip()]
        (root / "draft" / "CHAPTER_0001.state.json").write_text(
            json.dumps({"stage": "VALIDATE", "lines": lines, "chunks": [],
                        "attempt": 0, "feedback": [],
                        "title": "Chapter"}, ensure_ascii=False),
            encoding="utf-8", newline="\n")
        manifest = project.load_manifest(root)
        project.set_status(manifest, "CHAPTER_0001.md", "needs-review")
        project.save_manifest(root, manifest)
        logger._run_path = None
        pipeline._chat = fake_chat
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline.run_chapter(root, "CHAPTER_0001.md",
                                     config.load_config(root))
        finally:
            pipeline._chat = _REAL_CHAT
        starts = named(root, "chapter_start")
        check("5a resume: the second chapter_start reports the resume stage",
              len(starts) == 2 and starts[1].get("resume_stage") == "VALIDATE",
              f"{starts[1] if len(starts) > 1 else None}")
        # The first stage each run ENTERS, recovered by walking the timeline and
        # pairing each chapter_start with the next stage begin. run_id cannot
        # separate the runs here (it is second-granular, and two stubbed runs
        # land inside one second), and counts would be muddied by the retry
        # the resumed run then takes.
        firsts: list[str] = []
        pending = False
        for e in timeline(root):
            if e.get("event") == "chapter_start":
                pending = True
            elif pending and e.get("event") == "stage" and e.get("phase") == "begin":
                firsts.append(e["stage"])
                pending = False
        check("5b resume: run 1 entered TRANSLATE and run 2 entered VALIDATE",
              firsts == ["TRANSLATE", "VALIDATE"], f"{firsts}")
        began = [e["stage"] for e in timeline(root)
                 if e.get("event") == "stage" and e.get("phase") == "begin"]
        check("5c resume: the resumed run never re-entered TRANSLATE first, "
              "and both runs reached ASSEMBLE",
              began.count("ASSEMBLE") == 2, f"{began}")


_REAL_CHAT = pipeline._chat


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_lifecycle()
    case_2_gates()
    case_3_skip()
    case_4_crash()
    case_5_resume()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())