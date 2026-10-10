"""Tests for the tightened chapter-naming rule and near-miss reporting.

project.CHAPTER_RE is now the 4-digit-only pattern below -- padding is fixed at
exactly 4 digits AND the letter suffix is gone, so "CHAPTER_0001.md" is the
only accepted spelling. That closes the padding ambiguity where
"CHAPTER_001.md" and "CHAPTER_0001.md" were both admitted and both claimed
chapter 1, and drops the extras/bonus-chapter spelling nobody used.

Tightening alone would have turned a recoverable collision into silent data
loss: a name that stops matching produces no manifest entry and no signal.
So discovery stays quiet about non-matching files (source/ legitimately holds
more than chapters) while project.near_miss_reason() classifies the ones that
look like intended chapters, and `init`/`sync` print a [warn] naming each. The
classes covered are exactly those references/ingestion.md documents as
silently-ignored: CHAPTER_0007.zh.md, chapter 7.md, 0007.md, and non-ASCII
digits -- plus the short-padded names the tightened bound now rejects.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_near_miss.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import project
from translate import cmd_sync

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


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


NEAR_MISS_CASES = [
    ("CHAPTER_001.md", "3 digits, not 4", True),
    ("CHAPTER_12.md", "2 digits, not 4", True),
    ("CHAPTER_12345.md", "5 digits, not 4", True),
    ("CHAPTER_0007.zh.md", "not CHAPTER_NNNN.md", True),
    ("chapter 7.md", "not CHAPTER_NNNN.md", True),
    ("0007.md", "no CHAPTER_ prefix", True),
    ("CHAPTER_０００７.md", "non-ASCII digits", True),
    ("CHAPTER_٠٠٠７.md", "non-ASCII digits", True),
    ("CHAPTER_0042a.md", "letter suffix", True),
    ("CHAPTER_0007xy.md", "not CHAPTER_NNNN.md", True),
    ("CHAPTER_0007x.md", "letter suffix", True),
    ("CHAPTER_0007.txt", "extension", True),
    ("README.md", None, False),
    ("notes.txt", None, False),
    (".gitkeep", None, False),
    ("CHAPTER_0007.md", None, False),
    ("chapter_0012.md", None, False),
]


def case_1_near_miss_reason() -> None:
    """near_miss_reason classifies each documented near-miss class and stays
    silent for files that plainly are not chapters."""
    for name, needle, flagged in NEAR_MISS_CASES:
        reason = project.near_miss_reason(name)
        if flagged:
            ok = reason is not None and (needle is None or needle in reason)
            check(f"1 {name}: flagged ({reason!r})", ok, f"reason={reason!r}")
        else:
            check(f"1 {name}: not flagged", reason is None, f"reason={reason!r}")


def case_2_ignored_chapters_lists_sorted() -> None:
    """ignored_chapters returns (name, reason) sorted by name, skips
    non-chapters, and is empty when source/ does not exist."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check("2a missing source/: no ignored chapters",
              project.ignored_chapters(root) == [],
              f"got={project.ignored_chapters(root)!r}")
        source = root / "source"
        source.mkdir(parents=True)
        for name, _n, flagged in NEAR_MISS_CASES:
            write_lf(source / name, "body\n")
        found = project.ignored_chapters(root)
        names = [n for n, _r in found]
        check("2b ignored_chapters: sorted by name", names == sorted(names),
              f"names={names!r}")
        check("2c ignored_chapters: README.md never reported",
              "README.md" not in names, f"names={names!r}")
        check("2d ignored_chapters: real chapters never reported",
              "CHAPTER_0007.md" not in names and "chapter_0012b.md" not in names,
              f"names={names!r}")
        check("2e ignored_chapters: 3-digit name reported",
              "CHAPTER_001.md" in names, f"names={names!r}")
        check("2f every entry carries a non-empty reason",
              all(r for _n, r in found), f"found={found!r}")


def case_3_sync_warns() -> None:
    """cmd_sync prints one [warn] per ignored near-miss plus a count line, and
    still exits 0 -- a stray file is a warning, never a failure."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "source"
        source.mkdir(parents=True)
        write_lf(source / "CHAPTER_0001.md", "第一章\nbody\n")
        write_lf(source / "CHAPTER_001.md", "第一章\nbody\n")
        write_lf(source / "CHAPTER_0007.zh.md", "第七章\nbody\n")
        write_lf(source / "README.md", "notes\n")
        project.sync_manifest(root)

        buf = io.StringIO()
        error: BaseException | None = None
        try:
            with contextlib.redirect_stdout(buf):
                rc = cmd_sync(argparse.Namespace(), root)
        except BaseException as exc:
            error = exc
            rc = None
        out = buf.getvalue()

        check("3a sync: exits 0", error is None and rc == 0,
              f"rc={rc} error={error!r}")
        check("3b sync: no exception raised", error is None,
              f"{type(error).__name__}: {error}" if error else "")
        check("3c sync: warns about the 3-digit name",
              "[warn] source/CHAPTER_001.md: ignored" in out, f"out={out!r}")
        check("3d sync: warns about the .zh near-miss",
              "[warn] source/CHAPTER_0007.zh.md: ignored" in out, f"out={out!r}")
        check("3e sync: prints the ignored count",
              "[warn] 2 source file(s) look like chapters" in out, f"out={out!r}")
        check("3f sync: does not warn about README.md",
              "README.md" not in out, f"out={out!r}")
        check("3g sync: still reports the manifest",
              "[ok] manifest: 1 chapter(s)" in out, f"out={out!r}")
        check("3h sync: no traceback in output", "Traceback" not in out)


def case_4_tightened_bound_end_to_end() -> None:
    """A short-padded file never reaches the manifest, so exactly one chapter
    is counted -- the padding ambiguity is closed at the storage layer."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "source"
        source.mkdir(parents=True)
        write_lf(source / "CHAPTER_0001.md", "第一章\nbody\n")
        write_lf(source / "CHAPTER_001.md", "第一章\nbody\n")
        manifest = project.sync_manifest(root)
        files = [e["file"] for e in manifest]
        check("4a manifest holds only the 4-digit chapter",
              files == ["CHAPTER_0001.md"], f"files={files!r}")
        check("4b the short-padded twin is not discovered",
              {c.file for c in project.discover(root)} == {"CHAPTER_0001.md"},
              f"found={[c.file for c in project.discover(root)]!r}")


def main() -> int:
    print("--- case 1: near_miss_reason ---")
    case_1_near_miss_reason()
    print("--- case 2: ignored_chapters ---")
    case_2_ignored_chapters_lists_sorted()
    print("--- case 3: sync warns ---")
    case_3_sync_warns()
    print("--- case 4: tightened bound ---")
    case_4_tightened_bound_end_to_end()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
