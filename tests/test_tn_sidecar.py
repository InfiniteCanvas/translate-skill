"""Tests for lib/tn.py notes-sidecar IO + strip_marked_notes, and
lib/assemble.py's clean-output contract.

Covers save_notes/load_notes round-trip (document shape chapter/updated_at/
notes, note keys exactly {line, term, note, anchor} with a model-supplied
threshold dropped, anchor snapshotted from the stripped line and capped at
80 chars, trailing newline, ensure_ascii=False so CJK stays readable in the
raw bytes); invalid-entry dropping (non-dict, negative line, line == len,
bool line, non-str/empty term/note); the empty-kept-list rule (all-invalid
or notes=[] DELETES the sidecar, pre-existing file included); load_notes
leniency (missing file, malformed JSON, notes not a list, non-dict notes
entries -> []); strip_marked_notes (full legacy body, stacked [^1][^2]
markers, markers with no section, section with no markers, clean-body
passthrough, whitespace-tolerant definition lines); and assemble.assemble
writing clean markdown (no markers, no TN section, title in frontmatter,
body lines verbatim via project.read_chapter).

All chapter fixtures are built inside tempfile.TemporaryDirectory()
sandboxes per case — repo fixtures are never touched. Files are written with
explicit LF newlines so byte-level comparisons are deterministic.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_tn_sidecar.py
"""

import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import assemble, project  # noqa: E402
from lib import tn  # noqa: E402

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


# ---------------------------------------------------------------------- cases


def case_1_round_trip() -> None:
    """save_notes + load_notes: document shape, anchor, encoding, newline."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        long_line = "A very long paragraph line. " * 5  # > 80 chars stripped
        lines = [
            "  He swept the tombs at Qingming.  ",
            long_line,
            "Line two.",
        ]
        notes = [
            {"line": 0, "term": "清明", "note": "Tomb-sweeping festival.",
             "threshold": "high"},  # model note: threshold must NOT persist
            {"line": 1, "term": "灵石", "note": "Spirit stones: currency and fuel."},
        ]
        kept = tn.save_notes(root, "Chapter_0001.md", lines, notes)

        check("1a round-trip: kept has exactly the two valid notes",
              len(kept) == 2, f"kept={kept}")
        check("1b round-trip: note keys exactly {line, term, note, anchor}",
              all(set(note) == {"line", "term", "note", "anchor"} for note in kept),
              f"keys={[sorted(n) for n in kept]}")
        check("1c round-trip: model-supplied 'threshold' dropped",
              all("threshold" not in note for note in kept), "")
        check("1d round-trip: anchor is the stripped line",
              kept[0]["anchor"] == "He swept the tombs at Qingming.",
              f"anchor={kept[0]['anchor']!r}")
        check("1e round-trip: anchor capped at 80 chars",
              kept[1]["anchor"] == long_line.strip()[:80],
              f"len={len(kept[1]['anchor'])}")

        path = tn.notes_path(root, "Chapter_0001.md")
        check("1f round-trip: sidecar at notes/<stem>.json",
              path == root / "notes" / "Chapter_0001.json" and path.is_file(),
              f"path={path}")
        raw = path.read_bytes()
        check("1g round-trip: file ends with a trailing newline",
              raw.endswith(b"\n"), f"tail={raw[-20:]!r}")
        text = raw.decode("utf-8")
        check("1h round-trip: ensure_ascii=False keeps the CJK term in raw bytes",
              "清明" in text and "\\u" not in text, "")

        document = json.loads(text)
        check("1i round-trip: document shape {chapter, updated_at, notes}",
              set(document) == {"chapter", "updated_at", "notes"}
              and document["chapter"] == "Chapter_0001.md"
              and isinstance(document["updated_at"], str) and document["updated_at"],
              f"document keys={sorted(document)}")
        check("1j round-trip: document notes == kept list",
              document["notes"] == kept, f"doc={document['notes']}")

        check("1k round-trip: load_notes returns the same list",
              tn.load_notes(root, "Chapter_0001.md") == kept, "")


def case_2_invalid_entries() -> None:
    """save_notes drops invalid entries defensively and keeps the rest."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lines = ["Line zero.", "Line one.", "Line two."]
        notes = [
            "not a dict",                                   # non-dict
            {"line": -1, "term": "t", "note": "n"},         # negative line
            {"line": 3, "term": "t", "note": "n"},          # line == len(lines)
            {"line": True, "term": "t", "note": "n"},       # bool is not an int here
            {"term": "t", "note": "n"},                     # line missing
            {"line": 0, "term": 5, "note": "n"},            # non-str term
            {"line": 0, "term": "t", "note": None},         # non-str note
            {"line": 0, "term": "", "note": "n"},           # empty term
            {"line": 0, "term": "t", "note": "   "},        # whitespace-only note
            {"line": 1, "term": "筑基", "note": "Second realm."},  # valid
        ]
        kept = tn.save_notes(root, "Chapter_0001.md", lines, notes)
        check("2a invalid: only the one valid entry kept",
              len(kept) == 1 and kept[0]["term"] == "筑基"
              and kept[0]["anchor"] == "Line one.", f"kept={kept}")
        document = json.loads(
            (root / "notes" / "Chapter_0001.json").read_text(encoding="utf-8"))
        check("2b invalid: sidecar on disk holds exactly the kept entry",
              document["notes"] == kept, f"doc={document['notes']}")


def case_3_empty_kept_deletes() -> None:
    """An empty kept list means NO sidecar: absent = no notes."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lines = ["Line zero.", "Line one."]
        path = tn.notes_path(root, "Chapter_0001.md")

        # notes=[] on a fresh project: nothing to delete, still no file
        kept = tn.save_notes(root, "Chapter_0001.md", lines, [])
        check("3a empty: notes=[] returns [] and writes nothing",
              kept == [] and not path.exists(), f"kept={kept}")

        # pre-existing file removed by notes=[]
        tn.save_notes(root, "Chapter_0001.md", lines,
                      [{"line": 0, "term": "t", "note": "n"}])
        check("3b empty: pre-existing sidecar in place", path.is_file(), "")
        kept = tn.save_notes(root, "Chapter_0001.md", lines, [])
        check("3c empty: notes=[] DELETES the pre-existing sidecar",
              kept == [] and not path.exists(), f"kept={kept}")

        # all-invalid input removes it too
        tn.save_notes(root, "Chapter_0001.md", lines,
                      [{"line": 0, "term": "t", "note": "n"}])
        kept = tn.save_notes(root, "Chapter_0001.md", lines,
                             [{"line": 99, "term": "t", "note": "n"}])
        check("3d empty: all-invalid input DELETES the pre-existing sidecar",
              kept == [] and not path.exists(), f"kept={kept}")


def case_4_load_leniency() -> None:
    """load_notes: a broken sidecar means 'no notes', never a crash."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check("4a load: missing file -> []",
              tn.load_notes(root, "Chapter_0001.md") == [], "")

        path = tn.notes_path(root, "Chapter_0001.md")
        write_lf(path, "{ not json")
        check("4b load: malformed JSON -> []",
              tn.load_notes(root, "Chapter_0001.md") == [], "")

        write_lf(path, json.dumps({"chapter": "Chapter_0001.md"}))
        check("4c load: missing 'notes' key -> []",
              tn.load_notes(root, "Chapter_0001.md") == [], "")

        write_lf(path, json.dumps({"notes": {"line": 0}}))
        check("4d load: notes not a list -> []",
              tn.load_notes(root, "Chapter_0001.md") == [], "")

        write_lf(path, json.dumps([{"line": 0}]))
        check("4e load: document not an object -> []",
              tn.load_notes(root, "Chapter_0001.md") == [], "")

        write_lf(path, json.dumps({"notes": [{"line": 0}, "junk"]}))
        check("4f load: non-dict entry inside notes -> []",
              tn.load_notes(root, "Chapter_0001.md") == [], "")

        write_lf(path, json.dumps(
            {"notes": [{"line": 0, "term": "t", "note": "n", "anchor": "a"}]}))
        check("4g load: well-formed sidecar loads as-is",
              tn.load_notes(root, "Chapter_0001.md")
              == [{"line": 0, "term": "t", "note": "n", "anchor": "a"}], "")


def case_5_strip_marked_notes() -> None:
    """strip_marked_notes: legacy baked-in note extraction, pure text."""
    # Full case: body + blanks + heading + definitions
    body = (
        "Lin Feng pressed his palm to the crystal.[^1]\n"
        "\n"
        "The elder said nothing, only noting the color.[^2]\n"
        "\n"
        "\n"
        "## Translator's Notes\n"
        "\n"
        "[^1]: **spirit root** — innate aptitude for cultivation.\n"
        "[^2]: **sect** — a cultivation organization.\n"
    )
    clean, existing = tn.strip_marked_notes(body)
    check("5a strip: clean body equals the pre-marker body exactly",
          clean == "Lin Feng pressed his palm to the crystal.\n\n"
                   "The elder said nothing, only noting the color.",
          f"clean={clean!r}")
    check("5b strip: both definitions parsed in order",
          existing == [
              {"term": "spirit root", "note": "innate aptitude for cultivation."},
              {"term": "sect", "note": "a cultivation organization."},
          ], f"existing={existing}")

    # Stacked markers on one line
    clean, existing = tn.strip_marked_notes(
        "One line carries two notes.[^1][^2]\n"
        "\n"
        "## Translator's Notes\n"
        "\n"
        "[^1]: **a** — first.\n"
        "[^2]: **b** — second.\n"
    )
    check("5c strip: stacked [^1][^2] markers removed together",
          clean == "One line carries two notes.", f"clean={clean!r}")
    check("5d strip: stacked markers yield both entries",
          existing == [{"term": "a", "note": "first."},
                       {"term": "b", "note": "second."}], f"existing={existing}")

    # Markers with no section
    clean, existing = tn.strip_marked_notes("A marked line.[^3]\nPlain line.")
    check("5e strip: markers without a section still stripped, no entries",
          clean == "A marked line.\nPlain line." and existing == [],
          f"clean={clean!r}, existing={existing}")

    # Section with no markers
    clean, existing = tn.strip_marked_notes(
        "Body line.\n\n\n## Translator's Notes\n\n[^1]: **t** — n\n")
    check("5f strip: section without markers truncates body at the heading",
          clean == "Body line." and existing == [{"term": "t", "note": "n"}],
          f"clean={clean!r}, existing={existing}")

    # Clean passthrough: byte-identical string, empty list (trailing blanks kept)
    pristine = "Untouched body.\n\nStill clean.\n\n"
    clean, existing = tn.strip_marked_notes(pristine)
    check("5g strip: clean body passthrough identical, no entries",
          clean == pristine and existing == [], f"clean={clean!r}")

    # Definition-line parsing tolerant of extra whitespace
    clean, existing = tn.strip_marked_notes(
        "Line.\n\n## Translator's Notes\n\n[^1]:    **t**  —  n  \n")
    check("5h strip: extra whitespace in the definition line tolerated",
          existing == [{"term": "t", "note": "n"}], f"existing={existing}")

    # Unparseable definition lines are skipped silently
    clean, existing = tn.strip_marked_notes(
        "Line.\n\n## Translator's Notes\n\nfree text\n[^2]: no bold term format\n")
    check("5i strip: unparseable definition lines skipped silently",
          existing == [], f"existing={existing}")


def case_6_assemble_clean() -> None:
    """assemble.assemble writes clean markdown: no markers, no TN section."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out_path = root / "translated" / "Chapter_0007.md"
        assemble.assemble(
            out_path,
            {"chapter_title": "第二章 外门", "order": 6},
            "The Outer Gates",
            ["Para one.", "", "Para two."],
        )
        text = out_path.read_text(encoding="utf-8")
        check("6a assemble: no footnote markers added to the file",
              "[^" not in text, f"text={text!r}")
        check("6b assemble: no Translator's Notes section in the file",
              "## Translator's Notes" not in text, "")
        check("6c assemble: translated title in the frontmatter",
              text.startswith("---\n") and "title: The Outer Gates" in text,
              f"head={text[:80]!r}")

        fm, body = project.read_chapter(out_path)
        check("6d assemble: read_chapter sees the title + carried frontmatter",
              fm.get("title") == "The Outer Gates"
              and fm.get("chapter_title") == "第二章 外门" and fm.get("order") == 6,
              f"fm={fm}")
        check("6e assemble: body lines verbatim (one per translated line)",
              body == "Para one.\n\nPara two.", f"body={body!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_round_trip()
    case_2_invalid_entries()
    case_3_empty_kept_deletes()
    case_4_load_leniency()
    case_5_strip_marked_notes()
    case_6_assemble_clean()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
