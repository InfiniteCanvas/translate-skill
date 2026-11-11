# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "requests>=2.31", "ebooklib>=0.18", "pillow>=10.0", "openai>=1.60", "anthropic>=1.0"]
# ///
"""Smoke tests for the two regeneration commands.

  * `glossary expand --chapters` re-runs the GLOSSARY_EXPAND stage for an
    already-translated chapter and merges the proposed terms into
    glossary.json (the repair path for a stage that only warns in-pipeline).
  * `recap --chapters` rebuilds story_state.json entries from translated/ and
    anchors each on the nearest earlier recap.

Both drive the real CLI command functions with only consensus.chat stubbed.

    uv run tests/test_regen_commands.py
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

import translate
from lib import consensus

PASSED = 0
FAILED: list[str] = []

SOURCE = "---\nchapter_title: 第一章\n---\n\n林远站在门口。\n"
TRANSLATED = "---\ntitle: Chapter One\n---\n\nLin Yuan stood at the door.\n"


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


def make_project(td: str, chapters: int = 1, *,
                 min_occurrences: int = 0) -> Path:
    root = Path(td)
    manifest = []
    for index in range(1, chapters + 1):
        name = f"CHAPTER_{index:04d}.md"
        manifest.append({"file": name, "number": index, "suffix": "",
                         "order": index - 1, "status": "translated",
                         "title": f"Chapter {index}"})
        write_lf(root / "source" / name, SOURCE)
        write_lf(root / "translated" / name, TRANSLATED)
    write_lf(root / "chapters.json",
             json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    write_lf(root / "config.json", json.dumps({
        "source_lang": "zh", "target_lang": "en",
        "auto_build_epub": False,
        "min_term_occurrences": min_occurrences,
        "providers": {"translator": {"base_url": "http://test.invalid/v1"}},
    }) + "\n")
    write_lf(root / "glossary.json", '{"terms": [], "retired": []}\n')
    return root


def stub_consensus(payload: dict, sinks: list[str] | None = None):
    orig = consensus.chat

    def fake(project_dir, cfg, job, prompt, json_schema=None,
             max_tokens=None, chapter=None):
        if sinks is not None:
            sinks.append(prompt)
        return json.dumps(payload, ensure_ascii=False)

    consensus.chat = fake
    return orig


def case_recap_regenerates_entries() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, chapters=2)
        prompts: list[str] = []
        orig = stub_consensus({"recap": "A short running recap."}, prompts)
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = translate.cmd_recap(
                    argparse.Namespace(chapters="1-2"), root)
        finally:
            consensus.chat = orig
        state = json.loads((root / "story_state.json").read_text(encoding="utf-8"))
        check("recap: exits 0", code == 0, f"code={code}")
        check("recap: both entries written",
              set(state.get("chapters", {})) == {"CHAPTER_0001", "CHAPTER_0002"},
              f"chapters={sorted(state.get('chapters', {}))}")
        check("recap: the model was called once per chapter", len(prompts) == 2,
              f"calls={len(prompts)}")
        check("recap: chapter 2's prompt carries chapter 1's recap",
              len(prompts) == 2 and "A short running recap." in prompts[1],
              "predecessor recap missing")

        # An untranslated chapter is skipped, not fatal.
        (root / "translated" / "CHAPTER_0002.md").unlink()
        orig = stub_consensus({"recap": "Again."})
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = translate.cmd_recap(
                    argparse.Namespace(chapters="2"), root)
        finally:
            consensus.chat = orig
        check("recap: an untranslated chapter is a skip, not a failure",
              code == 1, f"code={code}")


def case_glossary_expand_merges_terms() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, chapters=1, min_occurrences=0)
        orig = stub_consensus({"terms": [{
            "source": "林远",
            "translation": "Lin Yuan",
            "definition": "The protagonist.",
            "category": "person",
        }]})
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = translate.cmd_glossary_expand(
                    argparse.Namespace(chapters="1"), root)
        finally:
            consensus.chat = orig
        glossary = json.loads((root / "glossary.json").read_text(encoding="utf-8"))
        sources = [term.get("source") for term in glossary.get("terms", [])]
        check("glossary expand: exits 0", code == 0, f"code={code}")
        check("glossary expand: the proposed term was merged",
              sources == ["林远"], f"sources={sources}")

        # No translated chapter in range -> a clean refusal, not a crash.
        (root / "translated" / "CHAPTER_0001.md").unlink()
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = translate.cmd_glossary_expand(
                    argparse.Namespace(chapters="1"), root)
        except SystemExit:  # pragma: no cover - defensive
            code = None
        check("glossary expand: no eligible chapter exits 1", code == 1,
              f"code={code}")


def main() -> int:
    case_recap_regenerates_entries()
    case_glossary_expand_merges_terms()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())