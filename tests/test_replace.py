"""Tests for lib/replace.py: smart term replacement across translated chapters.

Covers build_matcher's Latin/CJK split and \\b-bounded multi-word semantics,
replace_text's capitalization and inflection-suffix polish, replace_chapters'
manifest-driven surgical rewrite (frontmatter byte-verbatim, [^N] markers,
dry-run, missing files, untouched files never rewritten), glossary_replace's
source-or-variant lookup, alt pruning, noop short-circuit, and dry-run, and
the matcher pre-flight: a deterministic build_matcher failure (an old
translation of bare punctuation) must fire BEFORE the glossary save.

The recovery case covers the write-ordering contract: chapters are rewritten
BEFORE glossary.json is saved, so a mid-loop chapter-write failure (an
attribute-swapped project.atomic_write_text raising OSError on the second
chapter) re-raises after printing the exact "[warn] replace incomplete:
k/N chapters rewritten; glossary.json not updated" line with the glossary
byte-unchanged and only the pre-failure chapters rewritten -- and re-running
the same command completes idempotently (already-rewritten chapters match
zero occurrences) and saves the glossary. replace_chapters' optional
`progress` list collects each durably-rewritten file name (nothing on a
dry-run), and the util path still works with no progress argument at all.

The BOM case pins replace_chapters' utf-8-sig chapter read: a BOM'd
hand-edited chapter (Windows "UTF-8 with BOM") must not lose its
frontmatter split -- the YAML title carrying the phrase case-insensitively
is neither counted nor rewritten -- and a real rewrite strips the BOM while
no-match and dry-run runs leave the BOM'd bytes untouched.

All chapter/glossary fixtures are built inside tempfile.TemporaryDirectory()
sandboxes per case — repo fixtures are never touched. Files are written with
explicit LF newlines so byte-level comparisons are deterministic.

Self-contained PASS/FAIL script (no pytest). The lib modules import pyyaml
and requests, so run via uv (deps declared inline below):

    uv run tests/test_replace.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0"]
# ///
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import replace as R  # noqa: E402
from lib.replace import ReplaceError  # noqa: E402

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
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_bom(path: Path, text: str) -> None:
    """Write text as UTF-8 WITH a leading BOM (EF BB BF) and LF newlines --
    what a Windows editor saving 'UTF-8 with BOM' produces on a hand-edited
    chapter."""
    write_lf(path, "\ufeff" + text)


def raises_replace_error(fn) -> bool:
    try:
        fn()
    except ReplaceError:
        return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- case 3 data

CH1_HEAD = (
    "---\n"
    "chapter_title: 第一章 灵根\n"
    "title: Spirit Root Awakening\n"
    "---"
)
CH1_BODY = (
    "\n\nLin Feng pressed his palm to the crystal and his spirit root flared.[^1]\n"
    "\n"
    "The elder said nothing, only noting the color of the spirit root.\n"
    "\n"
    "## Translator's Notes\n"
    "\n"
    "[^1]: **spirit root** — innate aptitude for cultivation, measurable as an attribute.\n"
)
CH1 = CH1_HEAD + CH1_BODY
CH1_AFTER = (CH1_HEAD + CH1_BODY).replace("spirit root", "spiritual root")

CH2 = (
    "---\n"
    "chapter_title: 第二章 外门\n"
    "title: The Outer Gates\n"
    "---\n"
    "\n"
    "Mu Ya possessed a spirit root of poor grade.\n"
    "\n"
    "No one expected much of her.\n"
)
CH2_AFTER = CH2.replace("spirit root", "spiritual root")

# translated, on disk, but contains no match -> must NOT be rewritten
CH4 = (
    "---\n"
    "chapter_title: 第四章 山雨\n"
    "title: Rain on the Mountain\n"
    "---\n"
    "\n"
    "The storm gathered over the sect's peaks.\n"
)

# pending status, on disk, DOES contain the phrase -> must be skipped entirely
CH5 = (
    "---\n"
    "chapter_title: 第五章 夜行\n"
    "title: Night Walk\n"
    "---\n"
    "\n"
    "A wandering cultivator spoke of a spirit root in the east.\n"
)

MANIFEST_3 = [
    {"file": "Chapter_0001.md", "number": 1, "order": 0, "status": "translated",
     "title": "Spirit Root Awakening"},
    {"file": "Chapter_0002.md", "number": 2, "order": 1, "status": "translated",
     "title": "The Outer Gates"},
    {"file": "Chapter_0003.md", "number": 3, "order": 2, "status": "translated",
     "title": "Missing Chapter"},
    {"file": "Chapter_0004.md", "number": 4, "order": 3, "status": "translated",
     "title": "Rain on the Mountain"},
    {"file": "Chapter_0005.md", "number": 5, "order": 4, "status": "pending",
     "title": "Night Walk"},
]

REPORT_3 = {
    "scanned": 3,
    "changed": 2,
    "occurrences": 4,
    "per_chapter": [("Chapter_0001.md", 3), ("Chapter_0002.md", 1)],
    "missing": ["Chapter_0003.md"],
}


def make_chapters_project(td: str) -> tuple[Path, dict[str, bytes]]:
    """Temp project with translated chapters + manifest; returns (root, bytes-before)."""
    root = Path(td)
    translated = root / "translated"
    translated.mkdir()
    write_lf(translated / "Chapter_0001.md", CH1)
    write_lf(translated / "Chapter_0002.md", CH2)
    write_lf(translated / "Chapter_0004.md", CH4)
    write_lf(translated / "Chapter_0005.md", CH5)
    # Chapter_0003.md deliberately absent -> lands in `missing`
    before = {
        "Chapter_0001.md": (translated / "Chapter_0001.md").read_bytes(),
        "Chapter_0002.md": (translated / "Chapter_0002.md").read_bytes(),
        "Chapter_0004.md": (translated / "Chapter_0004.md").read_bytes(),
        "Chapter_0005.md": (translated / "Chapter_0005.md").read_bytes(),
    }
    return root, before


# ---------------------------------------------------------------- case 4 data

GLO_CHAPTER = (
    "---\n"
    "chapter_title: 第一章 灵根\n"
    "title: The Testing\n"
    "---\n"
    "\n"
    "His spirit root was graded poorly.\n"
    "\n"
    "The elder examined Spirit roots with an old mirror.\n"
)


def glossary_json() -> str:
    return json.dumps(
        {
            "terms": [
                {
                    "source": "灵根",
                    "translation": "spirit root",
                    "alt_translations": ["spirit root", "spirit core"],
                    "variants": ["靈根"],
                    "definition": "Innate aptitude for cultivation.",
                    "category": "level",
                    "origin": "seeded",
                }
            ]
        },
        ensure_ascii=False, indent=2,
    ) + "\n"


def make_glossary_project(td: str) -> tuple[Path, dict[str, bytes]]:
    """Temp project with glossary.json + one translated chapter on disk."""
    root = Path(td)
    translated = root / "translated"
    translated.mkdir()
    write_lf(root / "glossary.json", glossary_json())
    write_lf(root / "chapters.json", json.dumps([
        {"file": "Chapter_0001.md", "number": 1, "order": 0,
         "status": "translated", "title": "The Testing"},
    ], ensure_ascii=False, indent=2) + "\n")
    write_lf(translated / "Chapter_0001.md", GLO_CHAPTER)
    before = {
        "glossary": (root / "glossary.json").read_bytes(),
        "chapter": (translated / "Chapter_0001.md").read_bytes(),
    }
    return root, before


def load_glossary_entry(root: Path) -> dict:
    data = json.loads((root / "glossary.json").read_text(encoding="utf-8"))
    return data["terms"][0]


# ------------------------------------------------------------ case 7 fixtures

REC_FILES = ("Chapter_0001.md", "Chapter_0002.md", "Chapter_0003.md")


def rec_chapter(n: int) -> str:
    return (
        "---\n"
        f"chapter_title: 第{['一', '二', '三'][n - 1]}章 试炼\n"
        f"title: Trial {n}\n"
        "---\n"
        "\n"
        f"The spirit root flared for the {n}th time.\n"
    )


REC_MANIFEST = [
    {"file": name, "number": i + 1, "order": i, "status": "translated",
     "title": f"Trial {i + 1}"}
    for i, name in enumerate(REC_FILES)
]


def make_recovery_project(td: str) -> tuple[Path, dict[str, bytes]]:
    """Temp project: glossary 灵根 -> 'spirit root' + three translated
    chapters each carrying one lowercase occurrence; returns (root,
    bytes-before for the glossary and every chapter)."""
    root = Path(td)
    translated = root / "translated"
    translated.mkdir()
    write_lf(root / "glossary.json", json.dumps(
        {"terms": [{
            "source": "灵根", "translation": "spirit root", "variants": [],
            "alt_translations": [],
        }]}, ensure_ascii=False, indent=2) + "\n")
    write_lf(root / "chapters.json",
             json.dumps(REC_MANIFEST, ensure_ascii=False, indent=2) + "\n")
    for i, name in enumerate(REC_FILES, 1):
        write_lf(translated / name, rec_chapter(i))
    before: dict[str, bytes] = {
        "glossary": (root / "glossary.json").read_bytes(),
    }
    for name in REC_FILES:
        before[name] = (translated / name).read_bytes()
    return root, before


# ------------------------------------------------------------ case 8 fixtures

# One translated chapter whose frontmatter title contains the search phrase
# case-insensitively ("Spirit Root Awakening" vs phrase "spirit root"): a
# BOM'd read that defeats _split_frontmatter (head "") would count the title
# hit and rewrite the YAML. Reuses CH1 (title hit + 3 body occurrences).
BOM_MANIFEST = [
    {"file": "Chapter_0001.md", "number": 1, "order": 0, "status": "translated",
     "title": "Spirit Root Awakening"},
]

# No occurrence of the phrase anywhere (frontmatter or body): the BOM'd file
# must stay byte-identical, BOM included.
BOM_NO_MATCH = (
    "---\n"
    "chapter_title: 第四章 山雨\n"
    "title: Rain on the Mountain\n"
    "---\n"
    "\n"
    "The storm gathered over the sect's peaks.\n"
)


def make_bom_project(td: str, chapter_text: str) -> tuple[Path, bytes]:
    """Temp project with ONE BOM'd translated chapter (Chapter_0001.md);
    returns (root, chapter bytes-before)."""
    root = Path(td)
    translated = root / "translated"
    translated.mkdir()
    write_bom(translated / "Chapter_0001.md", chapter_text)
    return root, (translated / "Chapter_0001.md").read_bytes()


# ---------------------------------------------------------------------- cases

def case_1_matcher() -> None:
    """build_matcher: regex semantics for Latin, literal for CJK, errors."""
    kind, _matcher = R.build_matcher("spirit root")
    check("1a matcher: 'spirit root' builds a regex matcher", kind == "regex",
          f"kind={kind!r}")

    text = "spirit root and Spirit root, spirit-root or spirit roots."
    out, n = R.replace_text(text, "spirit root", "X")
    check("1b matcher: all four writings match (count 4)", n == 4, f"n={n}, out={out!r}")

    out, n = R.replace_text("mushroom beetroot", "root", "X")
    check("1c matcher: \\b keeps 'root' out of mushroom/beetroot", n == 0,
          f"n={n}, out={out!r}")

    out, n = R.replace_text("The Heavenly Thunder Sects fell.",
                            "Heavenly Thunder Sect", "thunder court")
    check("1d matcher: multi-word phrase matches plural form",
          n == 1 and out == "The Thunder Courts fell.", f"n={n}, out={out!r}")

    literal = R.build_matcher("灵根")
    check("1e matcher: CJK phrase builds a literal matcher",
          literal == ("literal", "灵根"), f"got={literal!r}")
    out, n = R.replace_text("他灵根觉醒，其灵根为金属性。", "灵根", "道基")
    check("1f matcher: literal replace is exact substring replace",
          n == 2 and out == "他道基觉醒，其道基为金属性。", f"n={n}, out={out!r}")

    for phrase in ("", "   ", " - "):
        check(f"1g matcher: empty/no-word phrase {phrase!r} raises ReplaceError",
              raises_replace_error(lambda p=phrase: R.build_matcher(p)))


def case_2_case_and_inflection() -> None:
    """replace_text polishes capitalization and re-appends the inflection."""
    out, n = R.replace_text("The Spirit root hummed.", "spirit root", "spiritual root")
    check("2a polish: initial-cap span -> initial-cap replacement",
          n == 1 and out == "The Spiritual root hummed.", f"n={n}, out={out!r}")

    out, n = R.replace_text("His Spirit Root sang.", "spirit root", "spiritual root")
    check("2b polish: all-words-cap span -> title-case replacement",
          n == 1 and out == "His Spiritual Root sang.", f"n={n}, out={out!r}")

    out, n = R.replace_text("She tended the spirit roots daily.",
                            "spirit root", "spiritual root")
    check("2c polish: captured 's' suffix re-appended",
          n == 1 and out == "She tended the spiritual roots daily.",
          f"n={n}, out={out!r}")

    out, n = R.replace_text("She tended the spiritual roots daily.",
                            "spiritual root", "spiritual root")
    check("2d polish: replacement + suffix reproduces text unchanged",
          n == 1 and out == "She tended the spiritual roots daily.",
          f"n={n}, out={out!r}")

    out, n = R.replace_text("a spirit root awoke at dawn.", "spirit root", "spiritual root")
    check("2e polish: suffix-less span leaves replacement untouched",
          n == 1 and out == "a spiritual root awoke at dawn.", f"n={n}, out={out!r}")

    out, n = R.replace_text("the spirit root glowed faintly.", "spirit root", "spiritual root")
    check("2f polish: lowercase span stays lowercase",
          n == 1 and out == "the spiritual root glowed faintly.", f"n={n}, out={out!r}")


def case_3_replace_chapters() -> None:
    """replace_chapters: manifest-driven, surgical body-only rewrite."""
    # Case A: real run
    with tempfile.TemporaryDirectory() as td:
        root, before = make_chapters_project(td)
        rep = R.replace_chapters(root, MANIFEST_3, "spirit root", "spiritual root")
        check("3a report: full counts (scanned/changed/occurrences/per_chapter/missing)",
              rep == REPORT_3, f"rep={rep}")

        translated = root / "translated"
        ch1 = (translated / "Chapter_0001.md").read_text(encoding="utf-8")
        check("3b rewrite: Chapter_0001 exact expected text (body + TN section)",
              ch1 == CH1_AFTER, f"got={ch1!r}")
        check("3c rewrite: frontmatter head block byte-exact",
              ch1.startswith(CH1_HEAD), f"head={ch1.split(chr(10))[0:4]!r}")
        marker_lines = [ln for ln in ch1.split("\n") if ln.endswith("[^1]")]
        check("3d rewrite: [^1] marker still ends its body line",
              len(marker_lines) == 1 and "spiritual root flared" in marker_lines[0],
              f"marker_lines={marker_lines!r}")
        check("3e rewrite: untouched body line preserved",
              "The elder said nothing, only noting the color of the spiritual root." in ch1,
              "")
        ch2 = (translated / "Chapter_0002.md").read_text(encoding="utf-8")
        check("3f rewrite: Chapter_0002 exact expected text", ch2 == CH2_AFTER,
              f"got={ch2!r}")
        check("3g rewrite: match-free file NOT rewritten",
              (translated / "Chapter_0004.md").read_bytes() == before["Chapter_0004.md"],
              "")
        check("3h rewrite: pending-status file skipped",
              (translated / "Chapter_0005.md").read_bytes() == before["Chapter_0005.md"],
              "")

    # Case B: dry_run computes the identical report but writes nothing
    with tempfile.TemporaryDirectory() as td:
        root, before = make_chapters_project(td)
        rep = R.replace_chapters(root, MANIFEST_3, "spirit root", "spiritual root",
                                 dry_run=True)
        check("3i dry-run: report identical to the real run", rep == REPORT_3,
              f"rep={rep}")
        translated = root / "translated"
        unchanged = all(
            (translated / name).read_bytes() == data for name, data in before.items()
        )
        check("3j dry-run: every on-disk file byte-identical", unchanged, "")

    # Case C: zero occurrences -> zero changes
    with tempfile.TemporaryDirectory() as td:
        root, before = make_chapters_project(td)
        rep = R.replace_chapters(root, MANIFEST_3, "golden core", "golden core")
        check("3k no-match: occurrences 0, changed 0, per_chapter empty",
              rep["occurrences"] == 0 and rep["changed"] == 0 and rep["per_chapter"] == [],
              f"rep={rep}")
        translated = root / "translated"
        unchanged = all(
            (translated / name).read_bytes() == data for name, data in before.items()
        )
        check("3l no-match: no file rewritten", unchanged, "")


def case_4_glossary_replace() -> None:
    """glossary_replace: lookup, alt pruning, noop, dry-run."""

    # Case A: default — prune old rendering from alts, rewrite chapters
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_glossary_project(td)
        rep = R.glossary_replace(root, "灵根", "spiritual root")
        g = rep["glossary"]
        check("4a default: glossary diff (old/new/pruned_alt/noop)",
              g["old"] == "spirit root" and g["new"] == "spiritual root"
              and g["pruned_alt"] == ["spirit root"] and g["noop"] is False,
              f"g={g}")
        check("4b default: chapters report occurrences 2 / changed 1",
              rep["chapters"]["occurrences"] == 2 and rep["chapters"]["changed"] == 1
              and rep["chapters"]["scanned"] == 1 and rep["chapters"]["missing"] == [],
              f"chapters={rep['chapters']}")
        entry = load_glossary_entry(root)
        check("4c default: saved translation updated, 'spirit root' pruned, "
              "'spirit core' kept",
              entry["translation"] == "spiritual root"
              and entry["alt_translations"] == ["spirit core"]
              and entry["variants"] == ["靈根"],
              f"entry={entry}")
        ch = ((root / "translated" / "Chapter_0001.md").read_text(encoding="utf-8"))
        expected = GLO_CHAPTER.replace("His spirit root", "His spiritual root") \
                              .replace("Spirit roots", "Spiritual roots")
        check("4d default: chapter rewritten with polished replacements",
              ch == expected, f"got={ch!r}")

    # Case B: keep_alt=True leaves the alt list untouched
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_glossary_project(td)
        rep = R.glossary_replace(root, "灵根", "spiritual root", keep_alt=True)
        check("4e keep_alt: pruned_alt empty",
              rep["glossary"]["pruned_alt"] == [], f"g={rep['glossary']}")
        entry = load_glossary_entry(root)
        check("4f keep_alt: alt list untouched on disk, translation still updated",
              entry["alt_translations"] == ["spirit root", "spirit core"]
              and entry["translation"] == "spiritual root", f"entry={entry}")
        check("4g keep_alt: chapters still rewritten",
              rep["chapters"]["occurrences"] == 2
              and "spiritual root" in (root / "translated" / "Chapter_0001.md")
              .read_text(encoding="utf-8"), f"chapters={rep['chapters']}")

    # Case C: lookup by variant finds the entry
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_glossary_project(td)
        rep = R.glossary_replace(root, "靈根", "spirit essence")
        check("4h variant lookup: resolves to the canonical source entry",
              rep["glossary"]["source"] == "灵根" and rep["glossary"]["old"] == "spirit root",
              f"g={rep['glossary']}")
        entry = load_glossary_entry(root)
        check("4i variant lookup: saved translation updated",
              entry["translation"] == "spirit essence", f"entry={entry}")
        ch = (root / "translated" / "Chapter_0001.md").read_text(encoding="utf-8")
        check("4j variant lookup: chapters rewritten",
              "His spirit essence was graded poorly." in ch
              and "The elder examined Spirit essences with an old mirror." in ch,
              f"got={ch!r}")

    # Case D: unknown term raises ReplaceError
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_glossary_project(td)
        check("4k unknown term: ReplaceError raised",
              raises_replace_error(lambda: R.glossary_replace(root, "道基", "dao foundation")))

    # Case E: old == new (case-insensitive) -> noop, nothing written
    with tempfile.TemporaryDirectory() as td:
        root, before = make_glossary_project(td)
        rep = R.glossary_replace(root, "灵根", "Spirit Root")
        check("4m noop: flagged when new matches old case-insensitively",
              rep["glossary"]["noop"] is True, f"g={rep['glossary']}")
        check("4n noop: chapters report empty",
              rep["chapters"]["scanned"] == 0 and rep["chapters"]["occurrences"] == 0,
              f"chapters={rep['chapters']}")
        check("4o noop: glossary.json and chapter bytes unchanged",
              (root / "glossary.json").read_bytes() == before["glossary"]
              and (root / "translated" / "Chapter_0001.md").read_bytes() == before["chapter"],
              "")

    # Case F: dry_run reports the diff but writes nothing
    with tempfile.TemporaryDirectory() as td:
        root, before = make_glossary_project(td)
        rep = R.glossary_replace(root, "灵根", "spiritual root", dry_run=True)
        check("4p dry-run: reports chapter occurrences and pruned alt",
              rep["chapters"]["occurrences"] == 2
              and rep["glossary"]["pruned_alt"] == ["spirit root"]
              and rep["glossary"]["noop"] is False, f"rep={rep}")
        check("4q dry-run: glossary.json bytes unchanged",
              (root / "glossary.json").read_bytes() == before["glossary"], "")
        check("4r dry-run: chapter bytes unchanged",
              (root / "translated" / "Chapter_0001.md").read_bytes() == before["chapter"],
              "")


def case_5_guards() -> None:
    """Setup problems fail before any write: empty/null translation,
    missing or corrupt manifest."""
    with tempfile.TemporaryDirectory() as td:
        root, before = make_glossary_project(td)
        entry = load_glossary_entry(root)
        entry["translation"] = ""
        write_lf(root / "glossary.json", json.dumps({"terms": [entry]}, ensure_ascii=False, indent=2) + "\n")
        empty_bytes = (root / "glossary.json").read_bytes()
        ok = raises_replace_error(lambda: R.glossary_replace(root, "灵根", "new"))
        check("5a guards: empty translation -> ReplaceError", ok)
        check("5b guards: glossary bytes unchanged after the empty failure",
              (root / "glossary.json").read_bytes() == empty_bytes)

        entry = load_glossary_entry(root)
        entry["translation"] = None
        write_lf(root / "glossary.json", json.dumps({"terms": [entry]}, ensure_ascii=False, indent=2) + "\n")
        null_bytes = (root / "glossary.json").read_bytes()
        ok = raises_replace_error(lambda: R.glossary_replace(root, "灵根", "new"))
        check("5c guards: null translation -> ReplaceError (never the phrase 'None')", ok)
        check("5d guards: glossary bytes unchanged after the null failure",
              (root / "glossary.json").read_bytes() == null_bytes)

    with tempfile.TemporaryDirectory() as td:
        root, _before = make_glossary_project(td)
        (root / "chapters.json").unlink()
        ok = raises_replace_error(lambda: R.glossary_replace(root, "灵根", "new", dry_run=True))
        check("5e guards: missing manifest -> ReplaceError", ok)

        write_lf(root / "chapters.json", "{ not json")
        ok = raises_replace_error(lambda: R.glossary_replace(root, "灵根", "new", dry_run=True))
        check("5f guards: corrupt manifest -> ReplaceError (no raw ValueError)", ok)


def case_6_preflight() -> None:
    """Matcher pre-flight: build_matcher(old) runs BEFORE glossary.save, so
    a deterministic setup failure leaves glossary.json byte-unchanged. Only
    hyphen/whitespace-only old translations fail ('-' has no matchable
    words); '...' escapes into a valid regex and the run proceeds."""
    # Case A: old translation '-' -> ReplaceError, glossary bytes untouched
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_glossary_project(td)
        entry = load_glossary_entry(root)
        entry["translation"] = "-"
        write_lf(root / "glossary.json", json.dumps({"terms": [entry]}, ensure_ascii=False, indent=2) + "\n")
        hyphen_bytes = (root / "glossary.json").read_bytes()
        ok = raises_replace_error(lambda: R.glossary_replace(root, "灵根", "new rendering"))
        check("6a pre-flight: '-' old translation -> ReplaceError", ok)
        check("6b pre-flight: glossary.json byte-identical after the '-' failure",
              (root / "glossary.json").read_bytes() == hyphen_bytes)

    # Case B: old '...' builds a valid regex -> no pre-flight failure; the
    # glossary save lands and the (match-free) chapter rewrite completes.
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_glossary_project(td)
        entry = load_glossary_entry(root)
        entry["translation"] = "..."
        write_lf(root / "glossary.json", json.dumps({"terms": [entry]}, ensure_ascii=False, indent=2) + "\n")
        rep = R.glossary_replace(root, "灵根", "new rendering")
        entry = load_glossary_entry(root)
        check("6c pre-flight: '...' old translation does NOT fail (run completes)",
              rep["glossary"]["old"] == "..."
              and rep["glossary"]["new"] == "new rendering"
              and rep["glossary"]["noop"] is False
              and rep["chapters"]["occurrences"] == 0,
              f"rep={rep}")
        check("6d pre-flight: '...' run saved the new translation on disk",
              entry["translation"] == "new rendering", f"entry={entry}")


def case_7_progress_and_recovery() -> None:
    """replace_chapters' progress collection (and the plain no-progress util
    path), then glossary_replace's write ordering: a chapter-write failure
    mid-loop leaves glossary.json untouched after the exact warn line, and
    re-running the same command finishes the job idempotently."""
    # A: the util path with NO progress argument still rewrites everything
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_recovery_project(td)
        rep = R.replace_chapters(root, REC_MANIFEST, "spirit root",
                                 "spiritual root")
        check("7a util: no-progress call reports every chapter",
              rep["changed"] == 3 and rep["occurrences"] == 3
              and rep["missing"] == [], f"rep={rep}")
        check("7b util: every chapter rewritten on disk", all(
            (root / "translated" / n).read_text(encoding="utf-8")
            == rec_chapter(i).replace("spirit root", "spiritual root")
            for i, n in enumerate(REC_FILES, 1)), "")

    # B: progress collects each durably-rewritten file name; dry-run collects
    # nothing (a rewrite that never landed must not be reported as done)
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_recovery_project(td)
        progress: list[str] = []
        R.replace_chapters(root, REC_MANIFEST, "spirit root", "spiritual root",
                           progress=progress)
        check("7c progress: collects rewritten file names in order",
              progress == list(REC_FILES), f"progress={progress}")

    with tempfile.TemporaryDirectory() as td:
        root, before = make_recovery_project(td)
        progress = []
        rep = R.replace_chapters(root, REC_MANIFEST, "spirit root",
                                 "spiritual root", dry_run=True,
                                 progress=progress)
        check("7d progress: dry-run computes the report but collects nothing",
              rep["changed"] == 3 and progress == [],
              f"rep={rep} progress={progress}")
        check("7e progress: dry-run wrote nothing", all(
            (root / "translated" / n).read_bytes() == before[n]
            for n in REC_FILES), "")

    # C: a chapter-write failure on the 2nd chapter -> warn + re-raise,
    # glossary.json untouched, only chapter 1 rewritten
    with tempfile.TemporaryDirectory() as td:
        root, before = make_recovery_project(td)
        translated = root / "translated"
        orig_awt = R.project.atomic_write_text

        def flaky_awt(path, text, newline=None):
            if Path(path).name == "Chapter_0002.md":
                raise OSError("simulated chapter-write failure")
            return orig_awt(path, text, newline=newline)

        R.project.atomic_write_text = flaky_awt
        try:
            buf = io.StringIO()
            exc: BaseException | None = None
            try:
                with contextlib.redirect_stdout(buf):
                    R.glossary_replace(root, "灵根", "spiritual root")
            except Exception as caught:  # noqa: BLE001 - asserted below
                exc = caught
        finally:
            R.project.atomic_write_text = orig_awt
        out = buf.getvalue()
        check("7f recovery: the write failure re-raises out of glossary_replace",
              isinstance(exc, OSError), f"exc={exc!r}")
        check("7g recovery: exact incomplete warn line, nothing else printed",
              out == "[warn] replace incomplete: 1/3 chapters rewritten; "
                     "glossary.json not updated - re-run the same command "
                     "to finish\n", f"out={out!r}")
        check("7h recovery: glossary.json bytes unchanged",
              (root / "glossary.json").read_bytes() == before["glossary"])
        check("7i recovery: chapter 1 rewritten before the failure",
              (translated / "Chapter_0001.md").read_text(encoding="utf-8")
              == rec_chapter(1).replace("spirit root", "spiritual root"),
              f"got={(translated / 'Chapter_0001.md').read_text(encoding='utf-8')!r}")
        check("7j recovery: chapters 2-3 untouched",
              (translated / "Chapter_0002.md").read_bytes()
              == before["Chapter_0002.md"]
              and (translated / "Chapter_0003.md").read_bytes()
              == before["Chapter_0003.md"], "")
        check("7k recovery: no *.tmp siblings left in translated/",
              list(translated.glob("*.tmp")) == [],
              f"tmp={[str(p) for p in translated.glob('*.tmp')]}")

        # D: re-run the SAME command with the failure removed -> completes,
        # glossary saved; already-rewritten chapter 1 matches zero and is
        # skipped, so the recovery is idempotent
        rep = R.glossary_replace(root, "灵根", "spiritual root")
        check("7l recovery: re-run completes (only the 2 untouched chapters)",
              rep["chapters"]["changed"] == 2
              and rep["chapters"]["occurrences"] == 2
              and rep["chapters"]["per_chapter"]
              == [("Chapter_0002.md", 1), ("Chapter_0003.md", 1)],
              f"chapters={rep['chapters']}")
        entry = load_glossary_entry(root)
        check("7m recovery: glossary.json now carries the new translation",
              entry["translation"] == "spiritual root", f"entry={entry}")
        check("7n recovery: every chapter rewritten after the re-run", all(
            (translated / n).read_text(encoding="utf-8")
            == rec_chapter(i).replace("spirit root", "spiritual root")
            for i, n in enumerate(REC_FILES, 1)), "")
        check("7o recovery: no *.tmp siblings after the re-run",
              list(translated.glob("*.tmp")) == [], "")
        saved = (root / "glossary.json").read_bytes()
        rep2 = R.glossary_replace(root, "灵根", "spiritual root")
        check("7p recovery: a third run is a clean noop (old == new)",
              rep2["glossary"]["noop"] is True
              and (root / "glossary.json").read_bytes() == saved,
              f"g={rep2['glossary']}")


def case_8_bom_chapters() -> None:
    """replace_chapters reads chapters BOM-tolerantly: a BOM must not
    defeat _split_frontmatter, or the frontmatter title carrying the phrase
    case-insensitively would be counted and rewritten (the old plain-utf-8
    read). A real rewrite strips the BOM; a no-match or dry-run run leaves
    the BOM'd bytes untouched."""
    # Case A: real run -- frontmatter excluded from count and rewrite
    with tempfile.TemporaryDirectory() as td:
        root, _before = make_bom_project(td, CH1)
        rep = R.replace_chapters(root, BOM_MANIFEST, "spirit root", "spiritual root")
        check("8a BOM: occurrences exclude the frontmatter title hit",
              rep["occurrences"] == 3
              and rep["per_chapter"] == [("Chapter_0001.md", 3)]
              and rep["changed"] == 1, f"rep={rep}")
        raw = (root / "translated" / "Chapter_0001.md").read_bytes()
        check("8b BOM: real rewrite drops the EF BB BF prefix",
              raw[:3] != b"\xef\xbb\xbf", f"head={raw[:6]!r}")
        text = raw.decode("utf-8")
        check("8c BOM: frontmatter head byte-identical (YAML title not rewritten)",
              text.startswith(CH1_HEAD)
              and "title: Spirit Root Awakening" in text.split("---")[1]
              and "Spiritual Root" not in text.split("---")[1],
              f"head={text.split(chr(10))[0:4]!r}")
        check("8d BOM: rewritten file equals the plain (non-BOM'd) expectation",
              text == CH1_AFTER, f"got={text!r}")

    # Case B: dry_run -- same body-only count, BOM'd bytes untouched
    with tempfile.TemporaryDirectory() as td:
        root, before = make_bom_project(td, CH1)
        rep = R.replace_chapters(root, BOM_MANIFEST, "spirit root", "spiritual root",
                                 dry_run=True)
        check("8e BOM: dry-run still reports the body-only count",
              rep["occurrences"] == 3 and rep["changed"] == 1, f"rep={rep}")
        check("8f BOM: dry-run leaves the BOM'd file byte-identical",
              (root / "translated" / "Chapter_0001.md").read_bytes() == before)

    # Case C: no match anywhere -- nothing rewritten, BOM preserved
    with tempfile.TemporaryDirectory() as td:
        root, before = make_bom_project(td, BOM_NO_MATCH)
        rep = R.replace_chapters(root, BOM_MANIFEST, "spirit root", "spiritual root")
        check("8g BOM: no-match BOM'd file untouched (BOM preserved)",
              rep["occurrences"] == 0 and rep["changed"] == 0
              and (root / "translated" / "Chapter_0001.md").read_bytes() == before,
              f"rep={rep}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_matcher()
    case_2_case_and_inflection()
    case_3_replace_chapters()
    case_4_glossary_replace()
    case_5_guards()
    case_6_preflight()
    case_7_progress_and_recovery()
    case_8_bom_chapters()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
