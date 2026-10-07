"""Tests for the TN annotator upgrade: note categories, the code-enforced
max_notes cap, and the dropped-candidates artifact.

Covers tn.NOTE_CATEGORIES handling in tn.process (a missing, unknown, or
non-string category silently defaults to "other"; a valid one passes
through onto kept entries); the cap (12 valid notes at max_notes=10 -> 10
kept in order + 2 dropped with reason "overflow" carrying
category/threshold; max_notes omitted -> no cap, direct callers keep the
old behavior); the cap's interaction with the gap rule (a gap-suppressed
note never consumes a cap slot: 2 fresh + 4 gap-suppressed + a
within-chapter duplicate at max_notes=2 -> both fresh kept, nothing
dropped, duplicates and gap drops NOT recorded in `dropped`); the
low-threshold gate recording reason "low_threshold" (and keeping the notes
-- unrecorded -- when keep_low=True); invalid entries recorded with reason
"invalid" while keeping their warnings; tn.save_dropped's lifecycle
(writes notes/<stem>.dropped.json when non-empty with document shape
{chapter, updated_at, dropped} + trailing newline, DELETES it when empty);
save_notes emitting the normalized "category" field in sidecar entries;
and pipeline.NOTES_SCHEMA carrying "category" as an OPTIONAL property with
the NOTE_CATEGORIES enum (not required -- models may omit it); and the
cap's history rollback (a truncated term's key is absent from the
returned history when it had no pre-call entry and restored to its
pre-call value when it did -- a same-chapter retranslation entry is
restored, not deleted -- so a later chapter inside the gap window can
still annotate the term, and the input history dict is never mutated).

All chapter fixtures are built inside tempfile.TemporaryDirectory()
sandboxes per case — repo fixtures are never touched.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_tn_categories.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0"]
# ///
import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import pipeline, tn  # noqa: E402

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


def note(term: str, category=None, threshold=None, line=0) -> dict:
    """A valid candidate note; category/threshold only when given."""
    entry = {"line": line, "term": term, "note": f"About {term}."}
    if category is not None:
        entry["category"] = category
    if threshold is not None:
        entry["threshold"] = threshold
    return entry


# ---------------------------------------------------------------------- cases


def case_1_category_defaulting() -> None:
    """process(): missing/unknown/non-string category -> "other" on kept
    entries; a valid category passes through."""
    notes = [
        note("甲"),                     # no category key
        note("乙", category="mystery"),  # not in NOTE_CATEGORIES
        note("丙", category=5),          # not a string at all
        note("丁", category="wordplay"),  # valid
    ]
    kept, _history, warnings, dropped = tn.process(notes, 3, 0, {}, 10)
    check("1a category: all four kept, no warnings/drops",
          len(kept) == 4 and warnings == [] and dropped == [],
          f"kept={len(kept)}, warnings={warnings}, dropped={dropped}")
    check("1b category: every kept entry carries the field",
          all("category" in entry for entry in kept), f"kept={kept}")
    check("1c category: missing/unknown/non-string default to 'other'",
          [entry["category"] for entry in kept]
          == ["other", "other", "other", "wordplay"],
          f"categories={[e.get('category') for e in kept]}")


def case_2_cap_overflow() -> None:
    """12 valid notes at max_notes=10: 10 kept in order, 2 dropped as
    overflow carrying category/threshold; no cap when max_notes omitted."""
    notes = [note(f"词{i}", category=("idiom" if i % 2 else "cultural"),
                  threshold="high", line=0) for i in range(12)]
    kept, _history, warnings, dropped = tn.process(
        notes, 2, 0, {}, 10, max_notes=10,
    )
    check("2a cap: exactly 10 kept (first 10, in order)",
          len(kept) == 10 and [e["term"] for e in kept] == [f"词{i}" for i in range(10)],
          f"kept={[e.get('term') for e in kept]}")
    check("2b cap: 2 dropped, both reason 'overflow', no warnings",
          len(dropped) == 2 and all(d["reason"] == "overflow" for d in dropped)
          and warnings == [],
          f"dropped={dropped}")
    check("2c cap: overflowed entries carry term/category/threshold",
          [d["term"] for d in dropped] == ["词10", "词11"]
          and [d["category"] for d in dropped] == ["cultural", "idiom"]
          and all(d["threshold"] == "high" for d in dropped),
          f"dropped={dropped}")

    kept_all, _h, _w, dropped_all = tn.process(notes, 2, 0, {}, 10)
    check("2d cap: max_notes omitted (None) keeps all 12, nothing dropped",
          len(kept_all) == 12 and dropped_all == [],
          f"kept={len(kept_all)}, dropped={dropped_all}")


def case_3_gap_rule_and_cap() -> None:
    """A gap-suppressed note must not waste a cap slot; duplicates and gap
    drops are never recorded in `dropped`."""
    # 4 terms already annotated one chapter ago (inside the default gap)
    history = {
        f"旧{i}": {"note": "old", "last_order": 4, "times": 1} for i in range(4)
    }
    notes = [note(f"旧{i}") for i in range(4)]          # gap-suppressed
    notes += [note("新甲"), note("新乙")]                # fresh
    notes.append(note("新甲"))                          # within-chapter duplicate
    kept, updated, warnings, dropped = tn.process(
        notes, 3, 5, history, 10, max_notes=2,
    )
    check("3a gap+cap: both fresh notes kept despite 7 candidates",
          [e["term"] for e in kept] == ["新甲", "新乙"],
          f"kept={[e.get('term') for e in kept]}")
    check("3b gap+cap: nothing dropped (no overflow, gap/dup not recorded)",
          dropped == [], f"dropped={dropped}")
    check("3c gap+cap: the duplicate still warns",
          any("duplicate" in w for w in warnings), f"warnings={warnings}")
    check("3d gap+cap: history untouched by suppressions",
          all(updated[f"旧{i}"]["times"] == 1 for i in range(4))
          and updated["新甲"]["times"] == 1 and updated["新乙"]["times"] == 1,
          f"history={updated}")


def case_4_low_threshold() -> None:
    """threshold:'low' candidates land in dropped (reason 'low_threshold');
    keep_low=True keeps them and records nothing."""
    notes = [
        note("低一", threshold="low", category="cultural"),
        note("高二", threshold="high"),
        note("无三"),  # no threshold key
    ]
    kept, _history, warnings, dropped = tn.process(notes, 3, 0, {}, 10)
    check("4a low: default gate keeps only the non-low notes",
          [e["term"] for e in kept] == ["高二", "无三"],
          f"kept={[e.get('term') for e in kept]}")
    check("4b low: dropped records the entry with reason 'low_threshold'",
          len(dropped) == 1 and dropped[0]["reason"] == "low_threshold"
          and dropped[0]["term"] == "低一" and dropped[0]["category"] == "cultural"
          and dropped[0]["threshold"] == "low",
          f"dropped={dropped}")
    check("4c low: the gate stays silent (no warnings)",
          warnings == [], f"warnings={warnings}")

    kept, _history, warnings, dropped = tn.process(notes, 3, 0, {}, 10,
                                                   keep_low=True)
    check("4d low: keep_low=True keeps every note, records nothing",
          len(kept) == 3 and dropped == [] and warnings == [],
          f"kept={len(kept)}, dropped={dropped}")


def case_5_invalid_recorded() -> None:
    """Invalid entries keep their warnings AND land in dropped with reason
    'invalid' (best-effort fields)."""
    notes = [
        "not a dict",
        {"line": 99, "term": "越界", "note": "out of range"},
        {"line": 0, "term": "", "note": "empty term"},
        {"line": 0, "term": 7, "note": "non-string term"},
        note("好词"),
    ]
    kept, _history, warnings, dropped = tn.process(notes, 3, 0, {}, 10)
    check("5a invalid: only the valid note kept",
          [e["term"] for e in kept] == ["好词"], f"kept={kept}")
    check("5b invalid: one warning per invalid entry",
          len(warnings) == 4, f"warnings={warnings}")
    check("5c invalid: every invalid entry recorded with reason 'invalid'",
          len(dropped) == 4 and all(d["reason"] == "invalid" for d in dropped),
          f"dropped={dropped}")
    check("5d invalid: recorded fields are best-effort (non-dict -> None)",
          {"line": None, "term": None, "note": None, "category": None,
           "threshold": None, "reason": "invalid"} in dropped,
          f"dropped={dropped}")


def case_6_save_dropped_lifecycle() -> None:
    """save_dropped writes notes/<stem>.dropped.json when non-empty and
    DELETES it when empty (absent = nothing dropped)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = tn.dropped_path(root, "CHAPTER_0001.md")
        check("6a dropped-path: notes/<stem>.dropped.json",
              path == root / "notes" / "CHAPTER_0001.dropped.json",
              f"path={path}")

        dropped = [{"line": 0, "term": "甲", "note": "n", "category": "other",
                    "threshold": "low", "reason": "low_threshold"}]
        tn.save_dropped(root, "CHAPTER_0001.md", dropped)
        check("6b save_dropped: file written when non-empty", path.is_file(), "")
        raw = path.read_bytes()
        check("6c save_dropped: trailing newline", raw.endswith(b"\n"),
              f"tail={raw[-10:]!r}")
        document = json.loads(raw.decode("utf-8"))
        check("6d save_dropped: document shape {chapter, updated_at, dropped}",
              set(document) == {"chapter", "updated_at", "dropped"}
              and document["chapter"] == "CHAPTER_0001.md"
              and isinstance(document["updated_at"], str)
              and document["dropped"] == dropped,
              f"document={document}")

        tn.save_dropped(root, "CHAPTER_0001.md", [])
        check("6e save_dropped: empty list DELETES the file",
              not path.exists(), f"path={path}")
        tn.save_dropped(root, "CHAPTER_0001.md", [])  # absent stays absent
        check("6f save_dropped: deleting an absent file is a no-op",
              not path.exists(), "")


def case_7_sidecar_category() -> None:
    """save_notes emits the normalized 'category' in sidecar entries."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lines = ["Line zero.", "Line one."]
        kept = tn.save_notes(root, "CHAPTER_0002.md", lines, [
            {"line": 0, "term": "甲", "note": "No category."},
            {"line": 1, "term": "乙", "note": "Categorized.", "category": "unit"},
            {"line": 1, "term": "丙", "note": "Bogus.", "category": "dragon"},
        ])
        check("7a sidecar: all three kept with a category field",
              len(kept) == 3
              and [e["category"] for e in kept] == ["other", "unit", "other"],
              f"kept={kept}")
        document = json.loads(
            (root / "notes" / "CHAPTER_0002.json").read_text(encoding="utf-8"))
        check("7b sidecar: on-disk entries carry the category",
              [e["category"] for e in document["notes"]]
              == ["other", "unit", "other"],
              f"document={document['notes']}")


def case_8_notes_schema() -> None:
    """pipeline.NOTES_SCHEMA: 'category' is an optional property with the
    NOTE_CATEGORIES enum (not required -- tn.process defaults to 'other')."""
    items = pipeline.NOTES_SCHEMA["properties"]["notes"]["items"]
    props = items["properties"]
    check("8a schema: 'category' property present with the enum",
          props.get("category")
          == {"type": "string", "enum": list(tn.NOTE_CATEGORIES)},
          f"category={props.get('category')}")
    check("8b schema: 'category' NOT required (models may omit it)",
          "category" not in items["required"], f"required={items['required']}")


def case_9_overflow_history_rollback() -> None:
    """The cap rolls truncated terms' history entries back: the reader never
    saw the note, so it must not consume the gap window."""
    # No pre-call entry: the truncated key must not survive this call.
    notes = [note("甲"), note("乙"), note("丙")]
    kept, updated, _warnings, dropped = tn.process(notes, 3, 5, {}, 10,
                                                   max_notes=2)
    check("9a rollback: truncated key with no pre-call entry absent from history",
          [e["term"] for e in kept] == ["甲", "乙"]
          and [d["term"] for d in dropped] == ["丙"]
          and set(updated) == {"甲", "乙"},
          f"updated={updated}")

    # Pre-call entry from beyond the gap: the truncated third note (旧词)
    # would set last_order=5/times=2; it must roll back to the input entry.
    history = {"旧词": {"note": "original", "last_order": 0, "times": 1}}
    snapshot = {"旧词": dict(history["旧词"])}
    notes = [note("新甲"), note("新乙"), note("旧词")]
    kept, updated, _warnings, dropped = tn.process(notes, 3, 5, history, 2,
                                                   max_notes=2)
    check("9b rollback: truncated key restored to its pre-call entry",
          [e["term"] for e in kept] == ["新甲", "新乙"]
          and [d["term"] for d in dropped] == ["旧词"]
          and updated["旧词"] == {"note": "original", "last_order": 0,
                                 "times": 1},
          f"updated={updated}")
    check("9c rollback: input history never mutated",
          history == snapshot, f"history={history}")
    # With the rollback, chapter 6 is 6 chapters past the restored
    # last_order=0 (> gap 2), so the term can still be annotated; without
    # it (last_order=5) the gap rule would suppress the note.
    later_kept, _h, _w, _d = tn.process([note("旧词")], 3, 6, updated, 2)
    check("9d rollback: a later chapter inside the old window can annotate",
          [e["term"] for e in later_kept] == ["旧词"],
          f"kept={[e.get('term') for e in later_kept]}")

    # Same-chapter retranslation edge: the input entry's last_order equals
    # this chapter's order; the truncated note restores THAT entry rather
    # than deleting the key (or leaving its would-be update behind).
    history = {"重译": {"note": "first attempt", "last_order": 5, "times": 2}}
    notes = [note("前甲"), note("重译")]
    kept, updated, _warnings, dropped = tn.process(notes, 2, 5, history, 10,
                                                   max_notes=1)
    check("9e rollback: same-chapter retranslation entry restored, not deleted",
          [e["term"] for e in kept] == ["前甲"]
          and [d["term"] for d in dropped] == ["重译"]
          and updated["重译"] == {"note": "first attempt", "last_order": 5,
                                 "times": 2},
          f"updated={updated}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_category_defaulting()
    case_2_cap_overflow()
    case_3_gap_rule_and_cap()
    case_4_low_threshold()
    case_5_invalid_recorded()
    case_6_save_dropped_lifecycle()
    case_7_sidecar_category()
    case_8_notes_schema()
    case_9_overflow_history_rollback()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
