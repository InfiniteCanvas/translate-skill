# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""The source repairs ingestion makes, and the one it must not make.

    uv run tests/test_source_repairs.py
"""
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import project

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


def write_chapter(root: Path, name: str, title: str, body: str) -> Path:
    path = root / "source" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {"chapter_title": title}
    project.write_chapter(path, frontmatter, body)
    return path


def body_of(path: Path) -> str:
    _, body = project.read_chapter(path)
    return body


def case_heading_prefix() -> None:
    """A first paragraph carrying the chapter heading is repaired."""
    frontmatter = {"chapter_title": "第135章 不甘"}
    body = "第135章 不甘杨浩宇一心盯着旁人的成就。\n第二段。"
    repaired, notes = project.repair_source_body(frontmatter, body)
    check("the heading prefix is stripped",
          repaired == "杨浩宇一心盯着旁人的成就。\n第二段。", repr(repaired))
    check("the repair is reported", len(notes) == 1, repr(notes))


def case_title_alone() -> None:
    """A first paragraph that IS the title is the pipeline's case, not ours."""
    frontmatter = {"chapter_title": "第135章 不甘"}
    repaired, notes = project.repair_source_body(frontmatter, "第135章 不甘\n第二段。")
    check("a line equal to the title is left to the pipeline",
          repaired == "第135章 不甘\n第二段。", repr(repaired))
    check("no repair is reported for it", notes == [], repr(notes))


def case_manufactured_title() -> None:
    """A prose line wearing a manufactured title must not be deleted.

    The skill's own frontmatter backfill derives a missing chapter_title
    from the chapter's WHOLE first body line, so a prose line can arrive
    with a title equal to itself. Stripping the title would delete the
    paragraph.
    """
    prose = "他不知道这句话该怎么接下去，只是沉默了很久。"
    frontmatter = {"chapter_title": prose}
    repaired, notes = project.repair_source_body(frontmatter, prose + "\n第二段。")
    check("a prose line is never deleted", repaired == prose + "\n第二段。", repr(repaired))
    check("no repair is reported", notes == [], repr(notes))


def case_non_heading_title() -> None:
    """A title that is not heading-shaped leaves the chapter alone."""
    frontmatter = {"chapter_title": "山边小村"}
    body = "山边小村\n第二段。"
    repaired, notes = project.repair_source_body(frontmatter, body)
    check("a non-heading title is left alone", repaired == body, repr(repaired))
    check("no repair is reported", notes == [], repr(notes))


def case_english_heading() -> None:
    """The rule knows an English heading too."""
    frontmatter = {"chapter_title": "Chapter 5: Decorations"}
    repaired, notes = project.repair_source_body(
        frontmatter, "Chapter 5: DecorationsThe lamps were lit.\nSecond.")
    check("an English heading prefix is stripped",
          repaired == "The lamps were lit.\nSecond.", repr(repaired))
    check("the repair is reported", len(notes) == 1, repr(notes))


def case_only_the_first_paragraph() -> None:
    """A speech that runs across paragraphs keeps its bare middle pieces."""
    frontmatter = {"chapter_title": "第135章 不甘"}
    body = "“你别太大意。\n高橋生一就是栽在她手里的。\n绝对不是普通的小姑娘。”"
    repaired, notes = project.repair_source_body(frontmatter, body)
    check("a speech's pieces are untouched", repaired == body, repr(repaired))
    check("no repair is reported", notes == [], repr(notes))


def case_idempotent() -> None:
    """A second run over a repaired body finds nothing."""
    frontmatter = {"chapter_title": "第135章 不甘"}
    body = "第135章 不甘杨浩宇一心盯着旁人的成就。\n第二段。"
    once, notes = project.repair_source_body(frontmatter, body)
    twice, again = project.repair_source_body(frontmatter, once)
    check("the repair is idempotent", once == twice and again == [], repr((once, twice, again)))
    check("the first run reported the repair", len(notes) == 1, repr(notes))


def case_duplicated_opening() -> None:
    """A repeated opening block is removed, and reported by paragraph."""
    frontmatter = {"chapter_title": "第1章 寻宝游戏"}
    body = "\n".join(["开头一。", "开头二。", "开头三。", "正文。",
                      "开头一。", "开头二。", "开头三。", "正文之后。"])
    repaired, notes = project.repair_source_body(frontmatter, body)
    check("the later occurrence is removed",
          repaired == "\n".join(["开头一。", "开头二。", "开头三。", "正文。", "正文之后。"]),
          repr(repaired))
    check("the removal is reported with the paragraphs",
          len(notes) == 1 and "paragraph" in notes[0], repr(notes))


def case_short_repeat_kept() -> None:
    """A one-line repeat is prose, not a duplicated opening."""
    frontmatter = {"chapter_title": "第1章 寻宝游戏"}
    body = "\n".join(["耐久度:100", "正文。", "耐久度:100", "正文之后。"])
    repaired, notes = project.repair_source_body(frontmatter, body)
    check("a short repeat is kept", repaired == body, repr(repaired))
    check("no repair is reported", notes == [], repr(notes))


def case_translated_chapter_untouched() -> None:
    """A chapter that already has a translation is reported, not rewritten."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = write_chapter(root, "CHAPTER_0001.md", "第135章 不甘",
                              "第135章 不甘杨浩宇。\n第二段。")
        translated = root / "translated" / "CHAPTER_0001.md"
        translated.parent.mkdir(parents=True, exist_ok=True)
        translated.write_text("Translation.\n", encoding="utf-8")
        before = body_of(path)
        records = project.repair_source_chapters(
            root, project.discover(root))
        check("the source of a translated chapter is left alone",
              body_of(path) == before, repr(body_of(path)))
        check("the chapter is reported instead",
              len(records) == 1 and records[0]["translated"] is True
              and records[0]["repaired"] is False, repr(records))


def case_untranslated_chapter_repaired() -> None:
    """A chapter with no translation is repaired in place."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = write_chapter(root, "CHAPTER_0001.md", "第135章 不甘",
                              "第135章 不甘杨浩宇。\n第二段。")
        records = project.repair_source_chapters(
            root, project.discover(root))
        check("the source is repaired",
              body_of(path) == "杨浩宇。\n第二段。", repr(body_of(path)))
        check("the repair is reported",
              len(records) == 1 and records[0]["repaired"] is True, repr(records))
        again = project.repair_source_chapters(root, project.discover(root))
        check("a second pass finds nothing", again == [], repr(again))


def main() -> int:
    for case in (case_heading_prefix, case_title_alone, case_manufactured_title,
                 case_non_heading_title, case_english_heading,
                 case_only_the_first_paragraph, case_idempotent,
                 case_duplicated_opening, case_short_repeat_kept,
                 case_translated_chapter_untouched,
                 case_untranslated_chapter_repaired):
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
