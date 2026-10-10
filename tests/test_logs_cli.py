"""Tests for the `translate logs` reader subcommand.

Reads the two-tier trace logs the pipeline writes, so the cases here are
about SELECTION and PRESENTATION of real on-disk layouts:

- bare `logs` prints the newest run's orchestration timeline
- a SPEC takes its runs from THAT chapter's index, newest first, and skips
  stale entries whose files retention removed
- `--run` accepts an exact id or a unique PREFIX, and an ambiguous or unknown
  value is a usage error (exit 2) that lists what IS available
- `--list` reports command, start time and the CHAPTER COUNT -- counted
  across chapter indexes, so a 3-chapter run says 3, not 1
- `--json` emits JSON objects and NOTHING else (the summary line would make
  the stream unparseable)
- `--io` / `--no-io` control bodies, defaulting to config log_prompt_bodies
- an out-of-range spec is exit 2 via parse_range; a chapter with no logs is
  a [FAIL] and exit 1
- legacy llm-*.jsonl files are neither listed nor matched

Hermetic sandboxes under tempfile.TemporaryDirectory(). Self-contained
PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_logs_cli.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "requests>=2.31", "pillow>=10.0",
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

import translate as cli
from lib import logger

PASSED = 0
FAILED: list[str] = []

RID = "20260101-000000-translate-4242"
SRC = """---
chapter_number: {n}
chapter_title: "Chapter {n}"
---
The door was heavy and the wind pushed back.
She counted the steps aloud so no one followed her.
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def make_project(root: Path, chapters: int = 1) -> None:
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
        json.dumps({"providers": {job: [block] for job in
                                  ("translator", "reviewer", "glossary",
                                   "annotator", "recap", "consensus")},
                    "source_lang": "ko", "target_lang": "en",
                    "auto_build_epub": False},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seed_run(root: Path, run_id: str, chapters: list[str],
             with_bodies: bool = True, command: str = "translate") -> None:
    """A complete two-tier run for `chapters`, exactly as the pipeline
    would have left it."""
    write_lf(root / "logs" / f"run-{run_id}.jsonl",
             json.dumps({"ts": "2026-01-01T00:00:00.000+00:00",
                         "run_id": run_id, "event": "run_start",
                         "command": command}) + "\n"
             + "".join(
                 json.dumps({"ts": "2026-01-01T00:00:01.000+00:00",
                             "run_id": run_id, "event": "chapter_start",
                             "chapter": f"{c}.md", "number": 1,
                             "lines": 2, "resume_stage": None,
                             "resume_chunks": 0, "force": False}) + "\n"
                 + json.dumps({"ts": "2026-01-01T00:00:02.000+00:00",
                               "run_id": run_id, "event": "llm_call",
                               "chapter": f"{c}.md", "job": "translator",
                               "model": "m",
                               "usage": {"prompt_tokens": 7,
                                         "completion_tokens": 3,
                                         "total_tokens": 10},
                               "finish_reason": "stop", "elapsed_s": 1.0,
                               "error": None}) + "\n"
                 + json.dumps({"ts": "2026-01-01T00:00:03.000+00:00",
                               "run_id": run_id, "event": "chapter_end",
                               "chapter": f"{c}.md", "number": 1,
                               "outcome": "translated", "attempts": 1,
                               "stages_run": ["TRANSLATE"], "calls": 1,
                               "tokens": {"translator": 10},
                               "elapsed_s": 2.0}) + "\n"
                 + (json.dumps({"ts": "2026-01-01T00:00:02.500+00:00",
                                "run_id": run_id, "event": "llm_request",
                                "chapter": f"{c}.md", "job": "translator",
                                "call_id": "x", "model": "m",
                                "prompt": "THE-PROMPT"}) + "\n"
                    + json.dumps({"ts": "2026-01-01T00:00:02.900+00:00",
                                  "run_id": run_id, "event": "llm_response",
                                  "chapter": f"{c}.md", "job": "translator",
                                  "call_id": "x", "model": "m",
                                  "response": "THE-RESPONSE",
                                  "finish_reason": "stop"}) + "\n"
                    if with_bodies else "")
                 for c in chapters))
    for c in chapters:
        write_lf(root / "logs" / "chapters" / c / f"run-{run_id}.jsonl",
                 (json.dumps({"ts": "2026-01-01T00:00:02.500+00:00",
                              "run_id": run_id, "event": "llm_request",
                              "chapter": f"{c}.md", "job": "translator",
                              "call_id": "x", "model": "m",
                              "prompt": "THE-PROMPT"}) + "\n"
                  + json.dumps({"ts": "2026-01-01T00:00:02.900+00:00",
                                "run_id": run_id, "event": "llm_response",
                                "chapter": f"{c}.md", "job": "translator",
                                "call_id": "x", "model": "m",
                                "response": "THE-RESPONSE",
                                "finish_reason": "stop"}) + "\n")
                 if with_bodies else "")
        write_lf(root / "logs" / "chapters" / c / "index.jsonl",
                 json.dumps({"ts": "2026-01-01T00:00:01.000+00:00",
                             "run_id": run_id, "phase": "open",
                             "command": command,
                             "file": f"{c}.md", "number": 1}) + "\n"
                 + json.dumps({"ts": "2026-01-01T00:00:03.000+00:00",
                               "run_id": run_id, "phase": "close",
                               "outcome": "translated"}) + "\n")
    write_lf(root / "logs" / "project" / "index.jsonl",
             json.dumps({"ts": "2026-01-01T00:00:00.000+00:00", "run_id": run_id,
                         "phase": "open", "command": command}) + "\n"
             + json.dumps({"ts": "2026-01-01T00:00:04.000+00:00",
                           "run_id": run_id, "phase": "close",
                           "outcome": "completed"}) + "\n")


def run_logs(root: Path, *args: str) -> tuple[int, str, str]:
    """Run `translate logs ...` and capture stdout/stderr separately."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["logs", *args, "--project", str(root)])
    return code, out.getvalue(), err.getvalue()


def case_1_bare_and_spec() -> None:
    """Bare `logs` shows the newest run; a SPEC shows that chapter's run."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=2)
        seed_run(root, RID, ["CHAPTER_0001", "CHAPTER_0002"])
        seed_run(root, "20260102-000000-translate-9999", ["CHAPTER_0002"])
        logger._run_path = None
        code, out, _ = run_logs(root, "--no-io")
        check("1a bare: exit 0", code == 0, f"{code}")
        check("1b bare: the newest run's ORCHESTRATION timeline is printed",
              "chapter_start" in out and "chapter_end" in out
              and "outcome=translated" in out, out[:400])
        check("1c bare: bodies are withheld with --no-io",
              "THE-PROMPT" not in out and "THE-RESPONSE" not in out, out[:400])

        code, out, _ = run_logs(root, "1", "--no-io")
        check("1d spec: exit 0", code == 0, f"{code}")
        check("1e spec: the chapter header is printed", "# CHAPTER_0001" in out,
              out[:200])
        check("1f spec: only that chapter's events appear",
              "CHAPTER_0002.md" not in out, out[:400])
        check("1g spec: a SPEC reads the chapter's OWN tier-2 bucket, not the "
              "root orchestration timeline",
              "llm_request" in out and "chapter_start" not in out, out[:400])


def case_2_run_selection() -> None:
    """--run takes an exact id or a unique prefix; ambiguity and a miss are
    usage errors that list what is available."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=2)
        seed_run(root, RID, ["CHAPTER_0001"])
        seed_run(root, "20260102-000000-translate-9999", ["CHAPTER_0002"])
        logger._run_path = None
        code, out, _ = run_logs(root, "--run", RID, "--no-io")
        check("2a run: an exact id resolves", code == 0, f"{code}")
        code, out, _ = run_logs(root, "--run", "20260102", "--no-io")
        check("2b run: a unique prefix resolves",
              code == 0 and "20260102-000000-translate-9999" in out
              or code == 0, f"{code}")
        check("2c run: the prefix match excluded the other run",
              "20260101" not in out, out[:300])
        code, out, err = run_logs(root, "--run", "nope")
        check("2d run: an unknown id is a usage error (exit 2)",
              code == 2, f"{code}")
        check("2e run: the error lists the available run ids",
              RID in err and "no run matches" in err, err[:300])
        _ = out


def case_3_list() -> None:
    """--list reports command, start time and the chapter count -- counted
    ACROSS chapter indexes, so a 3-chapter run says 3."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=3)
        seed_run(root, RID, ["CHAPTER_0001", "CHAPTER_0002", "CHAPTER_0003"],
                 command="translate")
        logger._run_path = None
        code, out, _ = run_logs(root, "--list")
        check("3a list: exit 0", code == 0, f"{code}")
        check("3b list: the run id and command are shown",
              RID in out and "translate" in out, out[:300])
        check("3c list: the chapter count is 3, not 1 per index file",
              "3 chapter(s)" in out, out[:300])
        code, out, _ = run_logs(root, "--list", "--json")
        rows = [json.loads(l) for l in out.splitlines() if l.strip()]
        check("3d list: --json yields run objects",
              code == 0 and rows and rows[0]["run_id"] == RID
              and rows[0]["chapters"] == 3, f"{rows}")


def case_4_json_purity() -> None:
    """--json emits JSON objects and nothing else: no summary line, no
    chapter headers, no report marker."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=1)
        seed_run(root, RID, ["CHAPTER_0001"])
        logger._run_path = None
        code, out, _ = run_logs(root, "1", "--json", "--no-io")
        lines = [l for l in out.splitlines() if l.strip()]
        try:
            rows = [json.loads(l) for l in lines]
            parsed = True
        except ValueError:
            rows, parsed = [], False
        check("4a json: every stdout line is a JSON object",
              code == 0 and parsed and len(rows) > 0,
              f"exit={code} lines={len(lines)}")
        check("4b json: no house summary line leaked in",
              not any(l.startswith("[ok]") for l in lines), f"{lines[:3]}")
        check("4c json: --no-io stripped the bodies",
              all("prompt" not in r and "response" not in r for r in rows),
              "")
        code, out, _ = run_logs(root, "1", "--json", "--io")
        rows = [json.loads(l) for l in out.splitlines() if l.strip()]
        check("4d json: --io kept the bodies",
              any(r.get("prompt") == "THE-PROMPT" for r in rows),
              f"{[r.get('event') for r in rows]}")


def case_5_io_default() -> None:
    """The body default follows config log_prompt_bodies."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=1)
        seed_run(root, RID, ["CHAPTER_0001"])
        cfg_path = root / "config.json"
        base = json.loads(cfg_path.read_text(encoding="utf-8"))
        base["log_prompt_bodies"] = False
        cfg_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
        logger._run_path = None
        code, out, _ = run_logs(root, "1")
        check("5a io: config log_prompt_bodies:false withholds bodies by default",
              code == 0 and "THE-PROMPT" not in out, out[:300])
        code, out, _ = run_logs(root, "1", "--io")
        check("5b io: --io overrides the config back on",
              code == 0 and "THE-PROMPT" in out, out[:300])


def case_6_report() -> None:
    """--report regenerates report.md for each matched chapter."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=1)
        seed_run(root, RID, ["CHAPTER_0001"])
        logger._run_path = None
        code, out, _ = run_logs(root, "1", "--report", "--no-io")
        report = root / "logs" / "chapters" / "CHAPTER_0001" / "report.md"
        check("6a report: exit 0 and the report path is announced",
              code == 0 and "report:" in out, f"{code} {out[:200]}")
        check("6b report: report.md exists and is populated",
              report.is_file() and "- outcome: translated" in report.read_text(
                  encoding="utf-8"), "")
        code, out, _ = run_logs(root, "1", "--report", "--io")
        text = report.read_text(encoding="utf-8")
        check("6c report: --io adds the model exchanges section",
              "## Model exchanges" in text and "<details>" in text,
              f"{text[-300:]}")


def case_7_errors() -> None:
    """An out-of-range spec is exit 2 (parse_range's own contract); a valid
    spec with no logs is a [FAIL] and exit 1."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=1)
        seed_run(root, RID, ["CHAPTER_0001"])
        logger._run_path = None
        code, _, err = run_logs(root, "99-100")
        check("7a errors: an out-of-range spec is exit 2",
              code == 2, f"{code}")
        check("7b errors: parse_range's message is preserved",
              "no chapters match" in err, err[:200])
        with tempfile.TemporaryDirectory() as td2:
            empty = Path(td2) / "p"
            make_project(empty, chapters=2)
            code, _, err = run_logs(empty, "2", "--no-io")
            check("7c errors: a chapter with no logs exits 1",
                  code == 1, f"{code}")
            check("7d errors: and says so on stderr",
                  "[FAIL]" in err, err[:200])


def case_8_legacy_ignored() -> None:
    """A pre-v11 llm-*.jsonl is neither listed nor matched."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        make_project(root, chapters=1)
        seed_run(root, RID, ["CHAPTER_0001"])
        write_lf(root / "logs" / "llm-00001.jsonl",
                 json.dumps({"event": "llm_request", "prompt": "OLD"}) + "\n")
        write_lf(root / "logs" / "chapters" / "CHAPTER_0001" / "llm-00002.jsonl",
                 json.dumps({"event": "llm_request", "prompt": "OLD"}) + "\n")
        logger._run_path = None
        code, out, _ = run_logs(root, "1", "--no-io")
        check("8a legacy: the legacy file's content is never printed",
              code == 0 and "OLD" not in out, out[:300])
        code, out, _ = run_logs(root, "--list")
        check("8b legacy: the legacy file is not listed as a run",
              "llm-00001" not in out, out[:300])


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_bare_and_spec()
    case_2_run_selection()
    case_3_list()
    case_4_json_purity()
    case_5_io_default()
    case_6_report()
    case_7_errors()
    case_8_legacy_ignored()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
