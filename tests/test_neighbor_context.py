"""Tests for neighbor-chapter reference context: pipeline.build_neighbor_section
and its [Reference Chapters] injection into the TRANSLATE and FAITH prompts.

context_prev_chapters / context_next_chapters (DEFAULTS 1 / 1, 0 = off) select
the nearest neighbors of the running chapter in manifest order. Each neighbor
ships its source body and, when translated/<file> exists, its translated body
alongside it, framed as reference-only so the model never translates or
retranslates it. The block rides background_section() as an explicit part on
exactly two prompt shapes -- every TRANSLATE chunk (including the chunk-tail
continuity branch) and FAITH -- while build_ctx's default stays recap-only, so
the annotator's tn_generate prompt must NOT inherit it; recap, glossary and
verify prompts never see it either.

Covered: the unit table for build_neighbor_section (middle chapter renders
prev source + prev translation + next source in that order, counts 0/0 ->
"", first chapter has no Previous block and last has no Next block, a
neighbor without a translation ships source-only, a neighbor whose source is
missing drops out silently, a file absent from the manifest -> ""); and the
run_chapter integration on the middle chapter of a three-chapter project
(outcome "translated"; the translate prompt carries the header, the
do-not-translate framing, both Previous labels and bodies, and the next
chapter's source but not a next translation that does not exist; the FAITH
verdict prompt carries the same block; the notes prompt does not; writing
translated/<next> first makes its translation appear with the Next
translation label; counts 0/0 keep [Reference Chapters] out of every
recorded prompt).

Mechanics mirror the sibling suites: every fixture lives in a
TemporaryDirectory sandbox built with test_sync's write_source pattern
(three chapters under source/, plain body lines without frontmatter, plus
an accurate chapters.json manifest, a config.json with a one-block
translator, an empty glossary.json, and the draft/ + translated/ dirs
run_chapter persists state and output into), and all model calls go through
a monkeypatched pipeline._chat whose responses are sniffed from the prompt
content exactly like tests/mock_server.py: "verdict" -> FAITH reviewer,
'"recap"' -> story recap, '"terms"' -> glossary expansion,
'"notes"'/'"note"' -> note generation, anything else -> the TRANSLATE call.
pipeline.py imports the whole lib package (requests, ebooklib, pillow,
pyyaml).

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_neighbor_context.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0", "openai>=1.60", "anthropic>=1.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config, pipeline, project

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


def sniff(prompt: str) -> str:
    """Which pipeline role a prompt targets (mock_server.py convention)."""
    if "verdict" in prompt:
        return "verdict"
    if '"recap"' in prompt:
        return "recap"
    if '"terms"' in prompt:
        return "terms"
    if '"notes"' in prompt or '"note"' in prompt:
        return "notes"
    return "translate"


def make_fake_chat(calls: list[dict], verdicts: list[tuple[str, list[str]]]):
    """pipeline._chat replacement: records {"job", "prompt"} per call and
    answers from the scripted verdict queue / canned role responses."""

    def fake(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None,
            chapter=None):
        calls.append({"job": job, "prompt": prompt})
        if "verdict" in prompt:
            verdict, reasons = verdicts.pop(0)
            return json.dumps({"verdict": verdict, "reasons": reasons},
                              ensure_ascii=False)
        if '"recap"' in prompt:
            return json.dumps({"recap": "Mock recap of the story so far."})
        if '"terms"' in prompt:
            return json.dumps({"terms": []})
        if '"notes"' in prompt or '"note"' in prompt:
            return json.dumps({"notes": []})
        return json.dumps(
            {"title": "Mock Title",
             "lines": [{"i": i, "t": f"Translated line {i}."}
                       for i in range(1, 4)]},
            ensure_ascii=False)

    return fake


def write_source(root: Path, name: str, text: str) -> None:
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    with open(source / name, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_translation(root: Path, name: str, text: str) -> None:
    translated = root / "translated"
    translated.mkdir(parents=True, exist_ok=True)
    with open(translated / name, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


BODIES = {
    "CHAPTER_0001.md": "Alpha one.\nAlpha two.\nAlpha three.",
    "CHAPTER_0002.md": "Beta one.\nBeta two.\nBeta three.",
    "CHAPTER_0003.md": "Gamma one.\nGamma two.\nGamma three.",
}
PREV_TRANSLATION = "Alpha rendered one.\nAlpha rendered two.\nAlpha rendered three."
NEXT_TRANSLATION = "Gamma rendered one.\nGamma rendered two.\nGamma rendered three."

MANIFEST = [
    {"file": fname, "number": i + 1, "suffix": "", "order": i, "status": "pending"}
    for i, fname in enumerate(sorted(BODIES))
]


def make_unit_root(root: Path, translations: dict[str, str] | None = None,
                   sources: dict[str, str] | None = None) -> dict:
    """A bare source/+translated/ pair for build_neighbor_section unit
    cases; defaults write every BODIES chapter and no translations."""
    for fname, body in (sources if sources is not None else BODIES).items():
        write_source(root, fname, body)
    for fname, text in (translations or {}).items():
        write_translation(root, fname, text)
    return {"source": root / "source", "translated": root / "translated"}


def make_project(root: Path, name: str, cfg_extra: dict | None = None,
                 translations: dict[str, str] | None = None) -> Path:
    """Minimal three-chapter project: the BODIES chapters under source/, an
    accurate chapters.json manifest, a config.json with a one-block
    translator + cfg_extra, an empty glossary.json, and the draft/ +
    translated/ dirs run_chapter persists state and output into; the
    `translations` entries are pre-written neighbor translations."""
    proj = root / name
    proj.mkdir()
    for fname, body in BODIES.items():
        write_source(proj, fname, body)
    for fname, text in (translations or {}).items():
        write_translation(proj, fname, text)
    (proj / "chapters.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cfg = {"providers": {"translator": {"base_url": "http://test.invalid/v1"}}}
    cfg.update(cfg_extra or {})
    (proj / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proj / "glossary.json").write_text(
        json.dumps({"terms": []}, ensure_ascii=False) + "\n", encoding="utf-8")
    (proj / "draft").mkdir()
    (proj / "translated").mkdir(exist_ok=True)
    return proj


def case_1_unit_middle_full() -> None:
    """Middle chapter, prev translation only: the block renders the header,
    the do-not-translate framing, prev source then prev translation, and the
    next chapter's source -- never a next translation that does not exist."""
    with tempfile.TemporaryDirectory() as td:
        paths = make_unit_root(Path(td), {"CHAPTER_0001.md": PREV_TRANSLATION})
        got = pipeline.build_neighbor_section(paths, MANIFEST, "CHAPTER_0002.md", {})
        check("1a unit: block starts with the [Reference Chapters] header",
              got.startswith("[Reference Chapters]"), f"got {got[:80]!r}")
        check("1b unit: framing forbids translating the neighbors",
              "do NOT translate or retranslate them" in got, f"got {got!r}")
        check("1c unit: prev source label and body",
              "--- Previous chapter (source) ---" in got and "Alpha one." in got,
              f"got {got!r}")
        check("1d unit: prev translation label and body",
              "--- Previous chapter (translation) ---" in got
              and "Alpha rendered one." in got, f"got {got!r}")
        check("1e unit: next source label and body",
              "--- Next chapter (source) ---" in got and "Gamma one." in got,
              f"got {got!r}")
        check("1f unit: no next translation when none exists",
              "Gamma rendered" not in got, f"got {got!r}")
        check("1g unit: prev source ships before prev translation",
              got.index("--- Previous chapter (source) ---")
              < got.index("--- Previous chapter (translation) ---"),
              f"got {got!r}")


def case_2_unit_counts_zero() -> None:
    """Both counts 0 -> the empty string, before any file access."""
    with tempfile.TemporaryDirectory() as td:
        paths = make_unit_root(Path(td))
        got = pipeline.build_neighbor_section(
            paths, MANIFEST, "CHAPTER_0002.md",
            {"context_prev_chapters": 0, "context_next_chapters": 0})
        check("2a unit: counts 0/0 -> ''", got == "", f"got {got!r}")
        got_one_side = pipeline.build_neighbor_section(
            paths, MANIFEST, "CHAPTER_0002.md",
            {"context_prev_chapters": 0, "context_next_chapters": 1})
        check("2b unit: one-sided count still renders the other side",
              got_one_side != "" and "Previous" not in got_one_side
              and "Gamma one." in got_one_side, f"got {got_one_side!r}")


def case_3_unit_first_and_last() -> None:
    """Chapter 1 has no Previous block (its Next carries chapter 2's source);
    the last chapter has no Next block (its Previous carries chapter 2)."""
    with tempfile.TemporaryDirectory() as td:
        paths = make_unit_root(Path(td))
        first = pipeline.build_neighbor_section(paths, MANIFEST, "CHAPTER_0001.md", {})
        check("3a unit: chapter 1 has no Previous block",
              "Previous" not in first, f"got {first!r}")
        check("3b unit: chapter 1 still sees chapter 2's source",
              "--- Next chapter (source) ---" in first and "Beta one." in first,
              f"got {first!r}")
        last = pipeline.build_neighbor_section(paths, MANIFEST, "CHAPTER_0003.md", {})
        check("3c unit: last chapter has no Next block",
              "Next" not in last, f"got {last!r}")
        check("3d unit: last chapter still sees chapter 2's source",
              "--- Previous chapter (source) ---" in last and "Beta one." in last,
              f"got {last!r}")


def case_4_unit_missing_pieces() -> None:
    """A neighbor without a translation ships source-only; a neighbor whose
    source is missing drops out entirely; when nothing qualifies the block
    is empty."""
    with tempfile.TemporaryDirectory() as td:
        paths = make_unit_root(Path(td))
        got = pipeline.build_neighbor_section(paths, MANIFEST, "CHAPTER_0002.md", {})
        check("4a unit: prev without a translation ships source-only",
              "--- Previous chapter (source) ---" in got
              and "--- Previous chapter (translation) ---" not in got,
              f"got {got!r}")
        orphan = [{"file": "CHAPTER_0004.md", "number": 4, "order": 3,
                   "status": "pending"}]
        got_orphan = pipeline.build_neighbor_section(
            paths, MANIFEST + orphan, "CHAPTER_0002.md",
            {"context_prev_chapters": 0, "context_next_chapters": 2})
        check("4b unit: neighbor with unreadable source drops out silently",
              "CHAPTER_0004" not in got_orphan
              and "Gamma one." in got_orphan, f"got {got_orphan!r}")
        alone = [{"file": "CHAPTER_0002.md", "number": 2, "order": 0,
                  "status": "pending"}]
        got_empty = pipeline.build_neighbor_section(
            paths, alone, "CHAPTER_0002.md", {})
        check("4c unit: manifest with no qualifying neighbor -> ''",
              got_empty == "", f"got {got_empty!r}")
        got_absent = pipeline.build_neighbor_section(
            paths, MANIFEST, "CHAPTER_0099.md", {})
        check("4d unit: file absent from the manifest -> ''",
              got_absent == "", f"got {got_absent!r}")


def case_5_unit_prev_window() -> None:
    """context_prev_chapters 2 on chapter 3 walks back two chapters in
    manifest order (oldest first) -- both bodies ship, never chapter 1's
    translation."""
    with tempfile.TemporaryDirectory() as td:
        paths = make_unit_root(Path(td), {"CHAPTER_0001.md": PREV_TRANSLATION})
        got = pipeline.build_neighbor_section(
            paths, MANIFEST, "CHAPTER_0003.md",
            {"context_prev_chapters": 2, "context_next_chapters": 0})
        check("5a unit: two Previous blocks render",
              got.count("--- Previous chapter (source) ---") == 2, f"got {got!r}")
        check("5b unit: oldest neighbor first",
              got.index("Beta one.") < got.index("Alpha one."), f"got {got!r}")
        check("5c unit: prev translations ride along when they exist",
              "--- Previous chapter (translation) ---" in got
              and "Alpha rendered one." in got, f"got {got!r}")


def case_6_integration_prompts() -> None:
    """run_chapter on the middle chapter with only the prev translation
    pre-written: the translate prompt carries the full block, the FAITH
    verdict prompt carries it too, and the notes prompt does not."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", None,
                            {"CHAPTER_0001.md": PREV_TRANSLATION})
        calls: list[dict] = []
        verdicts = [("SUCCESS", [])]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, verdicts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0002.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("6a integration: run_chapter returns 'translated'",
              exc is None and outcome == "translated", f"exc={exc!r}")
        translates = [c["prompt"] for c in calls if sniff(c["prompt"]) == "translate"]
        check("6b integration: a translate prompt was recorded",
              len(translates) >= 1, f"kinds={[sniff(c['prompt']) for c in calls]}")
        tp = translates[0]
        check("6c integration: translate prompt has the header and framing",
              "[Reference Chapters]" in tp
              and "do NOT translate or retranslate them" in tp, f"prompt={tp[:400]!r}")
        check("6d integration: translate prompt has prev source and translation",
              "--- Previous chapter (source) ---" in tp and "Alpha one." in tp
              and "--- Previous chapter (translation) ---" in tp
              and "Alpha rendered one." in tp, f"prompt={tp[:400]!r}")
        check("6e integration: translate prompt has next source only",
              "--- Next chapter (source) ---" in tp and "Gamma one." in tp
              and "Gamma rendered" not in tp, f"prompt={tp[:400]!r}")
        verdicts_prompts = [c["prompt"] for c in calls if sniff(c["prompt"]) == "verdict"]
        fp = verdicts_prompts[0] if verdicts_prompts else ""
        check("6f integration: faith prompt carries the same block",
              "[Reference Chapters]" in fp and "Alpha one." in fp
              and "Gamma one." in fp, f"prompt={fp[:400]!r}")
        notes_prompts = [c["prompt"] for c in calls if sniff(c["prompt"]) == "notes"]
        np_ = notes_prompts[0] if notes_prompts else ""
        check("6g integration: notes prompt stays neighbor-free",
              np_ != "" and "[Reference Chapters]" not in np_,
              f"prompt={np_[:200]!r}")
        check("6h integration: translated chapter exists",
              (proj / "translated" / "CHAPTER_0002.md").is_file())


def case_7_integration_next_translation() -> None:
    """With the next chapter's translation also pre-written (retranslation
    scenario), the translate prompt gains the Next translation label and
    body alongside its source."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", None,
                            {"CHAPTER_0001.md": PREV_TRANSLATION,
                             "CHAPTER_0003.md": NEXT_TRANSLATION})
        calls: list[dict] = []
        verdicts = [("SUCCESS", [])]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, verdicts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0002.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("7a integration: run_chapter returns 'translated'",
              exc is None and outcome == "translated", f"exc={exc!r}")
        translates = [c["prompt"] for c in calls if sniff(c["prompt"]) == "translate"]
        tp = translates[0] if translates else ""
        check("7b integration: next translation rides along with its source",
              "--- Next chapter (translation) ---" in tp
              and "Gamma rendered one." in tp
              and "--- Next chapter (source) ---" in tp, f"prompt={tp[:400]!r}")


def case_8_integration_counts_zero() -> None:
    """context_prev_chapters 0 + context_next_chapters 0 keep the block out
    of every recorded prompt, neighbors or not."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(
            Path(td), "proj",
            {"context_prev_chapters": 0, "context_next_chapters": 0},
            {"CHAPTER_0001.md": PREV_TRANSLATION})
        calls: list[dict] = []
        verdicts = [("SUCCESS", [])]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, verdicts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0002.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("8a integration: run_chapter returns 'translated'",
              exc is None and outcome == "translated", f"exc={exc!r}")
        leaked = [c["job"] for c in calls
                  if "[Reference Chapters]" in c["prompt"]]
        check("8b integration: no prompt carries the block",
              leaked == [], f"leaked jobs={leaked}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_unit_middle_full()
    case_2_unit_counts_zero()
    case_3_unit_first_and_last()
    case_4_unit_missing_pieces()
    case_5_unit_prev_window()
    case_6_integration_prompts()
    case_7_integration_next_translation()
    case_8_integration_counts_zero()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
