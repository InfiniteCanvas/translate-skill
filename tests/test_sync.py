"""Tests for project.sync_manifest and project.backfill_frontmatter.

Covers the incremental-ingestion primitives behind the `sync` subcommand:
manifest ordering by parsed chapter number (0-based, written back into each
source file's frontmatter), status preservation by file name across
rebuilds, new-chapter pickup with status "pending" and recomputed orders,
entry removal when a source file disappears, and backfill_frontmatter's
novel-level key filling (existing per-chapter values win, empty novel-level
values are skipped), chapter_title derivation from the first non-empty body
line, and the changed-chapter return count.

Case 6 exercises the `sync` command itself: a chapter with unparseable YAML
frontmatter must surface as a clean CliError (exit 2), never a traceback.

All fixtures live in tempfile.TemporaryDirectory() sandboxes per case.
Self-contained PASS/FAIL script (no pytest). The lib modules and
scripts/translate.py import pyyaml, requests, ebooklib and pillow, so run
via uv (deps declared inline below):

    uv run tests/test_sync.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# lib/ and translate.py live at novel-translator/scripts relative to this
# file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import project  # noqa: E402
from translate import CliError, cmd_sync  # noqa: E402

PASSED = 0
FAILED: list[str] = []

NOVEL_TITLE = "测试小说"
NOVEL_AUTHOR = "测试作者"
NOVEL_URL = "https://example.com/novel"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def write_source(root: Path, name: str, text: str) -> None:
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    with open(source / name, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ---------------------------------------------------------------------- cases


def case_1_ordering() -> None:
    """Manifest order follows the parsed chapter number, not the file name."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_source(root, "Chapter_2.md", "line one\nline two\n")
        write_source(root, "Chapter_0010.md", "line one\nline two\n")
        write_source(root, "Chapter_1.md", "line one\nline two\n")

        manifest = project.sync_manifest(root)

        check("1a order: files sorted by parsed number, not name",
              [e["file"] for e in manifest]
              == ["Chapter_1.md", "Chapter_2.md", "Chapter_0010.md"],
              f"{[e['file'] for e in manifest]}")
        check("1b order: 0-based order values",
              [e["order"] for e in manifest] == [0, 1, 2],
              f"{[e['order'] for e in manifest]}")
        for entry in manifest:
            fm, _body = project.read_chapter(root / "source" / entry["file"])
            check(f"1c order: frontmatter order written to {entry['file']}",
                  fm.get("order") == entry["order"],
                  f"frontmatter order={fm.get('order')!r}")


def case_2_status_and_pickup() -> None:
    """Rebuilds preserve status by file name and pick up new files as pending."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_source(root, "Chapter_001.md", "first\nbody\n")
        write_source(root, "Chapter_002.md", "second\nbody\n")
        project.sync_manifest(root)

        manifest = project.load_manifest(root)
        project.set_status(manifest, "Chapter_001.md", "translated")
        project.save_manifest(root, manifest)

        write_source(root, "Chapter_3.md", "third\nbody\n")
        rebuilt = project.sync_manifest(root)

        first = project.find_entry(rebuilt, "Chapter_001.md")
        check("2a status: translated survives the rebuild",
              first is not None and first.get("status") == "translated",
              f"entry={first}")
        third = project.find_entry(rebuilt, "Chapter_3.md")
        check("2b pickup: new chapter enters the manifest",
              third is not None, f"files={[e['file'] for e in rebuilt]}")
        check("2c pickup: new chapter status is pending",
              third is not None and third.get("status") == "pending",
              f"status={third.get('status') if third else None!r}")
        check("2d order: orders recomputed across all entries",
              [e["order"] for e in rebuilt] == [0, 1, 2],
              f"{[e['order'] for e in rebuilt]}")
        if third is not None:
            fm, _body = project.read_chapter(root / "source" / third["file"])
            check("2e pickup: order written into the new chapter's frontmatter",
                  fm.get("order") == 2, f"frontmatter order={fm.get('order')!r}")


def case_3_removal() -> None:
    """Deleting a source file drops its manifest entry on the next rebuild."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_source(root, "Chapter_001.md", "first\nbody\n")
        write_source(root, "Chapter_002.md", "second\nbody\n")
        project.sync_manifest(root)

        (root / "source" / "Chapter_002.md").unlink()
        rebuilt = project.sync_manifest(root)

        check("3a removal: deleted file's entry is gone",
              project.find_entry(rebuilt, "Chapter_002.md") is None,
              f"files={[e['file'] for e in rebuilt]}")
        check("3b removal: surviving entry kept with its status",
              [(e["file"], e["status"]) for e in rebuilt]
              == [("Chapter_001.md", "pending")],
              f"{rebuilt}")


def case_4_backfill() -> None:
    """Novel-level keys are filled, per-chapter values win, chapter_title is
    derived from the first non-empty body line, and the return value is the
    changed-chapter count."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Bare chapter: heading marker stripped from the derived title.
        write_source(root, "Chapter_001.md", "# 第一章 灵根\n正文第一行\n")
        # Per-chapter source_url must survive the novel-level default.
        write_source(root, "Chapter_002.md",
                     "---\n"
                     "source_url: https://example.com/per-chapter\n"
                     "---\n"
                     "\n"
                     "第二章 筑基\nbody\n")
        # Existing chapter_title must not be touched.
        write_source(root, "Chapter_003.md",
                     "---\n"
                     "chapter_title: Existing Title\n"
                     "---\n"
                     "\n"
                     "第三章\nbody\n")
        # Fully populated chapter: nothing to change.
        write_source(root, "Chapter_004.md",
                     "---\n"
                     f"novel_title: {NOVEL_TITLE}\n"
                     f"author: {NOVEL_AUTHOR}\n"
                     f"source_url: {NOVEL_URL}\n"
                     "chapter_title: Complete\n"
                     "---\n"
                     "\n"
                     "第四章\nbody\n")

        chapters = project.discover(root)
        check("4a setup: all four chapters discovered", len(chapters) == 4,
              f"{[c.file for c in chapters]}")
        changed = project.backfill_frontmatter(
            chapters, NOVEL_TITLE, NOVEL_AUTHOR, NOVEL_URL)

        check("4b return: count equals the number of changed chapters",
              changed == 3, f"changed={changed}")
        fm1, _ = project.read_chapter(root / "source" / "Chapter_001.md")
        check("4c bare: novel-level keys filled",
              fm1.get("novel_title") == NOVEL_TITLE
              and fm1.get("author") == NOVEL_AUTHOR
              and fm1.get("source_url") == NOVEL_URL,
              f"frontmatter={fm1}")
        check("4d bare: chapter_title derived from the first body line",
              fm1.get("chapter_title") == "第一章 灵根",
              f"chapter_title={fm1.get('chapter_title')!r}")
        fm2, _ = project.read_chapter(root / "source" / "Chapter_002.md")
        check("4e own-value: per-chapter source_url kept",
              fm2.get("source_url") == "https://example.com/per-chapter",
              f"source_url={fm2.get('source_url')!r}")
        check("4f own-value: missing novel-level keys still filled",
              fm2.get("novel_title") == NOVEL_TITLE
              and fm2.get("author") == NOVEL_AUTHOR,
              f"frontmatter={fm2}")
        fm3, _ = project.read_chapter(root / "source" / "Chapter_003.md")
        check("4g existing: chapter_title untouched",
              fm3.get("chapter_title") == "Existing Title",
              f"chapter_title={fm3.get('chapter_title')!r}")


def case_5_empty_values_skipped() -> None:
    """Empty novel-level values are never written as empty keys."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_source(root, "Chapter_001.md", "第一章\nbody\n")
        changed = project.backfill_frontmatter(
            project.discover(root), "", NOVEL_AUTHOR, "")

        fm, _ = project.read_chapter(root / "source" / "Chapter_001.md")
        check("5a empty: empty novel-level values skipped",
              "novel_title" not in fm and "source_url" not in fm
              and fm.get("author") == NOVEL_AUTHOR,
              f"frontmatter={fm}")
        check("5b empty: chapter_title still derived, chapter counted changed",
              changed == 1 and fm.get("chapter_title") == "第一章",
              f"changed={changed}, chapter_title={fm.get('chapter_title')!r}")


def case_6_malformed_chapter_is_cli_error() -> None:
    """A chapter with unparseable frontmatter makes cmd_sync raise a clean
    CliError naming the file, not a raw ValueError traceback - sync reads
    every source file, and scraped batches routinely contain bad ones."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_source(root, "Chapter_001.md", "first\nbody\n")
        write_source(root, "Chapter_002.md",
                     "---\n"
                     "chapter_title: [unclosed\n"
                     "---\n"
                     "\n"
                     "body\n")
        (root / "chapters.json").write_text("[]\n", encoding="utf-8")

        try:
            cmd_sync(argparse.Namespace(), root)
            check("6a malformed: broken frontmatter raises CliError", False,
                  "cmd_sync returned without raising")
        except CliError as exc:
            check("6a malformed: broken frontmatter raises CliError",
                  "Chapter_002.md" in str(exc), f"message={exc}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_ordering()
    case_2_status_and_pickup()
    case_3_removal()
    case_4_backfill()
    case_5_empty_values_skipped()
    case_6_malformed_chapter_is_cli_error()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
