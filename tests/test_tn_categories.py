# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0"]
# ///
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import pipeline, tn

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


def case_1_category_defaulting() -> None:
    """process(): missing/unknown/non-string category -> "other" on kept
    entries; a valid category passes through."""
    notes = [
        note("甲"),
        note("乙", category="mystery"),
        note("丙", category=5),
        note("丁", category="wordplay"),
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
    history = {
        f"旧{i}": {"note": "old", "last_order": 4, "times": 1} for i in range(4)
    }
    notes = [note(f"旧{i}") for i in range(4)]
    notes += [note("新甲"), note("新乙")]
    notes.append(note("新甲"))
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
        note("无三"),
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
        tn.save_dropped(root, "CHAPTER_0001.md", [])
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
    notes = [note("甲"), note("乙"), note("丙")]
    kept, updated, _warnings, dropped = tn.process(notes, 3, 5, {}, 10,
                                                   max_notes=2)
    check("9a rollback: truncated key with no pre-call entry absent from history",
          [e["term"] for e in kept] == ["甲", "乙"]
          and [d["term"] for d in dropped] == ["丙"]
          and set(updated) == {"甲", "乙"},
          f"updated={updated}")

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
    later_kept, _h, _w, _d = tn.process([note("旧词")], 3, 6, updated, 2)
    check("9d rollback: a later chapter inside the old window can annotate",
          [e["term"] for e in later_kept] == ["旧词"],
          f"kept={[e.get('term') for e in later_kept]}")

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


def case_10_reading_order() -> None:
    """Kept notes come back in READING order (ascending `line`), never in the
    severity order the annotator returned -- the epub numbers footnotes off
    this list, so a line-4 note ranked first must not print as [1].

    Also pins the two properties that make the sort safe: it happens AFTER the
    cap (so severity still decides which notes SURVIVE), and ties on one line
    keep the annotator's relative order (the sort is stable)."""
    notes = [
        note("首注", line=4),
        note("次注", line=1),
        note("末注", line=9),
    ]
    kept, _history, warnings, dropped = tn.process(notes, 12, 0, {}, 10)
    check("10a order: survivors sorted by line, not by model rank",
          [e["term"] for e in kept] == ["次注", "首注", "末注"],
          f"kept={[e.get('term') for e in kept]}")
    check("10b order: reordering is silent and drops nothing",
          warnings == [] and dropped == [], f"warnings={warnings}, dropped={dropped}")

    many = [note(f"词{i}", line=11 - i) for i in range(12)]
    capped, _h, _w, over = tn.process(many, 12, 0, {}, 10, max_notes=10)
    check("10c order: cap keeps the annotator's first 10 (severity), not the earliest lines",
          [d["term"] for d in over] == ["词10", "词11"], f"dropped={[d.get('term') for d in over]}")
    check("10d order: the cap survivors are then sorted by line",
          [e["term"] for e in capped] == ["词9", "词8", "词7", "词6", "词5",
                                          "词4", "词3", "词2", "词1", "词0"],
          f"kept={[e.get('term') for e in capped]}")

    tied = [note("甲", line=2), note("乙", line=2), note("丙", line=0)]
    tied_kept, _h2, _w2, _d2 = tn.process(tied, 5, 0, {}, 10)
    check("10e order: notes sharing a line keep the annotator's relative order",
          [e["term"] for e in tied_kept] == ["丙", "甲", "乙"],
          f"kept={[e.get('term') for e in tied_kept]}")


def case_11_load_sorts_reading_order() -> None:
    """load_notes re-sorts a sidecar written in the old severity order, so an
    epub built today numbers its footnotes in reading order without waiting
    for a re-translate. Unusable entries (no usable `line`) park at the end
    instead of raising -- the epub builder drops those with its own [warn]."""
    with tempfile.TemporaryDirectory() as td:
        project_dir = Path(td)
        (project_dir / "notes").mkdir()
        document = {
            "chapter": "CHAPTER_0001.md",
            "updated_at": "2026-10-01T00:00:00+00:00",
            "notes": [
                {"line": 7, "term": "晚", "note": "late", "category": "other", "anchor": "x"},
                {"line": 2, "term": "早", "note": "early", "category": "other", "anchor": "y"},
                {"line": 5, "term": "中", "note": "mid", "category": "other", "anchor": "z"},
                {"term": "无线", "note": "no line at all", "category": "other", "anchor": ""},
            ],
        }
        (project_dir / "notes" / "CHAPTER_0001.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8")

        loaded = tn.load_notes(project_dir, "CHAPTER_0001.md")
        check("11a load: an old severity-ordered sidecar comes back line-ordered",
              [n["term"] for n in loaded] == ["早", "中", "晚", "无线"],
              f"loaded={[n.get('term') for n in loaded]}")
        check("11b load: no content is dropped by the sort",
              len(loaded) == 4, f"loaded={len(loaded)}")
        check("11c load: sorting does not rewrite the file on disk",
              json.loads((project_dir / "notes" / "CHAPTER_0001.json").read_text(encoding="utf-8"))
              ["notes"][0]["term"] == "晚")


def main() -> int:
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
    case_10_reading_order()
    case_11_load_sorts_reading_order()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
