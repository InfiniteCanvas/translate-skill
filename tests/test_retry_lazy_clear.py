# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "requests>=2.31", "ebooklib>=0.18", "pillow>=10.0", "openai>=1.60", "anthropic>=1.0"]
# ///
"""Pins retry's LAZY artifact clearing.

`retry` used to delete every selected chapter's artifacts before the batch
started. With abort-and-resume, a provider outage on chapter 1 would then have
already erased chapters 2..N -- work the run never touched. The clearing moved
into run_range's `prepare` callback, which fires per chapter immediately before
that chapter runs.

This suite drives the real cmd_retry -> run_range -> prepare path, with only
run_chapter stubbed to raise a PipelineError (the exhausted-provider signal),
and asserts the second chapter is byte-for-byte untouched.

Self-contained PASS/FAIL script (no pytest):

    uv run tests/test_retry_lazy_clear.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import pipeline, tn
from lib import client  # noqa: F401 - imported so the CLI modules resolve

PASSED = 0
FAILED: list[str] = []

SOURCE = "---\nchapter_title: One\n---\n\n文字。\n"
TRANSLATED = "---\ntitle: One\n---\n\nWords.\n"


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
    path.write_text(text, encoding="utf-8", newline="\n")


def make_project(td: str) -> tuple[Path, list[str]]:
    root = Path(td)
    files = ["CHAPTER_0001.md", "CHAPTER_0002.md"]
    manifest = [
        {"file": name, "number": index, "suffix": "", "order": index - 1,
         "status": "needs-review", "title": f"Chapter {index}"}
        for index, name in enumerate(files, start=1)
    ]
    write_lf(root / "chapters.json",
             json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    write_lf(root / "config.json", json.dumps({
        "source_lang": "zh", "target_lang": "en",
        "auto_build_epub": False,
        "providers": {"translator": {"base_url": "http://test.invalid/v1"}},
    }) + "\n")
    write_lf(root / "glossary.json", '{"terms": [], "retired": []}\n')
    for name in files:
        stem = Path(name).stem
        write_lf(root / "source" / name, SOURCE)
        write_lf(root / "translated" / name, TRANSLATED)
        write_lf(root / "draft" / f"{stem}.state.json", '{"stage": "TRANSLATE"}\n')
        write_lf(root / "draft" / f"{stem}.md", TRANSLATED)
        write_lf(root / "draft" / f"{stem}.lines.json", '{"lines": []}\n')
        write_lf(tn.notes_path(root, name), '{"notes": []}\n')
        write_lf(tn.dropped_path(root, name), '{"dropped": []}\n')
    return root, files


def artifacts(root: Path, name: str) -> dict[str, Path]:
    stem = Path(name).stem
    return {
        "state": root / "draft" / f"{stem}.state.json",
        "draft": root / "draft" / f"{stem}.md",
        "lines": root / "draft" / f"{stem}.lines.json",
        "translated": root / "translated" / name,
        "notes": tn.notes_path(root, name),
        "dropped": tn.dropped_path(root, name),
    }


def case_lazy_clear() -> None:
    with tempfile.TemporaryDirectory() as td:
        # translate.py imports resolve config through the skill root, so import
        # it only after the sandbox exists (same pattern as the other suites).
        import translate

        root, files = make_project(td)
        before = {name: artifacts(root, name) for name in files}

        def boom(project_dir, file, cfg, force=False, verify_on_faith=True):
            raise pipeline.PipelineError("provider unavailable (test)")

        orig = pipeline.run_chapter
        pipeline.run_chapter = boom
        raised = False
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    translate.cmd_retry(
                        argparse.Namespace(failed=False, chapters="1-2",
                                           no_verify=False), root)
                except pipeline.PipelineError:
                    raised = True
        finally:
            pipeline.run_chapter = orig

        check("1 the abort propagates out of cmd_retry", raised, "not raised")
        check("2 the attempted chapter's artifacts are cleared",
              not any(path.exists() for path in before[files[0]].values()),
              f"left={[p.name for p in before[files[0]].values() if p.exists()]}")
        check("3 the untouched chapter keeps every artifact",
              all(path.exists() for path in before[files[1]].values()),
              f"gone={[p.name for p in before[files[1]].values() if not p.exists()]}")
        saved = json.loads((root / "chapters.json").read_text(encoding="utf-8"))
        check("4 the attempted chapter is pending", saved[0]["status"] == "pending",
              f"status={saved[0]['status']}")
        check("5 the untouched chapter keeps needs-review",
              saved[1]["status"] == "needs-review", f"status={saved[1]['status']}")


def main() -> int:
    case_lazy_clear()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())