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
GLOSSARY_EXPAND proposal: retired sources are skipped first, then proposals
matching an existing entry by source merge in -- the entry's source is
never re-counted, but every NEW variant the re-proposal carries is gated
like a brand-new term (skipped with an exact `skip variant` count line and
the entry's variants unchanged; an already-present variant stays a silent
no-op and a variant equal to the source is dropped ungated).
Every other source -- nickname absorption included -- is gated: when
min_occurrences > 0 the term must occur at least that many times in
`corpus` or it is skipped with an exact-count line BEFORE the nickname
loop, so a one-occurrence string can never become a permanently matchable
variant; a nickname that meets the threshold is still absorbed as a
variant; min_occurrences == 0 disables the gate (also the fail-open value
the GLOSSARY_EXPAND stage uses when the source corpus is unreadable). Both
model-sourced category flows are coerced: a proposal category outside
glossary.CATEGORIES lands as "other" with the warn line (known values pass
verbatim; "unit" additionally warns that balance checks skip the category
while its translation is non-empty; an absent category keeps the field
ValueError and is never coerced), and the same check covers a category
the merge model ECHOES for an existing entry -- a merge omitting the key
never warns and never touches the entry's own category, while a merge
landing 'unit' on an entry with a surviving translation prints the same
guide-only warn before the update line (an empty surviving translation
stays silent); both pipeline warns route through
glossary.unit_translation_warning, keeping the text byte-identical with
the `glossary set` advisory.

The gate's corpus cache (pipeline._gate_corpus / _GATE_CORPUS) is covered
unit-level and end to end: the joined source corpus is read once per
resolved project dir per process (a counting project.read_chapter swap
proves multiple evaluations share one pass), separate projects cache
separately, gate outcomes against the cached corpus are identical to a
freshly rebuilt one (the skip line fires at the same thresholds), and a
failed read stays uncached -- every evaluation re-reads and re-fails until
the corpus becomes readable again in the same process. The run_chapter
integration (fake pipeline._chat, mock_server.py prompt sniffing) proves
the fail-open behavior: an unreadable sibling chapter disables the gate on
EVERY chapter run (warn each time, below-threshold term added), and after
the chapter is repaired the same process gates again -- a fresh
below-threshold proposal is skipped and the corpus becomes cached.

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
without the key stays below DEFAULTS 3); the gate paths above called
directly on an in-memory glossary dict -- including the category coercion
sub-cases (known "person" verbatim with no warn, "unit" verbatim with the
guide-only warn, unknown "faction" coerced to "other" with the warn line
before the add line, absent category -> ValueError) and, through a faked
pipeline._chat merge reply, the merge-path coercion (unknown echoed
category coerced + warned, omitted category preserving the entry's own,
echoed 'unit' warning on a surviving translation with the
empty-translation control); also the variant gate on the existing-entry
path (a zero-occurrence new variant skipped with the exact `skip variant`
line and the entry untouched, a threshold-meeting variant absorbed as
before, an already-present variant a silent no-op, a source-equal variant
dropped ungated);
and balance.count_in_target's target handling (case-variant duplicate
targets deduped to one count; per-target script split -- a CJK target
counted exactly while a Latin sibling target keeps word-boundary
matching); and the CLI-half of the guide-only advisory (`glossary set
--category unit` on an entry with a translation prints the exact warn, a
'place' set and an empty-translation unit set never do).

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

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import balance, config, glossary, pipeline, project
import translate
from translate import CliError

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
    except Exception as caught:
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
    except Exception as caught:
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


BODIES = {
    "CHAPTER_0001.md": "灵石铺满了山谷。\n他又捡起一块灵石。\n",
    "CHAPTER_0002.md": "山门外风平浪静。\n",
    "CHAPTER_0003.md": "裴小丫握紧了灵石。\n裴小丫点头。小丫笑了。\n",
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
                     "[glossary] CHAPTER_0001.md: 2\n"
                     "[glossary] CHAPTER_0003.md: 1\n"
                     "[glossary] total: 3 occurrence(s) in 2/3 chapter(s)\n"
                     "[ok] '灵石' meets the significance threshold (min 3)\n",
              f"out={out!r}")
        total, hits = glossary.count_term_in_chapters(proj, "灵石")
        check("2c basic: helper total and hits in discover order",
              total == 3 and hits == [("CHAPTER_0001.md", 2),
                                      ("CHAPTER_0003.md", 1)],
              f"total={total} hits={hits}")


def case_3_variants() -> None:
    """--variants are counted alongside TERM in one longest-first pass: a
    variant that is a substring of the term never double-counts."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        code, out, exc = run_cli(
            translate._cmd_glossary_count, count_ns("裴小丫", variants="小丫"), proj)
        check("3a variants: exit 0 without double counting the substring",
              exc is None and code == 0, f"code={code} exc={exc!r} out={out!r}")
        check("3b variants: header carries the (+1 variant(s)) note",
              "[glossary] count '裴小丫' (+1 variant(s)) across 3 chapter(s)" in out,
              f"out={out!r}")
        check("3c variants: all 3 hits in the one chapter with occurrences",
              "[glossary] CHAPTER_0003.md: 3" in out
              and "[glossary] total: 3 occurrence(s) in 1/3 chapter(s)" in out,
              f"out={out!r}")
        _total, hits = glossary.count_term_in_chapters(proj, "裴小丫", ["小丫"])
        check("3d variants: helper agrees (total in hits == 3)",
              hits == [("CHAPTER_0003.md", 3)], f"hits={hits}")


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
              "[glossary] CHAPTER_0001.md: 2" in out
              and "CHAPTER_0003" not in out
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
        write_source(proj, "CHAPTER_0004.md",
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


def proposal(src: str, tr: str = "Spirit Term", **extra) -> dict:
    """A well-formed GLOSSARY_EXPAND proposal (all four fields non-empty)."""
    d = {"source": src, "translation": tr,
         "definition": "A recurring term.", "category": "other"}
    d.update(extra)
    return d


def case_9_gate() -> None:
    """pipeline._apply_glossary_proposal: every proposal whose source is not
    an existing entry is gated -- nickname absorption included. Skips below
    the threshold with the exact line (a below-threshold nickname is dropped
    BEFORE it can become a variant); adds at >= the threshold (origin
    model); a nickname that meets the threshold in the corpus is still
    absorbed as a variant; min_occurrences 0 (the default and the
    corpus-unreadable fail-open value) adds despite zero occurrences;
    same-source re-proposals are never gated; retired sources are skipped
    before the gate is even consulted."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)

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

        g3: dict = {"terms": []}
        _r, _out3, exc3 = capture(
            pipeline._apply_glossary_proposal, g3, proposal("荒塔"), 2, {},
            "", "[t]", proj, corpus="", min_occurrences=0)
        check("9d gate: min_occurrences 0 adds with an empty corpus",
              exc3 is None and [t.get("source") for t in g3["terms"]] == ["荒塔"],
              f"exc={exc3!r} terms={g3.get('terms')}")

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

        known = {"source": "裴小丫", "translation": "Pei Xiaoya",
                 "definition": "A girl.", "category": "person",
                 "origin": "seeded", "variants": [], "alt_translations": [],
                 "first_seen_chapter": 0}
        g5: dict = {"terms": [dict(known)]}
        _r, out5, exc5 = capture(
            pipeline._apply_glossary_proposal, g5, proposal("小丫", "Pei Xiaoya"),
            2, {}, "", "[t]", proj, corpus="小丫笑了。", min_occurrences=3)
        check("9f gate: below-threshold nickname dropped before absorption",
              exc5 is None and len(g5["terms"]) == 1
              and g5["terms"][0].get("variants") == []
              and out5 == "[t] [glossary] skip '小丫' - 1 occurrence(s) "
                         "across the novel (min 3)\n",
              f"exc={exc5!r} out={out5!r} terms={g5['terms']}")

        g5b: dict = {"terms": [dict(known)]}
        _r, out5b, exc5b = capture(
            pipeline._apply_glossary_proposal, g5b,
            proposal("小丫", "Pei Xiaoya"), 2, {}, "", "[t]", proj,
            corpus="小丫。小丫和小丫。", min_occurrences=3)
        check("9f2 gate: nickname at the threshold absorbed as a variant",
              exc5b is None and len(g5b["terms"]) == 1
              and g5b["terms"][0].get("variants") == ["小丫"]
              and "[t] [ok] glossary ~ '裴小丫' +variant(s) 小丫" in out5b
              and "skip" not in out5b,
              f"exc={exc5b!r} out={out5b!r} terms={g5b['terms']}")

        g5c: dict = {"terms": [dict(known)]}
        _r, out5c, exc5c = capture(
            pipeline._apply_glossary_proposal, g5c,
            proposal("裴小丫", "Pei Xiaoya", variants=["未命中的词"]), 2, {},
            "", "[t]", proj, corpus="", min_occurrences=3)
        check("9f3 gate: zero-occurrence new variant dropped, exact line",
              exc5c is None and len(g5c["terms"]) == 1
              and g5c["terms"][0] == known
              and out5c == "[t] [glossary] skip variant '未命中的词' - 0 "
                          "occurrence(s) across the novel (min 3)\n",
              f"exc={exc5c!r} out={out5c!r} terms={g5c['terms']}")

        g5d: dict = {"terms": [dict(known)]}
        _r, out5d, exc5d = capture(
            pipeline._apply_glossary_proposal, g5d,
            proposal("裴小丫", "Pei Xiaoya", variants=["小丫"]), 2, {}, "",
            "[t]", proj, corpus="小丫。小丫和小丫。", min_occurrences=3)
        check("9f4 gate: threshold-meeting new variant absorbed as before",
              exc5d is None and len(g5d["terms"]) == 1
              and g5d["terms"][0].get("variants") == ["小丫"]
              and out5d == "[t] [ok] glossary ~ '裴小丫' +variant(s) 小丫\n",
              f"exc={exc5d!r} out={out5d!r} terms={g5d['terms']}")

        known_v = dict(known, variants=["小丫"])
        g5e: dict = {"terms": [dict(known_v)]}
        _r, out5e, exc5e = capture(
            pipeline._apply_glossary_proposal, g5e,
            proposal("裴小丫", "Pei Xiaoya", variants=["小丫"]), 2, {}, "",
            "[t]", proj, corpus="", min_occurrences=3)
        check("9f5 gate: already-present variant not re-gated, silent no-op",
              exc5e is None and len(g5e["terms"]) == 1
              and g5e["terms"][0] == known_v and out5e == "",
              f"exc={exc5e!r} out={out5e!r} terms={g5e['terms']}")

        g5f: dict = {"terms": [dict(known)]}
        _r, out5f, exc5f = capture(
            pipeline._apply_glossary_proposal, g5f,
            proposal("裴小丫", "Pei Xiaoya", variants=["裴小丫"]), 2, {}, "",
            "[t]", proj, corpus="", min_occurrences=3)
        check("9f6 gate: source-equal variant dropped ungated, no skip line",
              exc5f is None and len(g5f["terms"]) == 1
              and g5f["terms"][0] == known and out5f == "",
              f"exc={exc5f!r} out={out5f!r} terms={g5f['terms']}")

        g6: dict = {"terms": [], "retired": ["废丹"]}
        _r, out6, exc6 = capture(
            pipeline._apply_glossary_proposal, g6, proposal("废丹"), 2, {},
            "", "[t]", proj, corpus="", min_occurrences=3)
        check("9g gate: retired term skipped first, nothing added",
              exc6 is None
              and out6 == "[t] [glossary] skip re-adding retired term '废丹'\n"
              and g6["terms"] == [],
              f"exc={exc6!r} out={out6!r} terms={g6.get('terms')}")

        g7: dict = {"terms": []}
        _r, out7, exc7 = capture(
            pipeline._apply_glossary_proposal, g7,
            proposal("荒塔", category="person"), 2, {}, "", "[t]", proj,
            corpus="", min_occurrences=0)
        check("9h gate: known category 'person' stored verbatim, no warn",
              exc7 is None and len(g7["terms"]) == 1
              and g7["terms"][0].get("category") == "person"
              and out7 == "[t] [ok] glossary + '荒塔' -> 'Spirit Term'\n",
              f"exc={exc7!r} out={out7!r} terms={g7.get('terms')}")
        g8: dict = {"terms": []}
        _r, out8, exc8 = capture(
            pipeline._apply_glossary_proposal, g8,
            proposal("古塔", category="unit"), 2, {}, "", "[t]", proj,
            corpus="", min_occurrences=0)
        check("9h2 gate: 'unit' stored verbatim with the guide-only warn",
              exc8 is None and len(g8["terms"]) == 1
              and g8["terms"][0].get("category") == "unit"
              and out8 == "[t] [warn] glossary: '古塔' has a translation but "
                          "category 'unit' (guide-only: balance checks skip "
                          "it)\n"
                         "[t] [ok] glossary + '古塔' -> 'Spirit Term'\n",
              f"exc={exc8!r} out={out8!r} terms={g8.get('terms')}")

        g9: dict = {"terms": []}
        _r, out9, exc9 = capture(
            pipeline._apply_glossary_proposal, g9,
            proposal("荒塔", category="faction"), 2, {}, "", "[t]", proj,
            corpus="", min_occurrences=0)
        check("9i gate: unknown category 'faction' coerced to 'other'",
              exc9 is None and len(g9["terms"]) == 1
              and g9["terms"][0].get("category") == "other",
              f"exc={exc9!r} terms={g9.get('terms')}")
        check("9i2 gate: exactly the warn line then the add line, in order",
              out9 == "[t] [glossary] warn unknown category 'faction' for "
                      "'荒塔' - coerced to 'other'\n"
                     "[t] [ok] glossary + '荒塔' -> 'Spirit Term'\n",
              f"out={out9!r}")

        g10: dict = {"terms": []}
        _r, _out10, exc10 = capture(
            pipeline._apply_glossary_proposal, g10,
            proposal("荒塔", category=None), 2, {}, "", "[t]", proj,
            corpus="", min_occurrences=0)
        check("9j gate: absent category keeps the ValueError skip",
              isinstance(exc10, ValueError)
              and "must be non-empty strings" in str(exc10)
              and g10 == {"terms": []},
              f"exc={exc10!r} terms={g10.get('terms')}")


def case_10_gate_corpus_cache() -> None:
    """pipeline._gate_corpus: the joined source corpus is read once per
    resolved project dir per process and shared by every later gate
    evaluation, separate projects cache separately, and gate outcomes
    against the cached corpus are identical to a freshly rebuilt one. A
    failed read is never cached: each evaluation re-reads the chapters and
    re-fails, until the corpus becomes readable again in the same process
    (then it works and lands in the cache)."""
    pipeline._GATE_CORPUS.clear()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj1 = make_project(root, "p1")
        proj2 = make_project(root, "p2")
        expected = "\n".join(
            project.read_chapter(c.path)[1] for c in project.discover(proj1)
        )
        reads = {"n": 0}
        orig_rc = project.read_chapter

        def counting_rc(path):
            reads["n"] += 1
            return orig_rc(path)

        project.read_chapter = counting_rc
        try:
            corpus = pipeline._gate_corpus(proj1)
            check("10a cache: joins every discovered chapter body",
                  corpus == expected, f"corpus={corpus!r}")
            check("10b cache: one read per chapter on the first evaluation",
                  reads["n"] == 3, f"reads={reads['n']}")
            again = pipeline._gate_corpus(proj1)
            check("10c cache: a second evaluation re-reads nothing",
                  reads["n"] == 3 and again == corpus,
                  f"reads={reads['n']} same={again == corpus}")
            check("10d cache: keyed by the resolved project dir",
                  pipeline._GATE_CORPUS.get(proj1.resolve()) == corpus,
                  f"keys={list(pipeline._GATE_CORPUS)}")
            pipeline._gate_corpus(proj2)
            check("10e cache: a second project caches its own entry",
                  reads["n"] == 6 and len(pipeline._GATE_CORPUS) == 2,
                  f"reads={reads['n']} entries={len(pipeline._GATE_CORPUS)}")
        finally:
            project.read_chapter = orig_rc

        g_cached: dict = {"terms": []}
        _r, out_cached, _e = capture(
            pipeline._apply_glossary_proposal, g_cached, proposal("山谷"),
            2, {}, "", "[t]", proj1,
            corpus=pipeline._gate_corpus(proj1), min_occurrences=3)
        pipeline._GATE_CORPUS.clear()
        g_fresh: dict = {"terms": []}
        _r, out_fresh, _e = capture(
            pipeline._apply_glossary_proposal, g_fresh, proposal("山谷"),
            2, {}, "", "[t]", proj1,
            corpus=pipeline._gate_corpus(proj1), min_occurrences=3)
        check("10f cache: skip line identical against cached vs rebuilt corpus",
              out_cached == out_fresh
              == "[t] [glossary] skip '山谷' - 1 occurrence(s) across the "
                 "novel (min 3)\n"
              and g_cached == g_fresh,
              f"cached={out_cached!r} fresh={out_fresh!r}")
        g_min1: dict = {"terms": []}
        _r, out_min1, _e = capture(
            pipeline._apply_glossary_proposal, g_min1, proposal("山谷"),
            2, {}, "", "[t]", proj1,
            corpus=pipeline._gate_corpus(proj1), min_occurrences=1)
        check("10g cache: threshold unchanged (min 1 adds the same term)",
              out_min1 == "[t] [ok] glossary + '山谷' -> 'Spirit Term'\n"
              and [t.get("source") for t in g_min1["terms"]] == ["山谷"],
              f"out={out_min1!r}")

    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "p3")
        write_source(proj, "CHAPTER_0004.md",
                     "---\nchapter_title: [unclosed\n---\n\n正文。\n")
        pipeline._GATE_CORPUS.clear()
        reads = {"n": 0}
        orig_rc = project.read_chapter

        def counting_rc(path):
            reads["n"] += 1
            return orig_rc(path)

        project.read_chapter = counting_rc
        try:
            _r, _out, exc1 = capture(pipeline._gate_corpus, proj)
            check("10h fail-open: unreadable corpus raises ValueError",
                  isinstance(exc1, ValueError), f"exc={exc1!r}")
            check("10i fail-open: the failed read is not cached",
                  reads["n"] == 4
                  and proj.resolve() not in pipeline._GATE_CORPUS,
                  f"reads={reads['n']} keys={list(pipeline._GATE_CORPUS)}")
            _r, _out, exc2 = capture(pipeline._gate_corpus, proj)
            check("10j fail-open: EVERY evaluation re-reads and re-fails",
                  isinstance(exc2, ValueError) and reads["n"] == 8
                  and proj.resolve() not in pipeline._GATE_CORPUS,
                  f"reads={reads['n']} exc={exc2!r}")
            write_source(proj, "CHAPTER_0004.md", "山门前风平浪静。")
            corpus = pipeline._gate_corpus(proj)
            check("10k fail-open: a later readable corpus succeeds in-process",
                  "山门前风平浪静。" in corpus and reads["n"] == 12
                  and pipeline._GATE_CORPUS.get(proj.resolve()) == corpus,
                  f"reads={reads['n']}")
        finally:
            project.read_chapter = orig_rc


def make_gate_project(root: Path, name: str) -> Path:
    """Pipeline-shaped fixture for the gate integration: three one-line
    source chapters (灵石 occurs exactly once, in ch1), an accurate
    chapters.json manifest, config.json {"providers": {}}, an empty
    glossary.json, and the draft/ + translated/ dirs run_chapter persists
    state and output into (test_retry_feedback's make_project shape)."""
    proj = root / name
    proj.mkdir()
    bodies = {
        "CHAPTER_0001.md": "他捡起一块灵石。",
        "CHAPTER_0002.md": "山门外风平浪静。",
        "CHAPTER_0003.md": "山门前风平浪静。",
    }
    for fname, body in bodies.items():
        write_source(proj, fname, body)
    (proj / "chapters.json").write_text(
        json.dumps([{"file": f, "number": i + 1, "suffix": "", "order": i,
                     "status": "pending"}
                    for i, f in enumerate(sorted(bodies))],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proj / "config.json").write_text(
        json.dumps({"providers": {}}, indent=2) + "\n", encoding="utf-8")
    (proj / "glossary.json").write_text(
        json.dumps({"terms": []}, ensure_ascii=False) + "\n", encoding="utf-8")
    (proj / "draft").mkdir()
    (proj / "translated").mkdir()
    return proj


def make_gate_chat(terms_responses: list[list[dict]]):
    """pipeline._chat replacement (mock_server.py prompt sniffing): SUCCESS
    verdicts, one scripted GLOSSARY_EXPAND terms list per run, empty notes,
    and a 1-line translation -- one chapter = one run of the fake."""
    queue = list(terms_responses)

    def fake(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None,
            chapter=None):
        if "verdict" in prompt:
            return json.dumps({"verdict": "SUCCESS", "reasons": []},
                              ensure_ascii=False)
        if '"terms"' in prompt:
            return json.dumps({"terms": queue.pop(0)}, ensure_ascii=False)
        if '"notes"' in prompt or '"note"' in prompt:
            return json.dumps({"notes": []})
        return json.dumps(
            {"title": "Mock Title",
             "lines": [{"i": 1, "t": "Translated line 1."}]},
            ensure_ascii=False)

    return fake


def case_11_gate_corpus_run_chapter() -> None:
    """End to end through run_chapter: an unreadable sibling chapter makes
    the GLOSSARY_EXPAND occurrence gate fail open (warn + below-threshold
    term added) on EVERY chapter run -- the failure is not cached -- and
    after the chapter is repaired the SAME process applies the gate again:
    a fresh below-threshold proposal is skipped with the exact count line,
    no warn, and the now-readable corpus lands in the cache."""
    pipeline._GATE_CORPUS.clear()
    with tempfile.TemporaryDirectory() as td:
        proj = make_gate_project(Path(td), "proj")
        write_source(proj, "CHAPTER_0003.md",
                     "---\nchapter_title: [unclosed\n---\n\n正文。\n")
        cfg = config.load_config(proj)
        orig = pipeline._chat
        pipeline._chat = make_gate_chat([
            [proposal("灵石", "spirit stone")],
            [proposal("灵石", "spirit stone")],
            [proposal("道基", "foundation")],
        ])
        try:
            outcome1, out1, exc1 = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md", cfg)
            check("11a run: chapter 1 translates despite the unreadable corpus",
                  exc1 is None and outcome1 == "translated",
                  f"outcome={outcome1} exc={exc1!r}")
            check("11b run: fail-open warn names the unreadable corpus",
                  out1.count("occurrence gate disabled") == 1
                  and "[warn] occurrence gate disabled - source corpus "
                      "unreadable: ValueError" in out1,
                  f"out={out1!r}")
            check("11c run: gate disabled -> below-threshold term ADDED",
                  "[ok] glossary + '灵石' -> 'spirit stone'" in out1
                  and any(t.get("source") == "灵石"
                          for t in glossary.load(proj)["terms"]),
                  f"out={out1!r}")

            outcome2, out2, exc2 = capture(
                pipeline.run_chapter, proj, "CHAPTER_0002.md", cfg)
            check("11d run: a second evaluation warns AGAIN (not cached)",
                  exc2 is None and outcome2 == "translated"
                  and out2.count("occurrence gate disabled") == 1,
                  f"outcome={outcome2} out={out2!r}")

            write_source(proj, "CHAPTER_0003.md", "山门前风平浪静。")
            outcome3, out3, exc3 = capture(
                pipeline.run_chapter, proj, "CHAPTER_0003.md", cfg)
            check("11e run: readable corpus -> no fail-open warn",
                  exc3 is None and outcome3 == "translated"
                  and "occurrence gate disabled" not in out3,
                  f"outcome={outcome3} out={out3!r}")
            check("11f run: gate active again -> fresh below-threshold skip",
                  "[glossary] skip '道基' - 0 occurrence(s) across the novel "
                  "(min 3)" in out3
                  and all(t.get("source") != "道基"
                          for t in glossary.load(proj)["terms"]),
                  f"out={out3!r}")
            check("11g run: the repaired corpus is now cached in-process",
                  pipeline._GATE_CORPUS.get(proj.resolve())
                  == pipeline._gate_corpus(proj),
                  f"keys={list(pipeline._GATE_CORPUS)}")
        finally:
            pipeline._chat = orig


def case_12_merge_category_coercion() -> None:
    """Merge-path category coercion: a category ECHOED by the merge model
    is validated exactly like a new-term proposal's -- an unknown value is
    coerced to other with the warn line before the [ok] glossary ~ line
    (naming the proposal's source) -- while a merge that omits the category
    key never warns and leaves the entry's own category untouched.
    pipeline._chat is swapped for a fake returning a fixed merge JSON
    (case_11's try/finally monkeypatch pattern); the merge path runs
    because the proposal's translation differs from the entry's."""
    existing = {"source": "荒塔", "variants": [], "translation": "Old Tower",
                "definition": "A recurring term.", "category": "person",
                "origin": "seeded", "alt_translations": [],
                "first_seen_chapter": 0}

    def fake_chat_factory(merged: dict):
        def fake(project_dir, cfg, job, prompt, json_schema=None,
                 max_tokens=None, chapter=None):
            return json.dumps(merged, ensure_ascii=False)
        return fake

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        orig = pipeline._chat
        try:
            pipeline._chat = fake_chat_factory(
                {"translation": "Desolate Tower", "category": "faction"})
            g: dict = {"terms": [dict(existing)]}
            _r, out, exc = capture(
                pipeline._apply_glossary_proposal, g,
                proposal("荒塔", "Spirit Term"), 2, {}, "", "[t]", proj,
                corpus="", min_occurrences=3)
            check("12a merge: unknown echoed category stored as 'other'",
                  exc is None and len(g["terms"]) == 1
                  and g["terms"][0].get("category") == "other"
                  and g["terms"][0].get("translation") == "Desolate Tower",
                  f"exc={exc!r} terms={g.get('terms')}")
            check("12b merge: warn line precedes the [ok] glossary ~ line",
                  out == "[t] [glossary] warn unknown category 'faction' "
                          "for '荒塔' - coerced to 'other'\n"
                         "[t] [ok] glossary ~ '荒塔' -> 'Desolate Tower'\n",
                  f"out={out!r}")

            pipeline._chat = fake_chat_factory({"translation": "Desolate Tower"})
            g2: dict = {"terms": [dict(existing)]}
            _r, out2, exc2 = capture(
                pipeline._apply_glossary_proposal, g2,
                proposal("荒塔", "Spirit Term"), 2, {}, "", "[t]", proj,
                corpus="", min_occurrences=3)
            check("12c merge: omitted category -> no warn, category preserved",
                  exc2 is None and len(g2["terms"]) == 1
                  and g2["terms"][0].get("category") == "person"
                  and out2 == "[t] [ok] glossary ~ '荒塔' -> 'Desolate Tower'\n",
                  f"exc={exc2!r} out={out2!r} terms={g2.get('terms')}")

            pipeline._chat = fake_chat_factory(
                {"translation": "Desolate Tower", "category": "unit"})
            g3: dict = {"terms": [dict(existing)]}
            _r, out3, exc3 = capture(
                pipeline._apply_glossary_proposal, g3,
                proposal("荒塔", "Spirit Term"), 2, {}, "", "[t]", proj,
                corpus="", min_occurrences=3)
            check("12d merge: 'unit' with a surviving translation stored",
                  exc3 is None and len(g3["terms"]) == 1
                  and g3["terms"][0].get("category") == "unit"
                  and g3["terms"][0].get("translation") == "Desolate Tower",
                  f"exc={exc3!r} terms={g3.get('terms')}")
            check("12e merge: exact guide-only warn before the [ok] ~ line",
                  out3 == "[t] [warn] glossary: '荒塔' has a translation but "
                          "category 'unit' (guide-only: balance checks skip "
                          "it)\n"
                         "[t] [ok] glossary ~ '荒塔' -> 'Desolate Tower'\n",
                  f"out={out3!r}")

            empty_tr = dict(existing, translation="")
            pipeline._chat = fake_chat_factory({"category": "unit"})
            g4: dict = {"terms": [dict(empty_tr)]}
            _r, out4, exc4 = capture(
                pipeline._apply_glossary_proposal, g4,
                proposal("荒塔", "Spirit Term"), 2, {}, "", "[t]", proj,
                corpus="", min_occurrences=3)
            check("12f merge: echoed 'unit' on an empty translation no warn",
                  exc4 is None and len(g4["terms"]) == 1
                  and g4["terms"][0].get("category") == "unit"
                  and g4["terms"][0].get("translation") == ""
                  and out4 == "[t] [ok] glossary ~ '荒塔' -> ''\n",
                  f"exc={exc4!r} out={out4!r} terms={g4.get('terms')}")
        finally:
            pipeline._chat = orig


def case_13_balance_targets() -> None:
    """balance.count_in_target target handling: dedup is case-insensitive
    (matching is case-insensitive, so case variants are one target, first
    form kept) and the CJK exact-substring counting is per target, so a
    Latin sibling target in the same entry keeps word-boundary matching."""
    entry = {"source": "灵石", "translation": "Spirit Stone",
             "alt_translations": ["spirit stone"]}
    count = balance.count_in_target(
        entry, ["He found a spirit stone.", "The Spirit Stone glowed."])
    check("13a balance: case-variant duplicate target counted once",
          count == 2, f"count={count}")

    entry2 = {"source": "灵石", "translation": "灵石",
              "alt_translations": ["spirit stone"]}
    lines2 = ["灵石发光了。", "a spirit-stone here", "灵石碎了。"]
    count2 = balance.count_in_target(entry2, lines2)
    check("13b balance: CJK target exact, Latin target word-bounded",
          count2 == 3, f"count2={count2}")


def case_14_set_unit_guide_only_warn() -> None:
    """`glossary set --category unit` on an entry WITH a translation prints
    the exact guide-only advisory (the CLI counterpart of the pipeline's
    9h2 proposal warn: a 'unit' entry is a rendering guide only, balance
    checks skip it); a non-guide-only category and a unit set on an entry
    with an EMPTY translation stay warning-free."""
    warn = ("[warn] glossary: '灵根' has a translation but category 'unit' "
            "(guide-only: balance checks skip it)")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        glossary.save(proj, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root",
             "category": "item"},
        ]})
        ns = parse(["glossary", "set", "--source", "灵根",
                    "--category", "unit"])
        check("14a set: the parser hands cmd_glossary the set shape",
              ns is not None and ns.action == "set" and ns.source == "灵根"
              and ns.category == "unit", f"ns={ns}")
        code, out, exc = run_cli(translate.cmd_glossary, ns, proj)
        check("14b set unit: exit 0, no error",
              exc is None and code == 0, f"code={code} exc={exc!r}")
        check("14c set unit: the change line then the exact guide-only warn",
              out == "[glossary] set '灵根': category 'item' -> 'unit'\n"
                     + warn + "\n",
              f"out={out!r}")
        check("14d set unit: the category landed on disk",
              glossary.load(proj)["terms"][0].get("category") == "unit", "")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        glossary.save(proj, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root",
             "category": "item"},
        ]})
        ns = parse(["glossary", "set", "--source", "灵根",
                    "--category", "place"])
        code, out, exc = run_cli(translate.cmd_glossary, ns, proj)
        check("14e set place: exit 0, no warn",
              exc is None and code == 0 and "[warn]" not in out,
              f"code={code} exc={exc!r} out={out!r}")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        glossary.save(proj, {"terms": [
            {"source": "空词", "variants": [], "translation": "",
             "category": "item"},
        ]})
        ns = parse(["glossary", "set", "--source", "空词",
                    "--category", "unit"])
        code, out, exc = run_cli(translate.cmd_glossary, ns, proj)
        check("14f set unit empty translation: exit 0, no warn",
              exc is None and code == 0 and "[warn]" not in out,
              f"code={code} exc={exc!r} out={out!r}")


def main() -> int:
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
    case_10_gate_corpus_cache()
    case_11_gate_corpus_run_chapter()
    case_12_merge_category_coercion()
    case_13_balance_targets()
    case_14_set_unit_guide_only_warn()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
