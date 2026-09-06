"""Tests for epub.chapter_md_to_xhtml's notes sidecar path.

Covers parity between the legacy baked-in format ([^N] markers + a
"## Translator's Notes" section parsed back out of the markdown) and the
notes/<chapter>.json sidecar: identical bodies must render identical
(title, xhtml, has_notes) triples. Sidecar rendering specifics: epub3
noteref/footnote markup (epub:type="noteref" href="#tn-N" anchors and
epub:type="footnote" asides), tn-1/tn-2 numbering across multiple notes;
anchor re-resolution (a stored line index that no longer matches its
anchor re-locates the paragraph by anchor, output identical to the correct
index); dropped notes (out-of-range line + anchor found nowhere -> no
noteref, has_notes False); defensive stripping of stray legacy markers in
the markdown when a sidecar is passed (no duplicate noterefs); and
notes=None on a clean file (no footnote markup at all).

All chapter fixtures are built inside tempfile.TemporaryDirectory()
sandboxes per case — repo fixtures are never touched. Files are written with
explicit LF newlines so byte-level comparisons are deterministic.

Self-contained PASS/FAIL script (no pytest). epub.py imports ebooklib, so
run via uv (deps declared inline below):

    uv run tests/test_epub_notes.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["ebooklib>=0.18", "pyyaml>=6.0"]
# ///
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import epub  # noqa: E402

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


FRONTMATTER = (
    "---\n"
    "chapter_title: 第一章 灵根\n"
    "title: Spirit Root Awakening\n"
    "---\n"
    "\n"
)


def make_chapter(td: str, name: str, body: str) -> Path:
    path = Path(td) / name
    write_lf(path, FRONTMATTER + body)
    return path


NOTE_TERM = "筑基"
NOTE_TEXT = "Foundation Establishment is the second realm of cultivation."
# The legacy definition line and the sidecar note must produce the same
# definition text: "**term** — note".
LEGACY_DEFINITION = f"[^1]: **{NOTE_TERM}** — {NOTE_TEXT}"


# ---------------------------------------------------------------------- cases


def case_1_parity() -> None:
    """Legacy baked-in notes and the sidecar render identically."""
    with tempfile.TemporaryDirectory() as td:
        legacy = make_chapter(
            td, "legacy.md",
            "Para one.[^1]\n"
            "\n"
            "Para two.\n"
            "\n"
            "## Translator's Notes\n"
            "\n"
            + LEGACY_DEFINITION + "\n",
        )
        clean = make_chapter(td, "clean.md", "Para one.\n\nPara two.\n")
        sidecar = [{"line": 0, "term": NOTE_TERM, "note": NOTE_TEXT,
                    "anchor": "Para one."}]

        legacy_out = epub.chapter_md_to_xhtml(legacy)
        sidecar_out = epub.chapter_md_to_xhtml(clean, notes=sidecar)

        check("1a parity: (title, xhtml, has_notes) identical",
              legacy_out == sidecar_out,
              f"legacy={legacy_out!r}\nsidecar={sidecar_out!r}")
        check("1b parity: both report notes present and the chapter title",
              legacy_out[0] == "Spirit Root Awakening"
              and legacy_out[2] is True and sidecar_out[2] is True,
              f"legacy={legacy_out[0]!r},{legacy_out[2]!r}")
        check("1c parity: noteref and footnote markup present in both",
              'epub:type="noteref" href="#tn-1"' in legacy_out[1]
              and '<aside epub:type="footnote"' in legacy_out[1],
              "")


def case_2_multiple_notes() -> None:
    """Sidecar path: two notes -> tn-1/tn-2 anchors and asides, in order."""
    with tempfile.TemporaryDirectory() as td:
        path = make_chapter(td, "chapter.md",
                            "Para one.\n\nPara two.\n\nPara three.\n")
        notes = [
            {"line": 0, "term": "筑基", "note": "First note.", "anchor": "Para one."},
            {"line": 2, "term": "灵石", "note": "Second note.", "anchor": "Para two."},
        ]
        _title, xhtml, has_notes = epub.chapter_md_to_xhtml(path, notes=notes)

        check("2a multi: has_notes true", has_notes is True, "")
        check("2b multi: tn-1 and tn-2 noterefs emitted",
              'epub:type="noteref" href="#tn-1"' in xhtml
              and 'epub:type="noteref" href="#tn-2"' in xhtml, "")
        check("2c multi: tn-1 and tn-2 footnotes emitted",
              '<aside epub:type="footnote" role="doc-footnote" id="tn-1">' in xhtml
              and '<aside epub:type="footnote" role="doc-footnote" id="tn-2">' in xhtml,
              "")
        check("2d multi: asides ordered tn-1 before tn-2",
              xhtml.index('id="tn-1"') < xhtml.index('id="tn-2"'), "")
        paras = xhtml.split("<p>")
        para_one = next(p for p in paras if p.startswith("Para one."))
        para_two = next(p for p in paras if p.startswith("Para two."))
        check("2e multi: tn-1 attached to the owning paragraph",
              'href="#tn-1"' in para_one, f"para_one={para_one!r}")
        check("2f multi: tn-2 attached to its own paragraph",
              'href="#tn-2"' in para_two and 'href="#tn-1"' not in para_two,
              f"para_two={para_two!r}")
        check("2g multi: footnote text carries term and note",
              "**筑基**" not in xhtml and "<strong>筑基</strong> — First note." in xhtml,
              "")


def case_3_anchor_resolution() -> None:
    """A stale line index re-resolves via the anchor: identical output."""
    with tempfile.TemporaryDirectory() as td:
        path = make_chapter(td, "chapter.md",
                            "Para one.\n\nPara two.\n\nPara three.\n")
        correct = [{"line": 2, "term": "灵石", "note": "Spirit stones.",
                    "anchor": "Para two."}]
        # index deliberately off (points at Para one), anchor still valid
        off_by_one = [{"line": 0, "term": "灵石", "note": "Spirit stones.",
                       "anchor": "Para two."}]

        correct_out = epub.chapter_md_to_xhtml(path, notes=correct)
        shifted_out = epub.chapter_md_to_xhtml(path, notes=off_by_one)

        check("3a anchor: off-by-one index re-resolved to the same paragraph",
              shifted_out == correct_out,
              f"shifted={shifted_out!r}\ncorrect={correct_out!r}")
        check("3b anchor: note lands on 'Para two.', not 'Para one.'",
              'href="#tn-1"' in correct_out[1]
              and 'href="#tn-1"' not in next(
                  p for p in correct_out[1].split("<p>")
                  if p.startswith("Para one.")),
              "")


def case_4_dropped_note() -> None:
    """Out-of-range line + anchor found nowhere -> note dropped cleanly."""
    with tempfile.TemporaryDirectory() as td:
        path = make_chapter(td, "chapter.md",
                            "Para one.\n\nPara two.\n")
        notes = [
            {"line": 99, "term": "灵石", "note": "Spirit stones.",
             "anchor": "NO SUCH LINE ANYWHERE"},
        ]
        title, xhtml, has_notes = epub.chapter_md_to_xhtml(path, notes=notes)

        check("4a dropped: has_notes false", has_notes is False, "")
        check("4b dropped: no noteref in the output",
              "noteref" not in xhtml, f"xhtml={xhtml!r}")
        check("4c dropped: no footnote aside / rule in the output",
              "aside" not in xhtml and "<hr/>" not in xhtml, "")


def case_5_stray_markers() -> None:
    """Stray legacy markers in the markdown are stripped when a sidecar is
    passed -- exactly one noteref per note, no duplicates."""
    with tempfile.TemporaryDirectory() as td:
        clean = make_chapter(td, "clean.md", "Para one.[^7]\n\nPara two.\n")
        # same file content, but written again for the independent call:
        # the marker must vanish while the sidecar owns the numbering
        notes = [{"line": 0, "term": NOTE_TERM, "note": NOTE_TEXT,
                  "anchor": "Para one."}]
        title, xhtml, has_notes = epub.chapter_md_to_xhtml(clean, notes=notes)

        check("5a stray: markers stripped from the body",
              "[^" not in xhtml, f"xhtml={xhtml!r}")
        noteref_count = xhtml.count('epub:type="noteref"')
        check("5b stray: exactly one noteref (sidecar owns numbering)",
              noteref_count == 1, f"count={noteref_count}")
        check("5c stray: noteref is tn-1, not the stray tn-7",
              'href="#tn-1"' in xhtml and "#tn-7" not in xhtml, "")

        # Equivalence: stray-marker file + sidecar == marker-free file + sidecar
        pristine = make_chapter(td, "pristine.md", "Para one.\n\nPara two.\n")
        pristine_out = epub.chapter_md_to_xhtml(pristine, notes=notes)
        check("5d stray: output identical to the marker-free file",
              (title, xhtml, has_notes) == pristine_out, "")


def case_6_legacy_path_on_clean_file() -> None:
    """notes=None on a clean file: plain chapter, no footnote markup."""
    with tempfile.TemporaryDirectory() as td:
        path = make_chapter(td, "chapter.md", "Para one.\n\nPara two.\n")
        title, xhtml, has_notes = epub.chapter_md_to_xhtml(path)

        check("6a legacy-clean: has_notes false", has_notes is False, "")
        check("6b legacy-clean: title from frontmatter, body rendered",
              title == "Spirit Root Awakening"
              and "<p>Para one.</p>" in xhtml and "<p>Para two.</p>" in xhtml,
              f"title={title!r}, xhtml={xhtml!r}")
        check("6c legacy-clean: no footnote markup at all",
              "noteref" not in xhtml and "aside" not in xhtml
              and "<hr/>" not in xhtml, "")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_parity()
    case_2_multiple_notes()
    case_3_anchor_resolution()
    case_4_dropped_note()
    case_5_stray_markers()
    case_6_legacy_path_on_clean_file()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
