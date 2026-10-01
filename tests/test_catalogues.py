"""Tests for the shipped seed catalogues (assets/catalogues/*.json).

The catalogues are curated data consumed by glossary.load_catalogue() and
glossary.seed() (the `init` / `glossary seed` actions). Nothing validates
them at runtime -- load_catalogue() only checks "is a dict with a terms
list" -- so schema drift would corrupt seeding SILENTLY: seed() skips a
duplicate source without a word, upsert() trusts the field shapes, and a
source colliding with another entry's variant is another silent seed-skip
(find() resolves it to the wrong entry). These tests pin the shipped files
against that drift, key for key with the entry schema documented in
lib/glossary.py's module docstring.

Covered: the catalogue directory yields exactly the four shipped files
(sorted zh-cultivation/zh-modern/zh-units/zh-wuxia .json -- a path typo in
this test must not pass silently); per file the top-level keys are within
{language, name, terms} with language and name non-empty strings and terms
a non-empty list; per entry the key set is within {source, variants,
translation, alt_translations, category, definition} with source,
variants, translation, category, definition REQUIRED, source/translation/
category/definition non-empty strings, variants a list of non-empty
strings that MAY be empty (zh-units.json deliberately ships "variants":
[] entries -- an empty list is legal), and the optional alt_translations a
list of non-empty strings (may be empty or absent); every category is one
of glossary.CATEGORIES; source values are unique per file; no file's
source set intersects the union of its entries' variants; the global term
count is exactly 37.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_catalogues.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
from __future__ import annotations

import json
import sys
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file
# (CWD-independent); the catalogues at novel-translator/assets/catalogues,
# discovered exactly like translate.py's `init` does it
# (sorted(CATALOGUES_DIR.glob("*.json"))).
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary  # noqa: E402

CATALOGUES_DIR = (
    Path(__file__).resolve().parent.parent / "novel-translator" / "assets" / "catalogues"
)

EXPECTED_FILES = ["zh-cultivation.json", "zh-modern.json", "zh-units.json", "zh-wuxia.json"]

ALLOWED_TOP_KEYS = {"language", "name", "terms"}
ALLOWED_ENTRY_KEYS = {
    "source", "variants", "translation", "alt_translations", "category", "definition",
}
REQUIRED_ENTRY_KEYS = {"source", "variants", "translation", "category", "definition"}

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


def load_catalogues() -> dict[str, dict]:
    """{file name: parsed catalogue} for every discovered catalogue file."""
    return {
        path.name: json.loads(path.read_text(encoding="utf-8-sig"))
        for path in sorted(CATALOGUES_DIR.glob("*.json"))
    }


def nonempty_str_list(value: object) -> bool:
    """A list of non-empty strings (the empty list is legal)."""
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
    )


def case_1_discovery(catalogues: dict[str, dict]) -> None:
    """The glob yields exactly the four shipped catalogues -- a typoed path
    (or a renamed/removed catalogue) must fail loudly, not vacuously."""
    check("1a discovery: exactly the four shipped catalogue files",
          sorted(catalogues) == EXPECTED_FILES,
          f"found={sorted(catalogues)}")


def case_2_top_level(catalogues: dict[str, dict]) -> None:
    """Per file: top-level keys within {language, name, terms}; language and
    name non-empty strings; terms a non-empty list."""
    for name, data in catalogues.items():
        stem = name.removesuffix(".json")
        check(f"2a {stem}: top-level keys within {{language, name, terms}}",
              isinstance(data, dict) and set(data) <= ALLOWED_TOP_KEYS,
              f"keys={sorted(data) if isinstance(data, dict) else type(data).__name__}")
        check(f"2b {stem}: language and name are non-empty strings",
              isinstance(data.get("language"), str) and bool(data["language"].strip())
              and isinstance(data.get("name"), str) and bool(data["name"].strip()),
              f"language={data.get('language')!r} name={data.get('name')!r}")
        check(f"2c {stem}: terms is a non-empty list",
              isinstance(data.get("terms"), list) and len(data["terms"]) >= 1,
              f"terms={type(data.get('terms')).__name__}"
              f" len={len(data.get('terms') or [])}")


def case_3_entries(catalogues: dict[str, dict]) -> None:
    """Per entry: keys within the six schema keys; the five required keys
    present; source/translation/category/definition non-empty strings;
    variants a list of non-empty strings (MAY be empty -- zh-units.json
    ships "variants": [] by design); optional alt_translations a list of
    non-empty strings when present."""
    for name, data in catalogues.items():
        stem = name.removesuffix(".json")
        bad_keys = [
            f"terms[{i}] extra={sorted(set(entry) - ALLOWED_ENTRY_KEYS)}"
            f" missing={sorted(REQUIRED_ENTRY_KEYS - set(entry))}"
            for i, entry in enumerate(data["terms"])
            if not isinstance(entry, dict)
            or not set(entry) <= ALLOWED_ENTRY_KEYS
            or not REQUIRED_ENTRY_KEYS <= set(entry)
        ]
        check(f"3a {stem}: every entry keyed within the schema, required keys present",
              not bad_keys, "; ".join(bad_keys))

        bad_strings = [
            f"terms[{i}] ({entry.get('source')!r})"
            for i, entry in enumerate(data["terms"])
            if not all(
                isinstance(entry.get(field), str) and entry[field].strip()
                for field in ("source", "translation", "category", "definition")
            )
        ]
        check(f"3b {stem}: source/translation/category/definition non-empty strings",
              not bad_strings, "; ".join(bad_strings))

        bad_variants = [
            f"terms[{i}] ({entry.get('source')!r}) variants={entry.get('variants')!r}"
            for i, entry in enumerate(data["terms"])
            if not nonempty_str_list(entry.get("variants"))
        ]
        check(f"3c {stem}: variants a list of non-empty strings (may be empty)",
              not bad_variants, "; ".join(bad_variants))

        bad_alts = [
            f"terms[{i}] ({entry.get('source')!r}) alt={entry.get('alt_translations')!r}"
            for i, entry in enumerate(data["terms"])
            if "alt_translations" in entry
            and not nonempty_str_list(entry.get("alt_translations"))
        ]
        check(f"3d {stem}: optional alt_translations a list of non-empty strings",
              not bad_alts, "; ".join(bad_alts))


def case_4_categories(catalogues: dict[str, dict]) -> None:
    """Every entry's category is one of glossary.CATEGORIES -- a category
    outside the tuple would silently fall through every category-aware path
    (e.g. balance's GUIDE_ONLY_CATEGORIES skip, the CLI's category
    validation)."""
    for name, data in catalogues.items():
        stem = name.removesuffix(".json")
        unknown = [
            f"terms[{i}] ({entry.get('source')!r}) category={entry.get('category')!r}"
            for i, entry in enumerate(data["terms"])
            if entry.get("category") not in glossary.CATEGORIES
        ]
        check(f"4a {stem}: every category in glossary.CATEGORIES",
              not unknown, "; ".join(unknown))


def case_5_uniqueness(catalogues: dict[str, dict]) -> None:
    """Per file: source values unique (seed() silently skips a duplicate),
    and the source set disjoint from the union of all entries' variants (a
    source colliding with another entry's variant resolves to that entry in
    find() -- another silent seed-skip)."""
    for name, data in catalogues.items():
        stem = name.removesuffix(".json")
        sources = [entry["source"] for entry in data["terms"]]
        dupes = sorted({s for s in sources if sources.count(s) > 1})
        check(f"5a {stem}: source values unique (seed() skips duplicates silently)",
              not dupes, f"dupes={dupes}")

        all_variants: set[str] = set()
        for entry in data["terms"]:
            all_variants.update(entry.get("variants") or [])
        collisions = sorted(set(sources) & all_variants)
        check(f"5b {stem}: no source equals another entry's variant",
              not collisions, f"collisions={collisions}")


def case_6_total(catalogues: dict[str, dict]) -> None:
    """The global term count is exactly 37. Curation-hygiene growth bound:
    keep the shipped catalogues <= 50 terms total (an internal guideline,
    not a documented limit)."""
    total = sum(len(data["terms"]) for data in catalogues.values())
    per_file = ", ".join(
        f"{name}={len(data['terms'])}" for name, data in catalogues.items()
    )
    check("6a total: the four catalogues ship exactly 37 terms", total == 37,
          f"total={total} ({per_file})")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    catalogues = load_catalogues()
    case_1_discovery(catalogues)
    case_2_top_level(catalogues)
    case_3_entries(catalogues)
    case_4_categories(catalogues)
    case_5_uniqueness(catalogues)
    case_6_total(catalogues)

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
