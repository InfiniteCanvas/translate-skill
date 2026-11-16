# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""What the book is missing, and which chapters look cut short.

    uv run tests/test_completeness.py
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


def write_chapter(root: Path, number: int, paragraphs: int = 90) -> Path:
    path = root / "source" / f"CHAPTER_{number:04d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"第{number}章的段落{n}。" for n in range(paragraphs))
    project.write_chapter(path, {"chapter_title": f"第{number}章"}, body)
    return path


def write_toc(root: Path, chapter_count: int, positions: list[int]) -> None:
    (root / project.SOURCE_TOC_FILE).write_text(
        json.dumps({
            "site": "novel543",
            "title": "书名",
            "chapter_count": chapter_count,
            "chapters": [{"position": p, "title": f"第{p}章"} for p in positions],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def case_expected_from_toc() -> None:
    """The source's own table of contents states how many chapters there are."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for number in (1, 2, 3):
            write_chapter(root, number)
        write_toc(root, 5, [1, 2, 3, 4, 5])
        report = project.completeness_report(root, {})
        check("the expected count comes from the table of contents",
              report["expected"] == 5, repr(report))
        check("the present count is the project's chapters",
              report["present"] == 3, repr(report))


def case_missing_positions() -> None:
    """The two kinds of missing are kept apart."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for number in (1, 2, 3):
            write_chapter(root, number)
        # the source says 5 chapters and lists 1, 2, 4, 5 - position 3 was
        # never published, and positions 4 and 5 the acquisition never fetched
        write_toc(root, 5, [1, 2, 4, 5])
        report = project.completeness_report(root, {})
        check("the positions the source does not list are named",
              report["missing_from_toc"] == [3], repr(report))
        check("the positions the project lacks are named",
              report["missing_from_project"] == [4, 5], repr(report))


def case_operator_entered_count() -> None:
    """Without a table of contents, an operator-entered count stands in."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for number in (1, 2):
            write_chapter(root, number)
        report = project.completeness_report(root, {"expected_chapters": 4})
        check("the operator's count is the expected count",
              report["expected"] == 4, repr(report))
        check("the missing positions are named",
              report["missing_from_project"] == [3, 4], repr(report))


def case_no_count_means_no_claim() -> None:
    """Nobody has said how many chapters there are: no shortfall is claimed."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_chapter(root, 1)
        report = project.completeness_report(root, {})
        check("no expected count is claimed", report["expected"] is None, repr(report))
        check("nothing is reported missing",
              report["missing_from_toc"] == [] and report["missing_from_project"] == [],
              repr(report))


def case_outlier() -> None:
    """A chapter far shorter than the book is reported with the median."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for number in (1, 2, 3):
            write_chapter(root, number, 90)
        write_chapter(root, 4, 10)
        report = project.completeness_report(root, {"completeness_outlier_ratio": 0.3})
        check("the short chapter is reported",
              [o["chapter"] for o in report["outliers"]] == ["CHAPTER_0004.md"],
              repr(report["outliers"]))
        check("the report names the book-wide figure",
              report["outliers"][0]["median"] == 90
              and report["outliers"][0]["paragraphs"] == 10, repr(report))


def case_healthy_book_is_quiet() -> None:
    """A healthy book reports no outlier."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for number in range(1, 6):
            write_chapter(root, number, 90 if number < 5 else 33)
        report = project.completeness_report(root, {"completeness_outlier_ratio": 0.3})
        check("a book with a short-but-normal chapter reports no outlier",
              report["outliers"] == [], repr(report["outliers"]))


def case_report_is_written() -> None:
    """The report lands where the operator's tools read it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_chapter(root, 1)
        report = project.completeness_report(root, {})
        path = project.write_completeness_report(root, report)
        check("the report is written to logs/completeness.json",
              path == root / "logs" / "completeness.json" and path.is_file(), str(path))
        loaded = json.loads(path.read_text(encoding="utf-8"))
        check("the written report is the report",
              loaded["present"] == report["present"], repr(loaded))


def case_missing_toc_is_not_an_error() -> None:
    """An unreadable table of contents is no finding, not a crash."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_chapter(root, 1)
        (root / project.SOURCE_TOC_FILE).write_text("{not json", encoding="utf-8")
        report = project.completeness_report(root, {})
        check("a broken table of contents yields no expected count",
              report["expected"] is None, repr(report))


def main() -> int:
    for case in (case_expected_from_toc, case_missing_positions,
                 case_operator_entered_count, case_no_count_means_no_claim,
                 case_outlier, case_healthy_book_is_quiet, case_report_is_written,
                 case_missing_toc_is_not_an_error):
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
