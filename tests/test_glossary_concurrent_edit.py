"""The concurrent-glossary-edit race: a glossary edit made while a chapter
is in flight must survive that chapter's end-of-stage save.

The pipeline loads glossary.json when a chapter starts and the
GLOSSARY_EXPAND stage wrote that copy back wholesale at the end of the
stage. A write during the chapter -- a human fixing a term from the
orchestrator's glossary page, a `review fix` retiring a mundane entry --
was therefore reverted by the stage's own save, silently, because the
write succeeded.

This drives the real stage through run_chapter (the suite's established
convention: pipeline._chat stubbed, no provider contacted) with the edit
made from inside the stubbed translator call -- i.e. exactly while the
chapter is in flight -- and asserts that BOTH the stage's own addition
and the concurrent edit are on disk afterwards.

Covers: a concurrent EDIT of an unrelated entry survives; a concurrent
RETIREMENT (an entry removed and recorded under "retired") survives; the
stage's own new term still lands; and an entry the stage itself retired
is not resurrected.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_glossary_concurrent_edit.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0", "openai>=1.60", "anthropic>=1.0"]
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

from lib import config, glossary, logger, pipeline

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


CHAPTER = "CHAPTER_0001.md"


def make_project(root: Path) -> Path:
    proj = root / "proj"
    proj.mkdir()
    for d in ("source", "translated", "draft", "notes"):
        (proj / d).mkdir()
    (proj / "source" / CHAPTER).write_text(
        "他捡起一块灵石。\n灵石发光了。\n第三行。", encoding="utf-8", newline="\n")
    (proj / "chapters.json").write_text(
        json.dumps([{"file": CHAPTER, "number": 1, "suffix": "",
                     "order": 0, "status": "pending"}],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    glossary.save(proj, {"terms": [
        {"source": "灵石", "variants": [], "translation": "spirit stone",
         "alt_translations": [], "category": "item", "definition": "A spirit stone."},
        {"source": "旧条目", "variants": [], "translation": "old entry",
         "alt_translations": [], "category": "other", "definition": "An old entry."},
    ]})
    (proj / "config.json").write_text(
        json.dumps({"min_term_occurrences": 0,
                    "providers": {"translator": {"base_url": "http://test.invalid/v1"}}},
                   indent=2) + "\n", encoding="utf-8")
    return proj


def fake_chat(concurrent_edit):
    """The translator call is where the concurrent edit happens: the stub
    edits glossary.json on disk while the chapter is in flight, which is
    the race the fix is about. The rest of the jobs answer in the
    established shapes."""
    def chat(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None,
             chapter=None):
        if "verdict" in prompt and '"terms"' not in prompt:
            return json.dumps({"verdict": "SUCCESS", "reasons": []})
        if '"notes"' in prompt or '"note"' in prompt:
            return json.dumps({"notes": []})
        if "Flagged Terms" in prompt:
            return json.dumps({"decisions": []})
        if '"terms"' in prompt:
            return json.dumps({"terms": [
                {"source": "新词条", "variants": [], "translation": "new term",
                 "definition": "A new term.", "category": "other"}]})
        if "### Source Data" in prompt:
            concurrent_edit(Path(project_dir))
            arr = json.loads(prompt.rsplit("### Source Data", 1)[1].strip())
            return json.dumps(
                {"title": "Mock Chapter Title",
                 "lines": [{"i": x["i"], "t": x["t"]} for x in arr]},
                ensure_ascii=False)
        if '"recap"' in prompt:
            return json.dumps({"recap": "Mock recap of the story so far."})
        raise AssertionError(f"unroutable prompt (job={job}): {prompt[:120]!r}")
    return chat


def case_concurrent_edit_survives() -> None:
    """An edit to an UNRELATED entry, made while the chapter is in flight,
    survives the chapter's end-of-stage save; the stage's own new term
    still lands."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td))

        def concurrent_edit(project_dir: Path) -> None:
            g = glossary.load(project_dir)
            for term in g["terms"]:
                if term["source"] == "旧条目":
                    term["translation"] = "edited by the human"
            glossary.save(project_dir, g)

        real = pipeline._chat
        pipeline._chat = fake_chat(concurrent_edit)
        logger._run_path = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                outcome = pipeline.run_chapter(proj, CHAPTER, config.load_config(proj))
        finally:
            pipeline._chat = real
        g = glossary.load(proj)
        by_src = {t["source"]: t for t in g["terms"]}
        check("1a the chapter ran", outcome == "translated", str(outcome))
        check("1b the concurrent edit survived the chapter's save",
              by_src.get("旧条目", {}).get("translation") == "edited by the human",
              json.dumps(by_src.get("旧条目"), ensure_ascii=False))
        check("1c the stage's own new term still landed",
              by_src.get("新词条", {}).get("translation") == "new term",
              json.dumps(by_src.get("新词条"), ensure_ascii=False))
        check("1d the untouched entry is unchanged",
              by_src.get("灵石", {}).get("translation") == "spirit stone",
              json.dumps(by_src.get("灵石"), ensure_ascii=False))


def case_concurrent_retirement_survives() -> None:
    """A concurrent RETIREMENT (the entry removed and recorded under
    "retired") survives, and the retired list is not rolled back."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td))

        def concurrent_edit(project_dir: Path) -> None:
            glossary.retire(project_dir, ["旧条目"])

        real = pipeline._chat
        pipeline._chat = fake_chat(concurrent_edit)
        logger._run_path = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline.run_chapter(proj, CHAPTER, config.load_config(proj))
        finally:
            pipeline._chat = real
        g = glossary.load(proj)
        by_src = {t["source"]: t for t in g["terms"]}
        check("2a the retired entry is gone", "旧条目" not in by_src,
              json.dumps(list(by_src), ensure_ascii=False))
        check("2b it is recorded under retired",
              "旧条目" in (g.get("retired") or []),
              json.dumps(g.get("retired"), ensure_ascii=False))
        check("2c the stage's own new term still landed",
              "新词条" in by_src, json.dumps(list(by_src), ensure_ascii=False))


def case_unit_shapes() -> None:
    """The diff helpers' own contract: the snapshot keys by source, and a
    changed entry is carried onto a fresh copy while an unchanged one is
    left alone."""
    g = {"terms": [{"source": "甲", "translation": "A"},
                   {"source": "乙", "translation": "B"}]}
    snap = pipeline._glossary_terms_snapshot(g)
    check("3a the snapshot keys by source",
          snap == {"甲": {"source": "甲", "translation": "A"},
                   "乙": {"source": "乙", "translation": "B"}},
          json.dumps(snap, ensure_ascii=False))
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        glossary.save(proj, {"terms": [
            {"source": "甲", "variants": [], "translation": "A",
             "alt_translations": [], "category": "other", "definition": ""},
            {"source": "乙", "variants": [], "translation": "B",
             "alt_translations": [], "category": "other", "definition": ""},
            {"source": "丙", "variants": [], "translation": "C",
             "alt_translations": [], "category": "other", "definition": ""},
        ]})
        # the in-memory copy changes 甲 and drops 乙, as a stage would
        live = {"terms": [{"source": "甲", "translation": "A2"},
                          {"source": "丙", "translation": "C"}],
                "retired": ["乙"]}
        pipeline._save_glossary_changes(proj, live, snap)
        out = glossary.load(proj)
        by_src = {t["source"]: t["translation"] for t in out["terms"]}
        check("3b the changed entry was written",
              by_src.get("甲") == "A2", json.dumps(by_src, ensure_ascii=False))
        check("3c the removed entry was not resurrected",
              "乙" not in by_src, json.dumps(by_src, ensure_ascii=False))
        check("3d the untouched entry kept the FILE's value",
              by_src.get("丙") == "C", json.dumps(by_src, ensure_ascii=False))


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    case_concurrent_edit_survives()
    case_concurrent_retirement_survives()
    case_unit_shapes()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
