"""Tests for `glossary count` and the GLOSSARY_EXPAND occurrence gate.

The CLI action (translate._cmd_glossary_count) is a read-only significance
check: it counts TERM (+ --variants) occurrences across the discovered
source chapters via glossary.count_term_in_chapters, prints the header, one
line per chapter with at least one hit (discover order), a total line, and
judges the total against the threshold (CLI --min > config
min_term_occurrences > DEFAULTS 3): >= threshold prints [ok] and returns 0,
below prints [warn] and returns 1. Empty TERM and negative --min are
CliErrors; a --chapters spec that matches nothing raises
pipeline.PipelineError through parse_range.

The occurrence gate (pipeline._apply_glossary_proposal) applies one
GLOSSARY_EXPAND proposal: retired sources are skipped first, then
proposals matching an existing entry (by source or, via the nickname
absorption loop, contained in / containing a known source with the same
translation) merge in without ever being counted. Only the brand-new-term
path is gated: when min_occurrences > 0 the term must occur at least that
many times in `corpus` or it is skipped with an exact-count line;
min_occurrences == 0 disables the gate (also the fail-open value the
GLOSSARY_EXPAND stage uses when the source corpus is unreadable).

Covered: parser shape for `glossary count`; basic counting (total, per-
chapter hits in discover order, exact output lines, exit 0 at the
threshold); --variants counted together longest-first without double
counting a substring variant; --chapters restricting the scan (header
shows the restricted count, other chapters' occurrences excluded); --min
override flipping the exit 1 -> 0; exit 1 below the default threshold with
the [warn] line; zero hits (0/3 chapters, no per-chapter lines); empty
TERM / negative --min -> CliError and an unknown chapter spec ->
PipelineError; the threshold read from project config.json
(min_term_occurrences: 1 lets a single-occurrence term pass, the same term
without the key stays below DEFAULTS 3); and the gate paths above called
directly on an in-memory glossary dict.

Mechanics mirror the sibling suites: cmd handlers are called with plain
Namespaces under captured stdout (test_command_defaults run_cli style),
the parser through translate._build_parser().parse_args
(test_parser_project style), and every fixture lives in a
TemporaryDirectory sandbox built with test_sync's write_source pattern
(Chapters under source/, one paragraph per line, plus an accurate
chapters.json manifest -- count discovers source/ itself, the manifest
just keeps the fixture a well-formed project). translate.py imports the
whole lib package (requests, ebooklib, pillow, pyyaml).

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_glossary_count.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

# lib/ and translate.py live at novel-translator/scripts relative to this
# file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary, pipeline, project  # noqa: E402
import translate  # noqa: E402
from translate import CliError  # noqa: E402

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


def run_cli(fn, ns: argparse.Namespace, project_dir: Path):
    """fn(ns, project_dir) with stdout captured; returns (code, output,
    exc) -- code is None when the call raised (the caller asserts on exc)."""
    buf = io.StringIO()
    code: int | None = None
    exc: Exception | None = None
    try:
        with contextlib.redirect_stdout(buf):
            code = fn(ns, project_dir)
    except Exception as caught:  # noqa: BLE001 - the caller asserts on it
        exc = caught
    return code, buf.getvalue(), exc


def capture(fn, *args, **kwargs):
    """fn(*args, **kwargs) with stdout captured; returns (result, output,
    exc) -- result is None when the call raised."""
    buf = io.StringIO()
    result = None
    exc: Exception | None = None
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except Exception as caught:  # noqa: BLE001 - the caller asserts on it
        exc = caught
    return result, buf.getvalue(), exc


def parse(argv: list[str]) -> argparse.Namespace | None:
    """_build_parser().parse_args(argv); None when argparse rejects it
    (SystemExit) -- no SystemExit may escape a valid invocation."""
    try:
        return translate._build_parser().parse_args(argv)
    except SystemExit:
        return None


def write_source(root: Path, name: str, text: str) -> None:
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    with open(source / name, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# Fixture bodies: 灵石 occurs 2x in ch1 + 1x in ch3 (total 3, exactly the
# DEFAULTS threshold), 裴小丫 2x in ch3 with one standalone 小丫, 山谷 1x
# in ch1, ch2 is empty of every fixture term.
BODIES = {
    "Chapter_0001.md": "灵石铺满了山谷。\n他又捡起一块灵石。\n",
    "Chapter_0002.md": "山门外风平浪静。\n",
    "Chapter_0003.md": "裴小丫握紧了灵石。\n裴小丫点头。小丫笑了。\n",
}
MANIFEST = [
    {"file": fname, "number": i + 1, "suffix": "", "order": i, "status": "pending"}
    for i, fname in enumerate(sorted(BODIES))
]


def make_project(root: Path, name: str, cfg_extra: dict | None = None) -> Path:
    """Minimal project: the three fixture chapters under source/, an
    accurate chapters.json manifest, and config.json {"providers": {}} +
    cfg_extra (count reads only min_term_occurrences from it)."""
    proj = root / name
    proj.mkdir()
    for fname, body in BODIES.items():
        write_source(proj, fname, body)
    (proj / "chapters.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cfg = {"providers": {}}
    cfg.update(cfg_extra or {})
    (proj / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return proj


def count_ns(term: str, variants: str | None = None,
             chapters: str | None = None, min_: int | None = None) -> argparse.Namespace:
    """Namespace in the exact shape the parser hands _cmd_glossary_count."""
    return argparse.Namespace(term=term, variants=variants or "",
                              chapters=chapters, min=min_)


# ---------------------------------------------------------------------- cases


def case_1_parser_shape() -> None:
    """`glossary count` parses TERM positionally plus --variants /
    --chapters / --min into the attribute names the handler reads."""
    ns = parse(["glossary", "count", "X", "--variants", "A,B",
                "--chapters", "1-3", "--min", "5"])
    check("1a parser: term/variants/chapters/min land under their names",
          ns is not None and ns.term == "X" and ns.variants == "A,B"
          and ns.chapters == "1-3" and ns.min == 5, f"ns={ns}")
    ns = parse(["glossary", "count", "荒塔"])
    check("1b parser: bare TERM -> variants '' and chapters/min None",
          ns is not None and ns.term == "荒塔" and ns.variants == ""
          and ns.chapters is None and ns.min is None, f"ns={ns}")


def case_2_basic_count() -> None:
    """Term in 2 of 3 chapters: total sums, hits stay in discover order,
    every output line matches the documented format, exit 0 at threshold."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        code, out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("灵石"), proj)
        check("2a basic: exit 0, no error (3 == DEFAULTS threshold)",
              exc is None and code == 0, f"code={code} exc={exc!r}")
        check("2b basic: exact output lines",
              out == "[glossary] count '灵石' across 3 chapter(s)\n"
                     "[glossary] Chapter_0001.md: 2\n"
                     "[glossary] Chapter_0003.md: 1\n"
                     "[glossary] total: 3 occurrence(s) in 2/3 chapter(s)\n"
                     "[ok] '灵石' meets the significance threshold (min 3)\n",
              f"out={out!r}")
        total, hits = glossary.count_term_in_chapters(proj, "灵石")
        check("2c basic: helper total and hits in discover order",
              total == 3 and hits == [("Chapter_0001.md", 2),
                                      ("Chapter_0003.md", 1)],
              f"total={total} hits={hits}")


def case_3_variants() -> None:
    """--variants are counted alongside TERM in one longest-first pass: a
    variant that is a substring of the term never double-counts."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        # 小丫 sits inside both 裴小丫 occurrences AND standalone once:
        # longest-first must count 裴小丫 x2 + 小丫 x1 = 3, not 5.
        code, out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("裴小丫", variants="小丫"), proj)
        check("3a variants: exit 0 without double counting the substring",
              exc is None and code == 0, f"code={code} exc={exc!r} out={out!r}")
        check("3b variants: header carries the (+1 variant(s)) note",
              "[glossary] count '裴小丫' (+1 variant(s)) across 3 chapter(s)" in out,
              f"out={out!r}")
        check("3c variants: all 3 hits in the one chapter with occurrences",
              "[glossary] Chapter_0003.md: 3" in out
              and "[glossary] total: 3 occurrence(s) in 1/3 chapter(s)" in out,
              f"out={out!r}")
        _total, hits = glossary.count_term_in_chapters(proj, "裴小丫", ["小丫"])
        check("3d variants: helper agrees (total in hits == 3)",
              hits == [("Chapter_0003.md", 3)], f"hits={hits}")


def case_4_chapters_spec() -> None:
    """--chapters 1-2 restricts the scan: chapter 3's occurrence is
    excluded and the header shows the restricted chapter count."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        code, out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("灵石", chapters="1-2"), proj)
        check("4a spec: header shows scanned=2",
              exc is None and "[glossary] count '灵石' across 2 chapter(s)" in out,
              f"exc={exc!r} out={out!r}")
        check("4b spec: only chapter 1 counted, chapter 3 excluded",
              "[glossary] Chapter_0001.md: 2" in out
              and "Chapter_0003" not in out
              and "[glossary] total: 2 occurrence(s) in 1/2 chapter(s)" in out,
              f"out={out!r}")
        check("4c spec: 2 < 3 -> exit 1 with the warn line",
              code == 1
              and "[warn] '灵石' is below the significance threshold: 2 < 3" in out,
              f"code={code} out={out!r}")


def case_5_min_override() -> None:
    """--min overrides the threshold: the same total-2 run that exits 1
    under the default 3 exits 0 once --min lowers the bar."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        code, out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("裴小丫"), proj)
        check("5a min: default threshold 3 -> exit 1",
              exc is None and code == 1, f"code={code} exc={exc!r}")
        code2, out2, exc2 = run_cli(
            translate._cmd_glossary_count, count_ns("裴小丫", min_=2), proj)
        check("5b min: --min 2 flips the exit to 0",
              exc2 is None and code2 == 0
              and "[ok] '裴小丫' meets the significance threshold (min 2)" in out2,
              f"code={code2} exc={exc2!r} out={out2!r}")


def case_6_no_hits() -> None:
    """A term that never occurs: 0 occurrence(s) in 0/3 chapters, no
    per-chapter lines, exit 1."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        code, out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("天外飞仙"), proj)
        check("6a no hits: exit 1", exc is None and code == 1,
              f"code={code} exc={exc!r}")
        check("6b no hits: exact output, no per-chapter lines",
              out == "[glossary] count '天外飞仙' across 3 chapter(s)\n"
                     "[glossary] total: 0 occurrence(s) in 0/3 chapter(s)\n"
                     "[warn] '天外飞仙' is below the significance threshold: 0 < 3\n",
              f"out={out!r}")


def case_7_bad_input() -> None:
    """Empty TERM -> CliError, negative --min -> CliError, unknown chapter
    spec -> pipeline.PipelineError from parse_range, corrupt chapter source
    -> CliError (never a raw ValueError, whose exit 1 would collide with the
    below-threshold verdict)."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        _code, _out, exc = run_cli(
            translate._cmd_glossary_count, count_ns(""), proj)
        check("7a empty TERM: CliError naming the requirement",
              isinstance(exc, CliError) and "TERM must be non-empty" in str(exc),
              f"exc={exc!r}")
        _code, _out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("灵石", min_=-1), proj)
        check("7b negative --min: CliError (--min must be >= 0)",
              isinstance(exc, CliError) and ">= 0" in str(exc), f"exc={exc!r}")
        _code, _out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("灵石", chapters="99"), proj)
        check("7c unknown spec: PipelineError (no chapters match 99)",
              isinstance(exc, pipeline.PipelineError) and "99" in str(exc),
              f"exc={exc!r}")
        write_source(proj, "Chapter_0004.md",
                     "---\nchapter_title: [unclosed\n---\n\nbody\n")
        _code, _out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("灵石"), proj)
        check("7d corrupt chapter: CliError (cannot count - ...)",
              isinstance(exc, CliError) and "cannot count" in str(exc),
              f"exc={exc!r}")


def case_8_config_threshold() -> None:
    """The threshold comes from project config min_term_occurrences:
    min_term_occurrences 1 lets a single-occurrence term pass, while the
    same term without the key stays below DEFAULTS 3."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg1 = make_project(root, "cfg1", {"min_term_occurrences": 1})
        code, out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("山谷"), cfg1)
        check("8a config: min_term_occurrences 1 -> 1 occurrence exits 0",
              exc is None and code == 0
              and "[ok] '山谷' meets the significance threshold (min 1)" in out,
              f"code={code} exc={exc!r} out={out!r}")
        plain = make_project(root, "plain")
        code2, out2, exc2 = run_cli(
            translate._cmd_glossary_count, count_ns("山谷"), plain)
        check("8b config: no key -> DEFAULTS 3 -> same term exits 1",
              exc2 is None and code2 == 1
              and "[warn] '山谷' is below the significance threshold: 1 < 3" in out2,
              f"code={code2} exc={exc2!r} out={out2!r}")
        check("8c config: DEFAULTS really carry min_term_occurrences 3",
              translate.config.DEFAULTS["min_term_occurrences"] == 3)


# --------------------------------------------- occurrence gate (GLOSSARY_EXPAND)


def proposal(src: str, tr: str = "Spirit Term", **extra) -> dict:
    """A well-formed GLOSSARY_EXPAND proposal (all four fields non-empty)."""
    d = {"source": src, "translation": tr,
         "definition": "A recurring term.", "category": "other"}
    d.update(extra)
    return d


def case_9_gate() -> None:
    """pipeline._apply_glossary_proposal: only the brand-new-term path is
    gated. Skips below the threshold with the exact line; adds at >= the
    threshold (origin model); min_occurrences 0 (the default and the
    corpus-unreadable fail-open value) adds despite zero occurrences;
    same-source and nickname-absorption merges are never gated; retired
    sources are skipped before the gate is even consulted."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)  # project_dir is only touched by the merge path

        # Brand-new term below the threshold: skipped, glossary untouched.
        g: dict = {"terms": []}
        _r, out, exc = capture(
            pipeline._apply_glossary_proposal, g, proposal("荒塔"), 2, {},
            "", "[t]", proj, corpus="荒塔立在城东。", min_occurrences=3)
        check("9a gate: 1 < 3 -> exact skip line, glossary unchanged",
              exc is None
              and out == "[t] [glossary] skip '荒塔' - 1 occurrence(s) "
                         "across the novel (min 3)\n"
              and g.get("terms") == [],
              f"exc={exc!r} out={out!r} terms={g.get('terms')}")

        # At the threshold: added as a model term with the proposal's fields.
        g2: dict = {"terms": []}
        _r, out2, exc2 = capture(
            pipeline._apply_glossary_proposal, g2,
            proposal("荒塔", variants=["古塔"]), 2, {}, "", "[t]", proj,
            corpus="荒塔。荒塔之下。再望荒塔。", min_occurrences=3)
        added = g2["terms"][0] if g2.get("terms") else {}
        check("9b gate: 3 occurrences -> upserted with origin model",
              exc2 is None and len(g2["terms"]) == 1
              and added.get("source") == "荒塔"
              and added.get("variants") == ["古塔"]
              and added.get("translation") == "Spirit Term"
              and added.get("origin") == "model"
              and added.get("first_seen_chapter") == 2,
              f"exc={exc2!r} entry={added}")
        check("9c gate: add confirmed with the [ok] glossary + line",
              out2 == "[t] [ok] glossary + '荒塔' -> 'Spirit Term'\n",
              f"out={out2!r}")

        # Gate disabled (min_occurrences 0, the fail-open value): added
        # despite zero corpus occurrences.
        g3: dict = {"terms": []}
        _r, _out3, exc3 = capture(
            pipeline._apply_glossary_proposal, g3, proposal("荒塔"), 2, {},
            "", "[t]", proj, corpus="", min_occurrences=0)
        check("9d gate: min_occurrences 0 adds with an empty corpus",
              exc3 is None and [t.get("source") for t in g3["terms"]] == ["荒塔"],
              f"exc={exc3!r} terms={g3.get('terms')}")

        # Same source, same translation: a no-op merge, never gated.
        existing = {"source": "荒塔", "translation": "Spirit Term",
                    "definition": "A recurring term.", "category": "other",
                    "origin": "seeded", "variants": [], "alt_translations": [],
                    "first_seen_chapter": 0}
        g4: dict = {"terms": [dict(existing)]}
        _r, out4, exc4 = capture(
            pipeline._apply_glossary_proposal, g4, proposal("荒塔"), 2, {},
            "", "[t]", proj, corpus="", min_occurrences=3)
        check("9e gate: same-translation re-proposal not gated (0 occurrences)",
              exc4 is None and "skip" not in out4
              and len(g4["terms"]) == 1 and g4["terms"][0] == existing,
              f"exc={exc4!r} out={out4!r} terms={g4['terms']}")

        # Nickname absorption: proposed source contained in a known source
        # with the same translation -> variant union, never gated.
        known = {"source": "裴小丫", "translation": "Pei Xiaoya",
                 "definition": "A girl.", "category": "person",
                 "origin": "seeded", "variants": [], "alt_translations": [],
                 "first_seen_chapter": 0}
        g5: dict = {"terms": [dict(known)]}
        _r, out5, exc5 = capture(
            pipeline._apply_glossary_proposal, g5, proposal("小丫", "Pei Xiaoya"),
            2, {}, "", "[t]", proj, corpus="", min_occurrences=3)
        check("9f gate: nickname absorbed as a variant, not gated",
              exc5 is None and len(g5["terms"]) == 1
              and g5["terms"][0].get("variants") == ["小丫"]
              and "skip" not in out5
              and "[t] [ok] glossary ~ '裴小丫' +variant(s) 小丫" in out5,
              f"exc={exc5!r} out={out5!r} terms={g5['terms']}")

        # Retired sources: skipped before the gate is consulted.
        g6: dict = {"terms": [], "retired": ["废丹"]}
        _r, out6, exc6 = capture(
            pipeline._apply_glossary_proposal, g6, proposal("废丹"), 2, {},
            "", "[t]", proj, corpus="", min_occurrences=3)
        check("9g gate: retired term skipped first, nothing added",
              exc6 is None
              and out6 == "[t] [glossary] skip re-adding retired term '废丹'\n"
              and g6["terms"] == [],
              f"exc={exc6!r} out={out6!r} terms={g6.get('terms')}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_parser_shape()
    case_2_basic_count()
    case_3_variants()
    case_4_chapters_spec()
    case_5_min_override()
    case_6_no_hits()
    case_7_bad_input()
    case_8_config_threshold()
    case_9_gate()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
