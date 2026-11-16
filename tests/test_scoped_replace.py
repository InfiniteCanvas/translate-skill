"""The source-scoped glossary rewrite.

    uv run tests/test_scoped_replace.py

A glossary entry's rendering can be shared by two terms whose source words
are one character apart, and a rewrite that matched the English string
alone renamed both of them - the protagonist, in 128 chapters and 1,905
occurrences. The scope is the SOURCE: a translated paragraph is only
rewritten when the source paragraph it is paired with carries the term.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary, project, replace

PASSED = 0
FAILED: list[str] = []

# Two chapters. The term 姜艳艳 (the stepsister) appears in chapter 1 only;
# the English rendering "Jiang Yanyan" appears in chapter 1 AND in chapter 2,
# where the source carries a DIFFERENT term (姜妍妍, the protagonist).
SOURCE_1 = """---
chapter_title: 第1章 开始
---
姜艳艳站在门口。
他看着姜妍妍离开。
姜艳艳笑了笑。
"""
SOURCE_2 = """---
chapter_title: 第2章 继续
---
姜妍妍走进院子。
她想起姜艳艳说过的话。
"""
TRANSLATED_1 = """---
title: Chapter 1
---
Jiang Yanyan stood in the doorway.
He watched Jiang Yanyan leave.
Jiang Yanyan smiled.
"""
TRANSLATED_2 = """---
title: Chapter 2
---
Jiang Yanyan walked into the yard.
She remembered what Jiang Yanyan had said.
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def make_project(root: Path) -> None:
    for name in ("source", "translated", "draft", "notes"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "source" / "CHAPTER_0001.md").write_text(SOURCE_1, encoding="utf-8")
    (root / "source" / "CHAPTER_0002.md").write_text(SOURCE_2, encoding="utf-8")
    (root / "translated" / "CHAPTER_0001.md").write_text(TRANSLATED_1, encoding="utf-8")
    (root / "translated" / "CHAPTER_0002.md").write_text(TRANSLATED_2, encoding="utf-8")
    (root / "chapters.json").write_text(
        json.dumps([{"file": "CHAPTER_0001.md", "number": 1, "order": 0,
                     "status": "translated"},
                    {"file": "CHAPTER_0002.md", "number": 2, "order": 1,
                     "status": "translated"}], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (root / "glossary.json").write_text(
        json.dumps({"terms": [
            {"source": "姜艳艳", "translation": "Jiang Yanyan", "variants": [],
             "alt_translations": [], "definition": "the stepsister",
             "category": "person", "origin": "model"},
            {"source": "姜妍妍", "translation": "Jiang Yanyan", "variants": [],
             "alt_translations": [], "definition": "the protagonist",
             "category": "person", "origin": "model"},
        ]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def body_of(path: Path) -> list[str]:
    _, body = project.read_chapter(path)
    return body.split("\n")


def case_the_scope_is_the_source() -> None:
    """Only the paragraphs whose source carries the term are rewritten."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_project(root)
        manifest = project.load_manifest(root)
        result = replace.glossary_replace(
            root, "姜艳艳", "Jiang Yan-yan", dry_run=True)
        check("the dry run changes nothing on disk",
              "Jiang Yanyan stood" in (root / "translated" / "CHAPTER_0001.md").read_text(
                  encoding="utf-8"))
        check("the dry run reports the paragraphs it would rewrite",
              result["chapters"]["paragraphs"] == 3, repr(result["chapters"]))
        check("the scope reaches the paragraphs that carry the term",
              result["chapters"]["per_chapter"]
              == [("CHAPTER_0001.md", 2), ("CHAPTER_0002.md", 1)],
              repr(result["chapters"]["per_chapter"]))


def case_the_rewrite_lands_only_where_the_source_carries_it() -> None:
    """The applied rewrite leaves the other term's rendering alone."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_project(root)
        replace.glossary_replace(root, "姜艳艳", "Jiang Yan-yan")
        first = body_of(root / "translated" / "CHAPTER_0001.md")
        second = body_of(root / "translated" / "CHAPTER_0002.md")
        check("the paragraphs that carry the term are rewritten",
              first[0] == "Jiang Yan-yan stood in the doorway."
              and first[2] == "Jiang Yan-yan smiled.", repr(first))
        check("the paragraph whose source carries the OTHER term is not",
              first[1] == "He watched Jiang Yanyan leave.", repr(first))
        check("the paragraph whose source carries the OTHER term is not "
              "rewritten, in the other chapter either",
              second[0] == "Jiang Yanyan walked into the yard.", repr(second))
        check("the paragraph whose source carries the term is rewritten "
              "wherever it is",
              second[1] == "She remembered what Jiang Yan-yan had said.",
              repr(second))
        entry = glossary.find(glossary.load(root), "姜艳艳")
        check("the term's pin is the new rendering",
              entry["translation"] == "Jiang Yan-yan", repr(entry))


def case_the_raw_phrase_command_is_unchanged() -> None:
    """`util replace` still matches the phrase wherever it sits."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_project(root)
        manifest = project.load_manifest(root)
        result = replace.replace_chapters(
            root, manifest, "Jiang Yanyan", "Jiang Yan-yan", dry_run=True)
        check("the raw replacement still reaches every chapter",
              [name for name, _count in result["per_chapter"]]
              == ["CHAPTER_0001.md", "CHAPTER_0002.md"], repr(result["per_chapter"]))


def case_an_unpairable_chapter_is_reported() -> None:
    """A chapter whose two sides differ in length is reported, not guessed."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_project(root)
        (root / "translated" / "CHAPTER_0002.md").write_text(
            "---\ntitle: Chapter 2\n---\nJiang Yanyan walked into the yard.\n",
            encoding="utf-8")
        manifest = project.load_manifest(root)
        result = replace.scoped_replace_chapters(
            root, manifest, "Jiang Yanyan", "Jiang Yan-yan",
            ["姜艳艳"], dry_run=True)
        check("the chapter is reported as unpairable",
              result["unpairable"] == ["CHAPTER_0002.md"], repr(result))
        check("the other chapter is still rewritten",
              result["paragraphs"] == 2, repr(result))


def case_a_variant_counts_as_the_term() -> None:
    """A spelling of the term in the source counts too."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_project(root)
        (root / "source" / "CHAPTER_0001.md").write_text(
            SOURCE_1.replace("姜艳艳", "姜艷艷"), encoding="utf-8")
        manifest = project.load_manifest(root)
        result = replace.scoped_replace_chapters(
            root, manifest, "Jiang Yanyan", "Jiang Yan-yan",
            ["姜艳艳", "姜艷艷"], dry_run=True)
        check("the variant's paragraphs are in scope",
              result["paragraphs"] == 3, repr(result))


def main() -> int:
    for case in (case_the_scope_is_the_source,
                 case_the_rewrite_lands_only_where_the_source_carries_it,
                 case_the_raw_phrase_command_is_unchanged,
                 case_an_unpairable_chapter_is_reported,
                 case_a_variant_counts_as_the_term):
        case()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
