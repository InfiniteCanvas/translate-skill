# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary

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


def seeded_entry() -> dict:
    """A realistic existing entry carrying alts (the seed() shape)."""
    return {
        "source": "灵根", "translation": "spirit root",
        "variants": ["靈根"], "alt_translations": ["spirit core"],
        "definition": "Innate aptitude.", "category": "level",
        "origin": "seeded", "first_seen_chapter": 0,
    }


def case_1_alt_preservation() -> None:
    """The alt_translations contract: absent key preserves the existing
    value, explicit key (even []) overwrites, brand-new entry gets []."""
    g = {"terms": [seeded_entry()]}
    replaced = glossary.upsert(g, {
        "source": "灵根", "translation": "spiritual root",
        "definition": "Updated.", "category": "level",
    })
    entry = g["terms"][0]
    check("1a preserve: absent key keeps the existing alts",
          replaced is True and len(g["terms"]) == 1
          and entry["alt_translations"] == ["spirit core"],
          f"replaced={replaced} alts={entry.get('alt_translations')!r}")

    g = {"terms": [seeded_entry()]}
    replaced = glossary.upsert(g, {
        "source": "灵根", "translation": "spiritual root",
        "alt_translations": [],
    })
    check("1b overwrite: explicit empty list clears the alts",
          replaced is True and g["terms"][0]["alt_translations"] == [],
          f"alts={g['terms'][0].get('alt_translations')!r}")

    g = {"terms": [seeded_entry()]}
    glossary.upsert(g, {
        "source": "灵根", "translation": "spiritual root",
        "alt_translations": ["root", "essence"],
    })
    check("1c overwrite: explicit list replaces the old alts",
          g["terms"][0]["alt_translations"] == ["root", "essence"],
          f"alts={g['terms'][0].get('alt_translations')!r}")

    g = {"terms": []}
    appended = glossary.upsert(g, {"source": "道基", "translation": "dao base"})
    check("1d default: brand-new entry without the key gets []",
          appended is False and len(g["terms"]) == 1
          and g["terms"][0]["alt_translations"] == [],
          f"appended={appended} entry={g['terms'][0]}")

    g = {"terms": []}
    glossary.upsert(g, {"source": "道基", "translation": "dao base",
                        "alt_translations": None})
    check("1e default: brand-new entry with explicit None gets []",
          g["terms"][0]["alt_translations"] == [],
          f"alts={g['terms'][0].get('alt_translations')!r}")

    g = {"terms": [seeded_entry(),
                   {"source": "other", "translation": "x"}]}
    glossary.upsert(g, {"source": "靈根", "translation": "spirit essence"})
    check("1f variant lookup: replaces the canonical entry, alts preserved",
          len(g["terms"]) == 2
          and g["terms"][0]["source"] == "靈根"
          and g["terms"][0]["translation"] == "spirit essence"
          and g["terms"][0]["alt_translations"] == ["spirit core"]
          and g["terms"][1] == {"source": "other", "translation": "x"},
          f"terms={g['terms']}")


def case_2_normalization_and_position() -> None:
    """Minimal entries are normalized (variants None -> [], category/
    origin/first_seen_chapter defaults) and a replacement lands in the
    exact list slot the old entry occupied."""
    g = glossary.empty()
    appended = glossary.upsert(g, {
        "source": "荒塔", "translation": "wild pagoda", "variants": None,
    })
    entry = g["terms"][0]
    check("2a normalize: append fills variants/category/origin/first_seen",
          appended is False and entry["variants"] == []
          and entry["category"] == "other" and entry["origin"] == "model"
          and entry["first_seen_chapter"] is None,
          f"entry={entry}")

    tail = {"source": "zzz", "translation": "last"}
    glossary.upsert(g, tail)
    replaced = glossary.upsert(g, {
        "source": "荒塔", "translation": "ancient pagoda",
        "variants": None, "alt_translations": ["old pagoda"],
    })
    check("2b normalize: replacement keeps its own explicit alts verbatim",
          replaced is True and g["terms"][0]["alt_translations"] == ["old pagoda"],
          f"terms={g['terms']}")
    check("2c normalize: replacement preserves the list position",
          len(g["terms"]) == 2 and g["terms"][0]["source"] == "荒塔"
          and g["terms"][1]["source"] == "zzz"
          and g["terms"][1]["translation"] == "last",
          f"order={[t['source'] for t in g['terms']]}")

    g2: dict = {}
    glossary.upsert(g2, {"source": "a", "translation": "b"})
    check("2d normalize: a bare dict gains a terms list",
          g2.get("terms") and g2["terms"][0]["source"] == "a",
          f"g2={g2}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_alt_preservation()
    case_2_normalization_and_position()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
