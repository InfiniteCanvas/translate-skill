"""Tests for lib/tn_recheck.py: the `tn` command's re-evaluation engine.

Covers the happy path (fake annotator -> sidecar written, tn_history.json
updated with last_order/times, sane result counts); legacy migration (a
translated chapter with baked [^N] markers + a "## Translator's Notes"
section is rewritten clean with the frontmatter byte-verbatim — a hand-
written YAML comment proves no re-serialization — baseline baked notes
counted in notes_before); eligibility (status pending -> skipped, status
translated but file missing -> skipped, zero eligible -> scanned 0, nothing
written); the cross-chapter gap rule (a term annotated at an earlier order
within tn_gap_chapters stays suppressed and history is untouched, while a
same-chapter history entry does NOT suppress); the low-threshold gate
(threshold "low" dropped with the default cfg); dry_run (LLM evaluation
included, but no sidecar, no history file, not even the legacy markdown
rewrite); annotator failure (file recorded in `failed`, pre-existing
sidecar + history byte-unchanged, and — regression — a LEGACY chapter is
left byte-unchanged too: the migration rewrite must land only after the
annotator succeeded); unreadable chapter YAML (recorded in `failed`, the
run continues); the cmd_retry wipe loop (deletes draft artifacts, the
translated file, AND the notes sidecar — regression: the sidecar unlink
once passed the paths dict where tn.notes_path expects the project dir and
crashed retry mid-wipe); and manifest-order processing (files
passed out of order are still processed by `order`, so the gap rule
suppresses the same term in the immediately following chapter).

Every case builds a full sandbox project (source/, translated/,
chapters.json, optional tn_history.json) inside tempfile.TemporaryDirectory()
— repo fixtures are never touched. The annotator LLM is a stub returning a
canned JSON string (recheck runs client.extract_json over it). Files are
written with explicit LF newlines so byte-level comparisons are
deterministic.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_tn_recheck.py
"""

import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import project, tn  # noqa: E402
from lib import tn_recheck  # noqa: E402

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
)

TRANSLATED_MD = (
    "---\n"
    "chapter_title: 第一章 灵根\n"
    "title: Spirit Root Awakening\n"
    "---\n"
    "\n"
    "Line zero body.\n"
    "Line one body.\n"
)

# Hand-written frontmatter (comment included) so byte-verbatim survival of
# the migration rewrite is observable: yaml parsing would drop the comment.
LEGACY_HEAD = (
    "---\n"
    "# a hand-written comment that yaml parsers drop\n"
    "chapter_title: 第一章 灵根\n"
    "order: 0\n"
    "---\n"
)
LEGACY_MD = (
    LEGACY_HEAD
    + "\n"
    + "Legacy line zero.[^1]\n"
    + "Legacy line one.\n"
    + "\n"
    + "## Translator's Notes\n"
    + "\n"
    + "[^1]: **灵根** — innate aptitude for cultivation.\n"
)


def make_project(td: str, chapters: list[dict]) -> tuple[Path, list[dict]]:
    """Sandbox project: source/, translated/, chapters.json. Each chapter
    dict: {file, order, status, source_md, translated_md} (texts optional --
    omit one to simulate a missing file)."""
    root = Path(td)
    manifest = []
    for ch in chapters:
        number = int(Path(ch["file"]).stem.split("_")[1])
        manifest.append({
            "file": ch["file"], "number": number, "suffix": "",
            "order": ch["order"], "status": ch["status"],
            "title": ch.get("title", "Title"),
        })
        if ch.get("source_md") is not None:
            write_lf(root / "source" / ch["file"], ch["source_md"])
        if ch.get("translated_md") is not None:
            write_lf(root / "translated" / ch["file"], ch["translated_md"])
    write_lf(root / "chapters.json",
             json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return root, manifest


def fake_chat_factory(payload: dict, sink: list | None = None):
    """Stub annotator: returns the canned payload as a JSON string (the
    engine runs client.extract_json over it); optionally records prompts."""
    def chat(prompt: str) -> str:
        if sink is not None:
            sink.append(prompt)
        return json.dumps(payload, ensure_ascii=False)
    return chat


def run(root: Path, manifest: list[dict], files: list[str], chat, **kw) -> dict:
    return tn_recheck.recheck_chapters(
        root, manifest, files, cfg={}, chat=chat, **kw
    )


# ---------------------------------------------------------------------- cases


def case_1_happy_path() -> None:
    """Fresh annotation: sidecar written, history threaded, counts sane."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        chat = fake_chat_factory(
            {"notes": [{"line": 1, "term": "清明",
                        "note": "Tomb-sweeping festival."}]})
        result = run(root, manifest, ["Chapter_0001.md"], chat)

        check("1a happy: full result dict",
              result == {"scanned": 1, "changed": 1, "migrated": 0,
                         "notes_before": 0, "notes_after": 1,
                         "failed": [], "skipped": [], "dry_run": False},
              f"result={result}")

        path = tn.notes_path(root, "Chapter_0001.md")
        check("1b happy: sidecar written for the chapter", path.is_file(), "")
        notes = tn.load_notes(root, "Chapter_0001.md")
        check("1c happy: sidecar holds the annotated note, normalized",
              len(notes) == 1 and notes[0]["term"] == "清明"
              and notes[0]["note"] == "Tomb-sweeping festival."
              and set(notes[0]) == {"line", "term", "note", "anchor"},
              f"notes={notes}")
        check("1d happy: anchor snapshotted from the annotated line",
              notes[0]["line"] == 1 and notes[0]["anchor"] == "Line one body.",
              f"note={notes[0] if notes else None}")

        history = json.loads((root / "tn_history.json").read_text(encoding="utf-8"))
        check("1e happy: history records the term at the chapter order",
              history.get("清明", {}).get("last_order") == 0
              and history.get("清明", {}).get("times") == 1,
              f"history={history}")
        check("1f happy: translated markdown untouched (already clean)",
              (root / "translated" / "Chapter_0001.md").read_text(encoding="utf-8")
              == TRANSLATED_MD, "")


def case_2_legacy_migration() -> None:
    """Baked-in notes migrate: clean markdown, byte-verbatim frontmatter."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": LEGACY_MD},
        ])
        chat = fake_chat_factory(
            {"notes": [{"line": 1, "term": "清明",
                        "note": "Tomb-sweeping festival."}]})
        result = run(root, manifest, ["Chapter_0001.md"], chat)

        check("2a migration: counts (migrated 1, baseline in notes_before)",
              result["scanned"] == 1 and result["migrated"] == 1
              and result["changed"] == 1
              and result["notes_before"] == 1 and result["notes_after"] == 1,
              f"result={result}")

        text = (root / "translated" / "Chapter_0001.md").read_text(encoding="utf-8")
        check("2b migration: frontmatter byte-verbatim (comment survives)",
              text.startswith(LEGACY_HEAD), f"head={text[:100]!r}")
        check("2c migration: no footnote markers left",
              "[^" not in text, f"text={text!r}")
        check("2d migration: no Translator's Notes section left",
              "## Translator's Notes" not in text, "")
        check("2e migration: body preserved, one trailing newline",
              text.endswith("Legacy line one.\n")
              and "Legacy line zero." in text, f"text={text!r}")
        check("2f migration: sidecar written for the fresh note",
              len(tn.load_notes(root, "Chapter_0001.md")) == 1, "")


def case_3_eligibility() -> None:
    """Not-translated and missing-file chapters skip; zero eligible scans 0."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            # pending: files exist but status is not translated
            {"file": "Chapter_0001.md", "order": 0, "status": "pending",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
            # translated but the translated file is missing
            {"file": "Chapter_0002.md", "order": 1, "status": "translated",
             "source_md": SOURCE_MD},
        ])
        chat = fake_chat_factory({"notes": []})
        result = run(root, manifest, ["Chapter_0001.md", "Chapter_0002.md"], chat)

        check("3a eligibility: both skipped, none scanned",
              result["scanned"] == 0
              and result["skipped"] == ["Chapter_0001.md", "Chapter_0002.md"]
              and result["failed"] == [],
              f"result={result}")
        check("3b eligibility: no sidecar written",
              not (root / "notes").exists()
              or not (root / "notes" / "Chapter_0001.json").exists(), "")
        check("3c eligibility: no history file written",
              not (root / "tn_history.json").exists(), "")


def case_4_gap_rule() -> None:
    """Cross-chapter suppression via tn_history.json; same-chapter never."""
    payload = {"notes": [{"line": 1, "term": "清明",
                          "note": "Tomb-sweeping festival."}]}

    # A: annotated at an earlier order within tn_gap_chapters (default 10)
    #    -> suppressed, history untouched
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0006.md", "order": 5, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        seeded = {"清明": {"note": "old note", "last_order": 0, "times": 1}}
        write_lf(root / "tn_history.json",
                 json.dumps(seeded, ensure_ascii=False, indent=2) + "\n")
        result = run(root, manifest, ["Chapter_0006.md"],
                     fake_chat_factory(payload))

        check("4a gap: term within gap suppressed (no sidecar, 0 after)",
              result["notes_after"] == 0 and result["scanned"] == 1
              and not tn.notes_path(root, "Chapter_0006.md").exists(),
              f"result={result}")
        history = json.loads((root / "tn_history.json").read_text(encoding="utf-8"))
        check("4b gap: history left unchanged by the suppression",
              history.get("清明", {}).get("last_order") == 0
              and history.get("清明", {}).get("times") == 1,
              f"history={history}")

    # B: a history entry from THIS chapter (same order) must NOT suppress
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0006.md", "order": 5, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        seeded = {"清明": {"note": "old note", "last_order": 5, "times": 1}}
        write_lf(root / "tn_history.json",
                 json.dumps(seeded, ensure_ascii=False, indent=2) + "\n")
        result = run(root, manifest, ["Chapter_0006.md"],
                     fake_chat_factory(payload))

        check("4c gap: same-chapter history entry does not suppress",
              result["notes_after"] == 1
              and tn.notes_path(root, "Chapter_0006.md").is_file(),
              f"result={result}")
        history = json.loads((root / "tn_history.json").read_text(encoding="utf-8"))
        check("4d gap: history re-recorded at the same order, times bumped",
              history.get("清明", {}).get("last_order") == 5
              and history.get("清明", {}).get("times") == 2,
              f"history={history}")


def case_5_low_threshold() -> None:
    """threshold:'low' notes are dropped with the default cfg ({})."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        chat = fake_chat_factory(
            {"notes": [{"line": 1, "term": "清明",
                        "note": "Tomb-sweeping festival.",
                        "threshold": "low"}]})
        result = run(root, manifest, ["Chapter_0001.md"], chat)

        check("5a low: note dropped, no sidecar, zero after",
              result["notes_after"] == 0 and result["scanned"] == 1
              and not tn.notes_path(root, "Chapter_0001.md").exists(),
              f"result={result}")


def case_6_dry_run() -> None:
    """dry_run evaluates fully (LLM included) but writes NOTHING."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": LEGACY_MD},
        ])
        chat = fake_chat_factory(
            {"notes": [{"line": 1, "term": "清明",
                        "note": "Tomb-sweeping festival."}]})
        result = run(root, manifest, ["Chapter_0001.md"], chat, dry_run=True)

        check("6a dry-run: result flags dry_run, counts still reported",
              result["dry_run"] is True and result["scanned"] == 1
              and result["migrated"] == 1
              and result["notes_before"] == 1 and result["notes_after"] == 1,
              f"result={result}")
        check("6b dry-run: legacy markdown byte-unchanged (no migration write)",
              (root / "translated" / "Chapter_0001.md").read_text(encoding="utf-8")
              == LEGACY_MD, "")
        check("6c dry-run: no sidecar written",
              not tn.notes_path(root, "Chapter_0001.md").exists(), "")
        check("6d dry-run: no history file written",
              not (root / "tn_history.json").exists(), "")


def case_7_chat_failure() -> None:
    """Annotator exception: chapter in `failed`, existing state untouched."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        tn.save_notes(
            root, "Chapter_0001.md", ["Line zero body.", "Line one body."],
            [{"line": 0, "term": "旧词", "note": "Old note."}],
        )
        sidecar_bytes = tn.notes_path(root, "Chapter_0001.md").read_bytes()
        seeded = {"旧词": {"note": "Old note.", "last_order": 0, "times": 1}}
        write_lf(root / "tn_history.json",
                 json.dumps(seeded, ensure_ascii=False, indent=2) + "\n")
        history_bytes = (root / "tn_history.json").read_bytes()
        chapter_bytes = (root / "translated" / "Chapter_0001.md").read_bytes()

        def broken_chat(prompt: str) -> str:
            raise RuntimeError("endpoint down")

        result = run(root, manifest, ["Chapter_0001.md"], broken_chat)

        check("7a failure: file recorded in failed, nothing re-annotated",
              result["failed"] == ["Chapter_0001.md"]
              and result["notes_after"] == 0 and result["scanned"] == 1,
              f"result={result}")
        check("7b failure: pre-existing sidecar byte-unchanged",
              tn.notes_path(root, "Chapter_0001.md").read_bytes()
              == sidecar_bytes, "")
        check("7c failure: history byte-unchanged",
              (root / "tn_history.json").read_bytes() == history_bytes, "")
        check("7d failure: chapter markdown byte-unchanged",
              (root / "translated" / "Chapter_0001.md").read_bytes()
              == chapter_bytes, "")


def case_8_manifest_order() -> None:
    """Files passed out of order still process by manifest order, so the
    gap rule suppresses the same term in the immediately following chapter."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD,
             "translated_md": TRANSLATED_MD.replace(
                 "Line zero body.", "Alpha line zero.").replace(
                 "Line one body.", "Alpha line one.")},
            {"file": "Chapter_0002.md", "order": 1, "status": "translated",
             "source_md": SOURCE_MD,
             "translated_md": TRANSLATED_MD.replace(
                 "Line zero body.", "Beta line zero.").replace(
                 "Line one body.", "Beta line one.")},
        ])
        prompts: list[str] = []
        chat = fake_chat_factory(
            {"notes": [{"line": 1, "term": "清明",
                        "note": "Tomb-sweeping festival."}]}, sink=prompts)
        # deliberately out of manifest order
        result = run(root, manifest, ["Chapter_0002.md", "Chapter_0001.md"], chat)

        check("8a order: both chapters processed (2 LLM calls)",
              result["scanned"] == 2 and len(prompts) == 2,
              f"result={result}, calls={len(prompts)}")
        check("8b order: first prompt is chapter 1 (manifest order wins)",
              "Alpha line one." in prompts[0] and "Beta line one." not in prompts[0],
              f"prompt0={prompts[0][:120]!r}")
        check("8c order: second prompt is chapter 2",
              "Beta line one." in prompts[1], "")
        check("8d order: same term kept in ch1, gap-suppressed in ch2",
              tn.notes_path(root, "Chapter_0001.md").is_file()
              and not tn.notes_path(root, "Chapter_0002.md").exists()
              and result["notes_after"] == 1,
              f"result={result}")
        history = json.loads((root / "tn_history.json").read_text(encoding="utf-8"))
        check("8e order: history recorded once, at chapter 1's order",
              history.get("清明", {}).get("last_order") == 0
              and history.get("清明", {}).get("times") == 1,
              f"history={history}")


def case_9_legacy_failure_no_migration() -> None:
    """Regression: annotator failure on a LEGACY chapter must leave the
    chapter byte-unchanged -- the baked notes are the only copy on disk, so
    stripping them before the annotator succeeded would destroy them."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": LEGACY_MD},
        ])
        chapter_bytes = (root / "translated" / "Chapter_0001.md").read_bytes()

        def broken_chat(prompt: str) -> str:
            raise RuntimeError("endpoint down")

        result = run(root, manifest, ["Chapter_0001.md"], broken_chat)

        check("9a legacy failure: recorded in failed, no migration counted",
              result["failed"] == ["Chapter_0001.md"]
              and result["migrated"] == 0 and result["changed"] == 0,
              f"result={result}")
        check("9b legacy failure: chapter markdown byte-unchanged",
              (root / "translated" / "Chapter_0001.md").read_bytes()
              == chapter_bytes, "")
        check("9c legacy failure: no sidecar, no history written",
              not tn.notes_path(root, "Chapter_0001.md").exists()
              and not (root / "tn_history.json").exists(), "")


def case_10_bad_yaml_continues() -> None:
    """Regression: one chapter with unreadable YAML frontmatter must not
    crash the run -- it lands in `failed` and the rest of the range still
    processes."""
    bad_md = "---\nchapter_title: [unclosed\n---\n\nLine zero body.\n"
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": bad_md},
            {"file": "Chapter_0002.md", "order": 1, "status": "translated",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        chat = fake_chat_factory(
            {"notes": [{"line": 1, "term": "清明",
                        "note": "Tomb-sweeping festival."}]})
        result = run(root, manifest, ["Chapter_0001.md", "Chapter_0002.md"], chat)

        check("10a bad yaml: broken chapter in failed, good one processed",
              result["failed"] == ["Chapter_0001.md"]
              and result["skipped"] == [] and result["scanned"] == 2
              and result["notes_after"] == 1,
              f"result={result}")
        check("10b bad yaml: good chapter's sidecar written",
              tn.notes_path(root, "Chapter_0002.md").is_file(), "")
        check("10c bad yaml: broken chapter left on disk untouched",
              (root / "translated" / "Chapter_0001.md").read_text(
                  encoding="utf-8") == bad_md, "")


def case_11_retry_wipe_sidecar() -> None:
    """Regression: cmd_retry's wipe loop must unlink the notes sidecar (and
    not crash -- it once called tn.notes_path with the paths DICT where the
    project dir belongs, raising TypeError after the chapter was half-wiped).
    pipeline.run_range is stubbed so no LLM call happens."""
    import argparse

    import translate

    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, [
            {"file": "Chapter_0001.md", "order": 0, "status": "needs-review",
             "source_md": SOURCE_MD, "translated_md": TRANSLATED_MD},
        ])
        write_lf(root / "config.json", '{"providers": {}}\n')
        write_lf(root / "glossary.json", '{"terms": [], "retired": []}\n')
        write_lf(root / "draft" / "Chapter_0001.state.json", "{}\n")
        tn.save_notes(
            root, "Chapter_0001.md", ["Line zero body.", "Line one body."],
            [{"line": 0, "term": "旧词", "note": "Old note."}],
        )

        real_run_range = translate.pipeline.run_range
        translate.pipeline.run_range = (
            lambda *a, **k: {"translated": [], "needs-review": [],
                             "skipped": []})
        try:
            code = translate.cmd_retry(
                argparse.Namespace(failed=False, chapters="Chapter_0001.md"),
                root,
            )
        finally:
            translate.pipeline.run_range = real_run_range

        check("11a retry wipe: exits 0 without crashing", code == 0,
              f"code={code}")
        check("11b retry wipe: translated file removed",
              not (root / "translated" / "Chapter_0001.md").exists(), "")
        check("11c retry wipe: draft state removed",
              not (root / "draft" / "Chapter_0001.state.json").exists(), "")
        check("11d retry wipe: notes sidecar removed",
              not tn.notes_path(root, "Chapter_0001.md").exists(), "")
        saved = json.loads((root / "chapters.json").read_text(encoding="utf-8"))
        check("11e retry wipe: manifest status reset to pending",
              saved[0]["status"] == "pending", f"status={saved[0].get('status')}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_happy_path()
    case_2_legacy_migration()
    case_3_eligibility()
    case_4_gap_rule()
    case_5_low_threshold()
    case_6_dry_run()
    case_7_chat_failure()
    case_8_manifest_order()
    case_9_legacy_failure_no_migration()
    case_10_bad_yaml_continues()
    case_11_retry_wipe_sidecar()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
