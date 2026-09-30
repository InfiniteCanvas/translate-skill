"""Tests for lib/review_notes.py: the `review notes` advisory audit tier.

Covers anchor re-resolution (stored index wins while its line still starts
with the anchor; a hand-edited body that shifted lines re-locates the note
by anchor; a line rewritten past recognition makes the note unresolvable ->
deterministic misanchored warn with NO model call); source pairing (the
source line at the resolved index, empty string past a shorter source, the
pipeline's leading-title drop) and the +-2-line target context windows;
closed-vocabulary validation of model output (unknown kind / severity /
idx and empty-reason rows dropped); global batching across chapters in
manifest order (ceil(units/batch_size) calls); chapter selection (an
explicit --chapters spec is honored, chapters without sidecars are skipped
silently, the default selects every chapter with a sidecar, a missing
translated file warns and skips); the report (frontmatter tallies incl.
tier + per-kind counts, findings rendered with chapter/term/kind/severity/
reason/suggestion, NO `- Command:` bullets anywhere, the tn re-check
overwrite footer, a custom review_report_path, and the per-run overwrite
semantics -- a notes run replaces a previous glossary report); and the CLI
smoke (translate.main("review notes ...") runs end-to-end with a stubbed
provider and exits 0 regardless of findings; --fix / --batch-size 0 are
usage errors).

Every case builds a full sandbox project (source/, translated/,
chapters.json, sidecars) inside tempfile.TemporaryDirectory() -- repo
fixtures are never touched. Model calls are stubbed by swapping
lib.client.chat via review_notes.client (module-attribute lookup at the
call site), the same convention as test_glossary_review.py. Files are
written with explicit LF newlines so byte-level comparisons are
deterministic.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_review_notes.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import review_notes, tn  # noqa: E402
import translate  # noqa: E402

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


# ------------------------------------------------------------------ fixtures

SOURCE_MD = (
    "---\n"
    "chapter_title: 第一章 灵根\n"
    "---\n"
    "\n"
    "源文一行。\n"
    "源文二行。\n"
    "源文三行。\n"
)

# 3-line translated body; note indexes/anchors are given per case.
TRANSLATED_MD = (
    "---\n"
    "chapter_title: 第一章 灵根\n"
    "title: Spirit Root Awakening\n"
    "---\n"
    "\n"
    "Alpha line with 灵根.\n"
    "Beta line.\n"
    "Gamma line.\n"
)


def make_project(td: str, chapters: list[dict]) -> Path:
    """Sandbox project: source/, translated/, chapters.json. Each chapter
    dict: {file, order, status?, source_md?, translated_md?} (texts optional
    -- omit one to simulate a missing file)."""
    root = Path(td)
    manifest = []
    for ch in chapters:
        number = int(Path(ch["file"]).stem.split("_")[1])
        manifest.append({
            "file": ch["file"], "number": number, "suffix": "",
            "order": ch["order"], "status": ch.get("status", "translated"),
            "title": "Title",
        })
        if ch.get("source_md") is not None:
            write_lf(root / "source" / ch["file"], ch["source_md"])
        if ch.get("translated_md") is not None:
            write_lf(root / "translated" / ch["file"], ch["translated_md"])
    write_lf(root / "chapters.json",
             json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return root


def write_sidecar(root: Path, file: str, notes: list[dict]) -> None:
    """Hand-written sidecar so line/anchor pairs are exactly as specified."""
    path = tn.notes_path(root, file)
    write_lf(path, json.dumps({
        "chapter": file,
        "updated_at": "2026-09-30T00:00:00+00:00",
        "notes": notes,
    }, ensure_ascii=False, indent=2) + "\n")


def review_cfg(**overrides) -> dict:
    """Minimal cfg the engine accepts; the reviewer provider is never
    actually called (client.chat is stubbed)."""
    cfg = {
        "source_lang": "zh", "target_lang": "en", "log_llm": False,
        "providers": {"reviewer": {"base_url": "http://unused", "model": None}},
    }
    cfg.update(overrides)
    return cfg


def run_with_fake_chat(root: Path, rows_by_call: list, chapters: str | None = None,
                       batch_size: int | None = None, cfg: dict | None = None):
    """audit_notes with lib.client.chat stubbed per call. rows_by_call: one
    entry per expected call -- a dict (returned as JSON) or an Exception
    (raised); calls past the list get an empty findings object, so passing
    [] asserts 'the model is never called'. Returns (result, prompts)."""
    prompts: list[str] = []

    def fake_chat(provider_cfg, prompt, json_schema=None, temperature=None,
                  max_tokens=None, meta_hook=None):
        prompts.append(prompt)
        step = (rows_by_call[len(prompts) - 1]
                if len(prompts) <= len(rows_by_call) else {"findings": []})
        if isinstance(step, Exception):
            raise step
        return json.dumps(step, ensure_ascii=False)

    orig = review_notes.client.chat
    review_notes.client.chat = fake_chat
    try:
        result = review_notes.audit_notes(
            root, cfg or review_cfg(), chapters=chapters, batch_size=batch_size)
    finally:
        review_notes.client.chat = orig
    return result, prompts


def run_full_with_fake_chat(root: Path, rows_by_call: list,
                            cfg: dict | None = None, **kw) -> int:
    """review_notes() (audit + report + console) with the same stub."""
    prompts: list[str] = []

    def fake_chat(provider_cfg, prompt, json_schema=None, temperature=None,
                  max_tokens=None, meta_hook=None):
        prompts.append(prompt)
        step = (rows_by_call[len(prompts) - 1]
                if len(prompts) <= len(rows_by_call) else {"findings": []})
        if isinstance(step, Exception):
            raise step
        return json.dumps(step, ensure_ascii=False)

    orig = review_notes.client.chat
    review_notes.client.chat = fake_chat
    try:
        count = review_notes.review_notes(root, cfg or review_cfg(), **kw)
    finally:
        review_notes.client.chat = orig
    return count


def unit_from_prompt(prompt: str, idx: int) -> dict:
    """Parse review unit idx's JSON line back out of a rendered prompt
    (units are embedded one compact JSON object per line)."""
    line = next((ln for ln in prompt.splitlines() if f'"idx": {idx}' in ln), "")
    return json.loads(line) if line else {}


def clean_text(root: Path) -> str:
    return (root / "review-report.md").read_text(encoding="utf-8")


NOTE_A = {"line": 0, "term": "灵根", "note": "Spirit root: innate cultivation aptitude.",
          "category": "cultural", "anchor": "Alpha line with 灵根."}
NOTE_B = {"line": 1, "term": "筑基", "note": "Foundation Establishment is the second realm.",
          "category": "cultural", "anchor": "Beta line."}


# ---------------------------------------------------------------------- cases


def case_1_anchor_resolution() -> None:
    """Stored index wins; drift recovers by anchor; unresolvable ->
    deterministic misanchored without a model call."""
    # A: matching index + anchor -> resolves at the stored line
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        write_sidecar(root, "Chapter_0001.md", [dict(NOTE_A), dict(NOTE_B)])
        result, prompts = run_with_fake_chat(root, [{"findings": []}])
        check("1a anchor: both notes became units", result["units"] == 2,
              f"units={result['units']}")
        unit_a = unit_from_prompt(prompts[0], 0) if prompts else {}
        unit_b = unit_from_prompt(prompts[0], 1) if prompts else {}
        check("1b anchor: matching index resolves at the stored line",
              result["findings"] == []
              and unit_a.get("line") == 0
              and unit_a.get("translated_line") == "Alpha line with 灵根."
              and unit_b.get("line") == 1,
              f"a={unit_a} b={unit_b}")

    # B: hand-edit inserted a first line -> stored index no longer matches,
    #    the anchor re-locates the note one line lower
    with tempfile.TemporaryDirectory() as td:
        drifted = TRANSLATED_MD.replace(
            "Alpha line with 灵根.", "New zeroth line.\nAlpha line with 灵根.")
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD, "translated_md": drifted},
        ])
        write_sidecar(root, "Chapter_0001.md", [dict(NOTE_A)])
        result, prompts = run_with_fake_chat(root, [{"findings": []}])
        unit = unit_from_prompt(prompts[0], 0) if prompts else {}
        check("1c anchor: drifted index recovered by anchor (line 0 -> 1)",
              result["units"] == 1 and result["findings"] == []
              and unit.get("line") == 1
              and unit.get("translated_line") == "Alpha line with 灵根.",
              f"unit={unit}")

    # C: the owning line was rewritten past recognition -> no line starts
    #    with the anchor -> deterministic misanchored, NO model call
    with tempfile.TemporaryDirectory() as td:
        rewritten = TRANSLATED_MD.replace(
            "Alpha line with 灵根.", "Completely different prose now.")
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD, "translated_md": rewritten},
        ])
        write_sidecar(root, "Chapter_0001.md", [dict(NOTE_A)])
        result, prompts = run_with_fake_chat(root, [])
        fs = result["findings"]
        check("1d anchor: unresolvable -> exactly one misanchored finding",
              len(fs) == 1 and fs[0]["kind"] == "misanchored",
              f"findings={fs}")
        if fs:
            check("1e anchor: deterministic warn with the fixed wording",
                  fs[0]["severity"] == "warn"
                  and fs[0]["origin"] == "deterministic"
                  and fs[0]["reason"] == "anchor no longer matches any translated line"
                  and fs[0]["suggestion"] == "re-attach or delete the note",
                  f"finding={fs[0]}")
        check("1f anchor: unresolvable note never reaches the model",
              prompts == [] and result["batches"] == 0,
              f"calls={len(prompts)}")


def case_2_source_pairing_and_context() -> None:
    """Units pair the source line at the resolved index (empty past a
    shorter source, with the pipeline's leading-title drop) and carry the
    +-2-line target context windows. The unit JSON is read back out of the
    prompt (one compact object per line, like the glossary tier's entries)."""
    five_line_body = (
        "---\n"
        "chapter_title: 第一章\n"
        "---\n"
        "\n"
        "One.\nTwo.\nThree.\nFour.\nFive.\n"
    )

    # aligned source (4 lines after the leading-title drop) + note mid-body
    with tempfile.TemporaryDirectory() as td:
        aligned_source = (
            "---\n"
            "chapter_title: 第一章\n"
            "---\n"
            "\n"
            "第一章\n"
            "源一。\n源二。\n源三。\n源四。\n"
        )
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": aligned_source, "translated_md": five_line_body},
        ])
        write_sidecar(root, "Chapter_0001.md", [
            {"line": 2, "term": "灵根", "note": "n", "category": "cultural",
             "anchor": "Three."},
        ])
        result, prompts = run_with_fake_chat(root, [{"findings": []}])
        check("2a pairing: one unit, no findings",
              result["units"] == 1 and result["findings"] == []
              and len(prompts) == 1,
              f"result={result}")
        unit = unit_from_prompt(prompts[0], 0)
        check("2b pairing: resolved line index carried on the unit",
              unit["line"] == 2, f"unit={unit}")
        check("2c pairing: translated/source lines index-aligned",
              unit["translated_line"] == "Three."
              and unit["source_line"] == "源三。",
              f"unit={unit}")
        check("2d pairing: unit carries exactly the documented fields "
              "(title drop already proven by 2c: line 2 pairs 源三, not 源二)",
              set(unit) == {"idx", "chapter", "line", "term", "note", "category",
                            "translated_line", "source_line",
                            "context_before", "context_after"},
              f"keys={sorted(unit)}")
        check("2e pairing: +-2-line target context windows",
              unit["context_before"] == "One.\nTwo."
              and unit["context_after"] == "Four.\nFive.",
              f"unit={unit}")

    # source SHORTER than the translation: the missing line pairs as ""
    with tempfile.TemporaryDirectory() as td:
        short_source = (
            "---\n"
            "chapter_title: 第一章\n"
            "---\n"
            "\n"
            "第一章\n"
            "源一。\n"
        )
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": short_source, "translated_md": five_line_body},
        ])
        write_sidecar(root, "Chapter_0001.md", [
            {"line": 2, "term": "灵根", "note": "n", "category": "other",
             "anchor": "Three."},
        ])
        _result, prompts = run_with_fake_chat(root, [{"findings": []}])
        unit = unit_from_prompt(prompts[0], 0)
        check("2f pairing: source shorter than translation -> empty source_line",
              unit["translated_line"] == "Three." and unit["source_line"] == "",
              f"unit={unit}")
        check("2g pairing: context windows still filled from the target side",
              unit["context_before"] == "One.\nTwo."
              and unit["context_after"] == "Four.\nFive.",
              f"unit={unit}")


def case_3_closed_vocab_validation() -> None:
    """Model rows outside the closed vocabulary are dropped (not
    normalized); only the valid row survives."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        write_sidecar(root, "Chapter_0001.md", [dict(NOTE_A), dict(NOTE_B)])
        rows = {"findings": [
            {"idx": 0, "kind": "restates", "severity": "warn",
             "reason": "note paraphrases the line", "suggestion": ""},
            {"idx": 0, "kind": "bizarre", "severity": "warn",
             "reason": "unknown kind", "suggestion": ""},
            {"idx": 1, "kind": "wrong", "severity": "garbage",
             "reason": "bad severity", "suggestion": ""},
            {"idx": 99, "kind": "wrong", "severity": "warn",
             "reason": "idx not in batch", "suggestion": ""},
            {"idx": 1, "kind": "overexplains", "severity": "info",
             "reason": "", "suggestion": ""},
            {"idx": "0", "kind": "wrong", "severity": "warn",
             "reason": "string idx", "suggestion": ""},
            "not-a-dict",
        ]}
        result, _prompts = run_with_fake_chat(root, [rows])
        fs = result["findings"]
        check("3a vocab: 7 rows -> 1 finding", len(fs) == 1,
              f"findings={fs}")
        if fs:
            check("3b vocab: the valid row passes verbatim",
                  fs[0]["idx"] == 0 and fs[0]["kind"] == "restates"
                  and fs[0]["severity"] == "warn"
                  and fs[0]["origin"] == "model"
                  and fs[0]["chapter"] == "Chapter_0001.md"
                  and fs[0]["term"] == "灵根",
                  f"finding={fs[0]}")


def case_4_batching() -> None:
    """Units flatten across chapters in manifest order and batch by
    batch_size: 5 units / batch 2 -> 3 batches, 3 calls."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD,
             "translated_md": TRANSLATED_MD.replace("灵根", "灵根A")},
            {"file": "Chapter_0002.md", "order": 1,
             "source_md": SOURCE_MD,
             "translated_md": TRANSLATED_MD.replace("灵根", "灵根B")},
        ])
        write_sidecar(root, "Chapter_0001.md", [
            {"line": 0, "term": f"词一{i}", "note": f"note {i}", "category": "other",
             "anchor": "Alpha line with 灵根A."} for i in (1, 2, 3)
        ])
        write_sidecar(root, "Chapter_0002.md", [
            {"line": 0, "term": f"词二{i}", "note": f"note {i}", "category": "other",
             "anchor": "Alpha line with 灵根B."} for i in (4, 5)
        ])
        rows_by_call = [
            {"findings": [{"idx": 0, "kind": "restates", "severity": "warn",
                           "reason": "b1", "suggestion": ""}]},
            {"findings": [{"idx": 2, "kind": "wrong", "severity": "warn",
                           "reason": "b2", "suggestion": "fixed note"}]},
            {"findings": [{"idx": 4, "kind": "overexplains", "severity": "info",
                           "reason": "b3", "suggestion": ""}]},
        ]
        result, prompts = run_with_fake_chat(root, rows_by_call, batch_size=2)
        check("4a batching: 5 units / batch 2 -> 3 batches, 3 calls",
              result["batches"] == 3 and len(prompts) == 3,
              f"batches={result['batches']} calls={len(prompts)}")
        check("4b batching: batch 1 holds chapter 1's first two notes",
              '"term": "词一1"' in prompts[0] and '"term": "词一2"' in prompts[0]
              and '"term": "词一3"' not in prompts[0],
              f"prompt0={prompts[0][:300]!r}")
        check("4c batching: batch 2 crosses the chapter boundary (ch1 tail + ch2 head)",
              '"term": "词一3"' in prompts[1] and '"term": "词二4"' in prompts[1],
              f"prompt1={prompts[1][:300]!r}")
        check("4d batching: batch 3 holds the last unit only",
              '"term": "词二5"' in prompts[2] and '"term": "词二4"' not in prompts[2],
              "")
        check("4e batching: idx maps findings back to the right chapter",
              {(f["chapter"], f["term"]) for f in result["findings"]}
              == {("Chapter_0001.md", "词一1"), ("Chapter_0001.md", "词一3"),
                  ("Chapter_0002.md", "词二5")},
              f"findings={result['findings']}")

        # batch failure resilience (mirrors review.py): one failed batch is
        # reported, the others' findings survive
        rows_by_call = [
            {"findings": [{"idx": 0, "kind": "restates", "severity": "warn",
                           "reason": "b1", "suggestion": ""}]},
            RuntimeError("mock batch failure"),
            {"findings": [{"idx": 4, "kind": "restates", "severity": "warn",
                           "reason": "b3", "suggestion": ""}]},
        ]
        result, _prompts = run_with_fake_chat(root, rows_by_call, batch_size=2)
        check("4f batching: failed batch recorded, others survive",
              len(result["batch_errors"]) == 1
              and "batch 2/3" in result["batch_errors"][0]
              and {f["term"] for f in result["findings"]} == {"词一1", "词二5"},
              f"errors={result['batch_errors']} findings={result['findings']}")


def case_5_chapter_selection() -> None:
    """--chapters spec honored; chapters without sidecars skipped silently;
    default = every chapter with a sidecar; missing translated file warns."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
            {"file": "Chapter_0002.md", "order": 1,
             "source_md": SOURCE_MD,
             "translated_md": TRANSLATED_MD.replace("灵根", "灵根B")},
            # chapter 3 has files but NO sidecar
            {"file": "Chapter_0003.md", "order": 2,
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        write_sidecar(root, "Chapter_0001.md", [dict(NOTE_A)])
        write_sidecar(root, "Chapter_0002.md", [
            {"line": 0, "term": "词二", "note": "note two", "category": "other",
             "anchor": "Alpha line with 灵根B."},
        ])

        # explicit spec: only chapter 2 (spec syntax same as `tn`)
        result, prompts = run_with_fake_chat(root, [{"findings": []}],
                                             chapters="2")
        check("5a selection: --chapters 2 audits chapter 2 only",
              result["chapters"] == ["Chapter_0002.md"]
              and '"term": "词二"' in prompts[0]
              and '"term": "灵根"' not in prompts[0],
              f"chapters={result['chapters']}")

        # spec spanning a sidecar-less chapter: skipped silently (both
        # chapters' 2 notes fit one default-size batch -> 1 call)
        result, prompts = run_with_fake_chat(root, [{"findings": []}],
                                             chapters="1-3")
        check("5b selection: chapters without sidecars skipped silently",
              result["chapters"] == ["Chapter_0001.md", "Chapter_0002.md"]
              and result["skipped"] == [] and len(prompts) == 1,
              f"chapters={result['chapters']} skipped={result['skipped']}")

        # default: every chapter with a sidecar
        result, prompts = run_with_fake_chat(root, [{"findings": []}, {"findings": []}])
        check("5c selection: default = every chapter with a sidecar",
              result["chapters"] == ["Chapter_0001.md", "Chapter_0002.md"],
              f"chapters={result['chapters']}")

    # a sidecar whose translated file is missing: warn and skip
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "source_md": SOURCE_MD},
            {"file": "Chapter_0002.md", "order": 1,
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        write_sidecar(root, "Chapter_0001.md", [dict(NOTE_A)])
        write_sidecar(root, "Chapter_0002.md", [dict(NOTE_B)])
        result, prompts = run_with_fake_chat(root, [{"findings": []}])
        check("5d selection: missing translated chapter warns and skips",
              result["chapters"] == ["Chapter_0002.md"]
              and result["skipped"] == ["Chapter_0001.md"],
              f"result={result}")
        check("5e selection: skipped chapter sends no units",
              len(prompts) == 1 and "Beta line." in prompts[0]
              and '"chapter": "Chapter_0001.md"' not in prompts[0]
              and '"term": "灵根"' not in prompts[0],
              f"prompt={prompts[0][:200]!r}")


def case_6_report() -> None:
    """Report: frontmatter tallies (tier + per-kind counts), rendered
    findings, no Command bullets, the tn re-check footer, custom path, and
    the per-run overwrite of a previous glossary report."""
    with tempfile.TemporaryDirectory() as td:
        rewritten = TRANSLATED_MD.replace(
            "Alpha line with 灵根.", "Completely different prose now.")
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD, "translated_md": rewritten},
            {"file": "Chapter_0002.md", "order": 1,
             "source_md": SOURCE_MD,
             "translated_md": TRANSLATED_MD.replace("灵根", "灵根B")},
        ])
        write_sidecar(root, "Chapter_0001.md", [
            {"line": 0, "term": "失锚", "note": "Orphaned note.",
             "category": "other", "anchor": "Alpha line with 灵根."},
        ])
        write_sidecar(root, "Chapter_0002.md", [
            {"line": 0, "term": "灵根B", "note": "Spirit root: innate aptitude.",
             "category": "cultural", "anchor": "Alpha line with 灵根B."},
            {"line": 1, "term": "筑基",
             "note": "Foundation Establishment is the second realm.",
             "category": "cultural", "anchor": "Beta line."},
        ])
        # a stale GLOSSARY report from an earlier run
        write_lf(root / "review-report.md",
                 "---\nreport_type: glossary-review\n---\n\n"
                 "# Glossary Review Report\n")

        rows = {"findings": [
            {"idx": 1, "kind": "restates", "severity": "warn",
             "reason": "the note paraphrases the translated line",
             "suggestion": "Spirit roots are the gate to cultivation; only one in ten children has one."},
            {"idx": 2, "kind": "overexplains", "severity": "info",
             "reason": "the realm is explained again in chapter 3",
             "suggestion": ""},
        ]}
        count = run_full_with_fake_chat(root, [rows])
        path = root / "review-report.md"
        check("6a report: written to the configured default path",
              count == 3 and path.is_file(), f"count={count} path={path}")
        text = path.read_text(encoding="utf-8")

        check("6b report: notes run REPLACES the glossary report",
              text.startswith("---\nreport_type: notes-review\n")
              and "Glossary Review Report" not in text,
              f"head={text[:80]!r}")
        check("6c report: frontmatter carries tier + counts",
              "\ntier: notes\n" in text
              and "\ngenerated_by: review notes\n" in text
              and "\nsource_lang: zh\n" in text and "\ntarget_lang: en\n" in text
              and "\nchapters_reviewed: 2\n" in text
              and "\nnotes_reviewed: 2\n" in text
              and "\nbatch_errors: 0\n" in text
              and "\noutcome:\n  warn: 2\n  info: 1\n" in text
              and "\nkinds:\n  restates: 1\n  overexplains: 1\n  wrong: 0\n"
                  "  misanchored: 1\n" in text,
              "frontmatter values wrong")
        check("6d report: findings section renders chapter/term/kind/severity "
              "(warn-first, then reading order)",
              "## Notes findings" in text
              and "### [1] warn / misanchored / Chapter_0001.md / 失锚" in text
              and "### [2] warn / restates / Chapter_0002.md / 灵根B" in text
              and "### [3] info / overexplains / Chapter_0002.md / 筑基" in text,
              "finding headings wrong")
        check("6e report: reason/suggestion/tier bullets present",
              "- Reason: the note paraphrases the translated line" in text
              and "- Suggestion: Spirit roots are the gate to cultivation" in text
              and "- Tier: model" in text and "- Tier: deterministic" in text,
              "bullets missing")
        check("6f report: NO Command bullets anywhere",
              "- Command:" not in text and "Machine-applicable" not in text,
              "command bullet leaked")
        check("6g report: footer warns about the tn re-check overwrite",
              "`tn` re-check command later regenerates" in text
              and "notes/<stem>.json" in text,
              "footer missing")

        # custom review_report_path, including a subdirectory
        cfg = review_cfg(review_report_path="reports/notes-audit.md")
        run_full_with_fake_chat(root, [], cfg=cfg)
        custom = root / "reports" / "notes-audit.md"
        check("6h report: custom review_report_path honored (subdir created)",
              custom.is_file() and not custom.with_suffix(".tmp").exists(), "")

        # a genuinely clean run (all notes resolve, model returns nothing)
        with tempfile.TemporaryDirectory() as td2:
            clean_root = make_project(td2, [
                {"file": "Chapter_0001.md", "order": 0,
                 "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
            ])
            write_sidecar(clean_root, "Chapter_0001.md",
                          [dict(NOTE_A), dict(NOTE_B)])
            count = run_full_with_fake_chat(clean_root, [])
            check("6i report: clean run -> zero findings, all-zero tallies",
                  count == 0
                  and "\noutcome:\n  warn: 0\n  info: 0\n" in clean_text(clean_root)
                  and "\nkinds:\n  restates: 0\n  overexplains: 0\n  wrong: 0\n"
                      "  misanchored: 0\n" in clean_text(clean_root)
                  and "No findings -- every reviewed note earns its place."
                      in clean_text(clean_root),
                  "clean report wrong")

        # no sidecars anywhere -> nothing written, mirroring the empty glossary
        with tempfile.TemporaryDirectory() as td2:
            bare = make_project(td2, [
                {"file": "Chapter_0001.md", "order": 0,
                 "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
            ])
            count = run_full_with_fake_chat(bare, [])
            check("6j report: no sidecars -> 0 findings, no report written",
                  count == 0 and not (bare / "review-report.md").exists(),
                  f"count={count}")


def case_7_cli_smoke() -> None:
    """review notes parses and runs end-to-end through translate.main with
    a stubbed provider; --fix and --batch-size 0 are usage errors."""
    import argparse

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0,
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        write_sidecar(root, "Chapter_0001.md", [dict(NOTE_A), dict(NOTE_B)])
        write_lf(root / "config.json",
                 json.dumps({"source_lang": "zh", "target_lang": "en"}) + "\n")
        rows = {"findings": [
            {"idx": 0, "kind": "restates", "severity": "warn",
             "reason": "note restates the line", "suggestion": ""},
        ]}

        orig = review_notes.client.chat
        review_notes.client.chat = (
            lambda pc, prompt, json_schema=None, temperature=None,
                   max_tokens=None, meta_hook=None: json.dumps(rows))
        try:
            code = translate.main(["review", "notes", "--project", str(root)])
        finally:
            review_notes.client.chat = orig
        check("7a cli: review notes via main() exits 0 with findings present",
              code == 0 and (root / "review-report.md").is_file(),
              f"code={code}")
        text = (root / "review-report.md").read_text(encoding="utf-8")
        check("7b cli: report reflects the run",
              "notes-review" in text and "- Reason: note restates the line" in text,
              "report wrong")

        # --fix is rejected for the notes tier (it is glossary-only)
        ns = argparse.Namespace(subject="notes", fix=True, chapters=None,
                                batch_size=None, glossary=None, dry_run=False,
                                exit_on_error=False)
        try:
            translate.cmd_review(ns, root)
            check("7c cli: review notes --fix rejected", False, "no CliError")
        except translate.CliError as exc:
            check("7c cli: review notes --fix rejected",
                  "--fix applies to 'review glossary' only" in str(exc),
                  f"exc={exc}")

        # --batch-size 0 is a usage error (mirrors review glossary)
        ns = argparse.Namespace(subject="notes", fix=False, chapters=None,
                                batch_size=0, glossary=None, dry_run=False,
                                exit_on_error=False)
        try:
            translate.cmd_review(ns, root)
            check("7d cli: --batch-size 0 rejected", False, "no CliError")
        except translate.CliError as exc:
            check("7d cli: --batch-size 0 rejected",
                  "--batch-size must be a positive integer" in str(exc),
                  f"exc={exc}")

        # parser level: the subject choices include "notes" and accept the flags
        parser = translate._build_parser()
        ns = parser.parse_args(["review", "notes", "--project", str(root),
                                "--chapters", "1", "--batch-size", "5"])
        check("7e cli: parser accepts 'review notes --chapters --batch-size'",
              ns.subject == "notes" and ns.chapters == "1" and ns.batch_size == 5,
              f"ns={ns}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_anchor_resolution()
    case_2_source_pairing_and_context()
    case_3_closed_vocab_validation()
    case_4_batching()
    case_5_chapter_selection()
    case_6_report()
    case_7_cli_smoke()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
