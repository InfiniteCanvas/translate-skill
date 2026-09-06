"""Tests for glossary.retire(): canonical-source recording and seed honoring.

retire() must record the ENTRY's canonical source in glossary.json's
"retired" list even when the match came via a variant (retiring 靈根 retires
灵根), so seed() can never re-add the term under its canonical spelling
while it still appears in the catalogue and the source corpus. The return
list echoes the caller-supplied lookup key (what the user typed), sources
with no matching entry are silently ignored (nothing written), and repeated
retires of the same canonical source never duplicate the "retired" entry.

All project fixtures are built inside tempfile.TemporaryDirectory()
sandboxes per case -- repo fixtures are never touched.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_glossary_retire.py
"""

import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary  # noqa: E402

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


LINGEN_ENTRY = {
    "source": "灵根", "variants": ["靈根"],
    "translation": "spirit root", "alt_translations": [],
    "definition": "Innate aptitude for cultivation.", "category": "level",
    "origin": "seeded", "first_seen_chapter": None,
}

LINGSHI_ENTRY = {
    "source": "灵石", "variants": [],
    "translation": "spirit stone", "alt_translations": [],
    "definition": "Currency of the cultivation world.", "category": "item",
    "origin": "seeded", "first_seen_chapter": None,
}


def make_project(td: str, entries: list | None = None,
                 retired: list | None = None) -> Path:
    """Temp project with a glossary.json written via glossary.save (the same
    atomic writer retire() itself uses)."""
    root = Path(td)
    g = {"terms": [dict(e) for e in entries]} if entries is not None \
        else {"terms": [dict(LINGEN_ENTRY)]}
    if retired is not None:
        g["retired"] = retired
    glossary.save(root, g)
    return root


def on_disk(root: Path) -> dict:
    return json.loads((root / "glossary.json").read_text(encoding="utf-8"))


def make_seeded_corpus_project(td: str) -> Path:
    """Project whose source corpus contains 灵根 (twice) -- seed() with
    min_count=2 would re-add the term unless it is retired."""
    root = make_project(td)
    (root / "source").mkdir()
    (root / "source" / "Chapter_0001.md").write_text(
        "他的灵根觉醒了。\n众人议论灵根的品阶。\n", encoding="utf-8", newline="\n"
    )
    return root


CATALOGUE = {
    "language": "zh", "name": "test catalogue",
    "terms": [
        {"source": "灵根", "translation": "spirit root",
         "definition": "Innate aptitude.", "category": "level"},
    ],
}


def case_1_retire_by_variant() -> None:
    """Retiring via the variant removes the entry and records the canonical
    source; the return list echoes the caller-supplied key."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        removed = glossary.retire(root, ["靈根"])
        data = on_disk(root)
        check("1a variant: removed list echoes the user-supplied key 靈根",
              removed == ["靈根"], f"removed={removed}")
        check("1b variant: entry removed from terms",
              data.get("terms") == [], f"terms={data.get('terms')}")
        check("1c variant: retired records the canonical source 灵根, not 靈根",
              data.get("retired") == ["灵根"], f"retired={data.get('retired')}")


def case_2_retire_by_source() -> None:
    """Retiring by the canonical source produces the identical on-disk
    result; the return list echoes 灵根."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        removed = glossary.retire(root, ["灵根"])
        data = on_disk(root)
        check("2a canonical: removed list echoes the canonical key",
              removed == ["灵根"], f"removed={removed}")
        check("2b canonical: entry removed, retired == ['灵根']",
              data.get("terms") == [] and data.get("retired") == ["灵根"],
              f"terms={data.get('terms')} retired={data.get('retired')}")


def case_3_no_matching_entry() -> None:
    """A source with no matching entry removes nothing, adds nothing to
    'retired', and leaves glossary.json byte-unchanged (no save)."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, retired=["仙人"])
        before = (root / "glossary.json").read_bytes()
        removed = glossary.retire(root, ["道基"])
        data = on_disk(root)
        check("3a unknown: removed list empty", removed == [],
              f"removed={removed}")
        check("3b unknown: entry kept, 'retired' untouched (still ['仙人'])",
              [e.get("source") for e in data.get("terms", [])] == ["灵根"]
              and data.get("retired") == ["仙人"],
              f"terms={data.get('terms')} retired={data.get('retired')}")
        check("3c unknown: glossary.json bytes unchanged",
              (root / "glossary.json").read_bytes() == before)


def case_4_seed_honors_retired() -> None:
    """seed() must not re-add a retired source even though the catalogue
    lists it and the corpus still contains it (count >= min_count). The
    pre-retire twin project proves seed() WOULD have added it."""
    # Precondition twin: identical corpus, glossary emptied WITHOUT retiring
    # -> seed re-adds 灵根 (added=1). This pins that the skip in the retired
    # project comes from the 'retired' list, not from the corpus or catalogue.
    with tempfile.TemporaryDirectory() as td:
        twin = make_seeded_corpus_project(td)
        glossary.save(twin, {"terms": []})
        added, skipped = glossary.seed(twin, CATALOGUE, 2)
        check("4a seed: control project (no retirement) re-adds 灵根",
              added == 1 and skipped == 0, f"added={added} skipped={skipped}")

    # Retire via the VARIANT, then seed: the canonical 灵根 recorded by
    # retire() must keep the catalogue term out (the variant string 靈根 the
    # user typed would NOT have matched the catalogue's 灵根).
    with tempfile.TemporaryDirectory() as td:
        root = make_seeded_corpus_project(td)
        removed = glossary.retire(root, ["靈根"])
        after_retire = (root / "glossary.json").read_bytes()
        check("4b seed: setup - retire via variant removed the entry",
              removed == ["靈根"] and on_disk(root).get("terms") == [],
              f"removed={removed}")
        added, skipped = glossary.seed(root, CATALOGUE, 2)
        data = on_disk(root)
        check("4c seed: retired source skipped (added 0, skipped 1)",
              added == 0 and skipped == 1, f"added={added} skipped={skipped}")
        check("4d seed: 灵根 not re-added, 'retired' still ['灵根']",
              data.get("terms") == [] and data.get("retired") == ["灵根"],
              f"terms={data.get('terms')} retired={data.get('retired')}")
        check("4e seed: nothing added -> glossary.json bytes unchanged",
              (root / "glossary.json").read_bytes() == after_retire)


def case_5_dedup_and_order() -> None:
    """Re-retiring a removed entry is a no-op (no duplicate in 'retired');
    a multi-source retire records canonical sources in call order."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        glossary.retire(root, ["靈根"])
        removed = glossary.retire(root, ["灵根"])
        data = on_disk(root)
        check("5a dedup: second retire (canonical key) removes nothing",
              removed == [] and data.get("terms") == [],
              f"removed={removed}")
        check("5b dedup: 'retired' still exactly ['灵根'] (no duplicate)",
              data.get("retired") == ["灵根"], f"retired={data.get('retired')}")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, entries=[LINGEN_ENTRY, LINGSHI_ENTRY])
        removed = glossary.retire(root, ["灵石", "靈根"])
        data = on_disk(root)
        check("5c multi: removed echoes both caller keys in order",
              removed == ["灵石", "靈根"], f"removed={removed}")
        check("5d multi: canonical sources recorded in call order",
              data.get("retired") == ["灵石", "灵根"],
              f"retired={data.get('retired')}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_retire_by_variant()
    case_2_retire_by_source()
    case_3_no_matching_entry()
    case_4_seed_honors_retired()
    case_5_dedup_and_order()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
