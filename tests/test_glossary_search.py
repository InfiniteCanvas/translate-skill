"""Tests for glossary.search(): substring + fuzzy Levenshtein lookup.

search() must find entries across source / variants / translation /
alt_translations with case-insensitive substring matching (distance 0,
always active) plus whole-value and per-token levenshtein fuzzy matching
(only when max_distance > 0 and len(query) > max_distance). Edit distance
is uniform on casefolded strings, so separator variants ('grand elder' vs
'grand-elder' / 'grand_elder' / 'grandxelder') are all distance 1 apart.
Retired sources are matched the same way but returned separately and
never counted as matches.

All exact distances asserted here were verified against
balance.levenshtein() (the same implementation search() uses); the
load-bearing ones are re-pinned as preconditions inside the cases.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_glossary_search.py
"""

import copy
import sys
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import balance, glossary  # noqa: E402

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


def make_glossary(*entries: dict, retired: list[str] | None = None) -> dict:
    g = {"terms": list(entries)}
    if retired is not None:
        g["retired"] = retired
    return g


def sources(result: dict) -> list[str]:
    """source list of matched entries, in result order."""
    return [m["entry"].get("source", "") for m in result["matches"]]


def hits_of(result: dict, source: str) -> list[tuple[str, str, int]]:
    """hits list for the matched entry with this source, else []."""
    for m in result["matches"]:
        if m["entry"].get("source") == source:
            return m["hits"]
    return []


def case_1_substring_each_field() -> None:
    """Substring hits report distance 0 with the right kind for each field."""
    g = make_glossary(
        {"source": "灵石", "translation": "spirit stone"},
        {"source": "仙界", "translation": "Immortal Realm"},
        {"source": "筑基", "variants": ["築基"],
         "translation": "Foundation Establishment"},
        {"source": "飞升", "alt_translations": ["immortal ascension"],
         "translation": "ascension"},
    )
    r = glossary.search(g, "灵石")
    check("1a substring source: 灵石 hit (source, 灵石, 0)",
          hits_of(r, "灵石") == [("source", "灵石", 0)], f"hits={hits_of(r, '灵石')}")
    r = glossary.search(g, "immortal realm")
    check("1b substring translation: 仙界 hit (translation, Immortal Realm, 0)",
          hits_of(r, "仙界") == [("translation", "Immortal Realm", 0)],
          f"hits={hits_of(r, '仙界')}")
    r = glossary.search(g, "築基")
    check("1c substring variant: 筑基 hit (variant, 築基, 0)",
          hits_of(r, "筑基") == [("variant", "築基", 0)], f"hits={hits_of(r, '筑基')}")
    r = glossary.search(g, "immortal ascension")
    check("1d substring alt: 飞升 hit (alt, immortal ascension, 0)",
          hits_of(r, "飞升") == [("alt", "immortal ascension", 0)],
          f"hits={hits_of(r, '飞升')}")


def case_2_case_insensitive() -> None:
    """Query case never matters: GRAND finds translation Grand Elder."""
    g = make_glossary({"source": "大长老", "translation": "Grand Elder"})
    r = glossary.search(g, "GRAND")
    check("2 case-insensitive: GRAND -> Grand Elder at distance 0",
          hits_of(r, "大长老") == [("translation", "Grand Elder", 0)],
          f"hits={hits_of(r, '大长老')}")


def case_3_user_examples() -> None:
    """'grand elder' finds Grand-Elder / Grand_Elder / Grandxelder, each 1."""
    # Preconditions: the exact distances this case pins (verified by hand).
    for value in ("grand-elder", "grand_elder", "grandxelder"):
        assert balance.levenshtein("grand elder", value) == 1
    g = make_glossary(
        {"source": "大长老", "translation": "Grand-Elder"},
        {"source": "二长老", "translation": "Grand_Elder"},
        {"source": "三长老", "translation": "Grandxelder"},
    )
    r = glossary.search(g, "grand elder")
    check("3a user example: all three entries found",
          sources(r) == ["大长老", "二长老", "三长老"], f"sources={sources(r)}")
    check("3b user example: Grand-Elder distance 1",
          hits_of(r, "大长老") == [("translation", "Grand-Elder", 1)],
          f"hits={hits_of(r, '大长老')}")
    check("3c user example: Grand_Elder distance 1",
          hits_of(r, "二长老") == [("translation", "Grand_Elder", 1)],
          f"hits={hits_of(r, '二长老')}")
    check("3d user example: Grandxelder distance 1",
          hits_of(r, "三长老") == [("translation", "Grandxelder", 1)],
          f"hits={hits_of(r, '三长老')}")


def case_4_reverse_direction() -> None:
    """Separators cut both ways: 'grand-elder' finds 'Grand Elder' at 1."""
    assert balance.levenshtein("grand-elder", "grand elder") == 1
    g = make_glossary({"source": "大长老", "translation": "Grand Elder"})
    r = glossary.search(g, "grand-elder")
    check("4 reverse: grand-elder -> Grand Elder distance 1",
          hits_of(r, "大长老") == [("translation", "Grand Elder", 1)],
          f"hits={hits_of(r, '大长老')}")


def case_5_token_level_fuzzy() -> None:
    """Single-word typo inside a multi-word value: the whole value is far
    beyond the band (length gap alone is 13; lev(..., band=2) short-circuits
    to 3) but the whitespace token 'foundation' is distance 1."""
    assert balance.levenshtein("foundaition", "foundation establishment", band=2) == 3
    assert balance.levenshtein("foundaition", "foundation") == 1
    g = make_glossary({"source": "筑基", "translation": "Foundation Establishment"})
    r = glossary.search(g, "Foundaition")
    check("5 token fuzzy: Foundaition -> Foundation Establishment distance 1",
          hits_of(r, "筑基") == [("translation", "Foundation Establishment", 1)],
          f"hits={hits_of(r, '筑基')}")


def case_6_distance_tiers() -> None:
    """Distance 2 matches at max_distance=2; genuine distance 3 does not."""
    assert balance.levenshtein("grand elderly", "grand elder") == 2
    assert balance.levenshtein("grund elderly", "grand elder") == 3
    g = make_glossary({"source": "大长老", "translation": "Grand Elder"})
    r = glossary.search(g, "Grand Elderly")
    check("6a distance 2: Grand Elderly matches at distance 2",
          hits_of(r, "大长老") == [("translation", "Grand Elder", 2)],
          f"hits={hits_of(r, '大长老')}")
    r = glossary.search(g, "Grund Elderly")
    check("6b distance 3: Grund Elderly does not match at max_distance=2",
          sources(r) == [], f"sources={sources(r)}")
    r = glossary.search(g, "Grund Elderly", max_distance=3)
    check("6c distance 3: same query matches at distance 3 with band 3",
          hits_of(r, "大长老") == [("translation", "Grand Elder", 3)],
          f"hits={hits_of(r, '大长老')}")


def case_7_substring_only_mode() -> None:
    """max_distance=0 disables fuzzy entirely: Grand Elder found (substring),
    Grand-Elder not (edit distance 1 would otherwise match)."""
    g = make_glossary(
        {"source": "甲", "translation": "Grand Elder"},
        {"source": "乙", "translation": "Grand-Elder"},
    )
    r = glossary.search(g, "grand elder", max_distance=0)
    check("7 substring only: finds Grand Elder at 0, skips Grand-Elder",
          sources(r) == ["甲"] and hits_of(r, "甲") == [("translation", "Grand Elder", 0)],
          f"sources={sources(r)} hits={hits_of(r, '甲')}")


def case_8_short_query_skips_fuzzy() -> None:
    """len(query) <= max_distance disables fuzzy: 'ab' (len 2) must NOT match
    'xy' even though lev == max_distance == 2; substring still works. A
    3-char query (len > max_distance) does fuzzy-match at distance 1."""
    assert balance.levenshtein("ab", "xy") == 2
    g = make_glossary(
        {"source": "丙", "translation": "xy"},
        {"source": "丁", "translation": "abracadabra"},
    )
    r = glossary.search(g, "ab")
    check("8a short query: 'ab' skips fuzzy (no 'xy' match)",
          sources(r) == ["丁"], f"sources={sources(r)}")
    check("8b short query: substring 'ab' in 'abracadabra' still distance 0",
          hits_of(r, "丁") == [("translation", "abracadabra", 0)],
          f"hits={hits_of(r, '丁')}")
    g2 = make_glossary({"source": "戊", "translation": "abd"})
    r2 = glossary.search(g2, "abc")
    check("8c len>max query: 'abc' fuzzy-matches 'abd' at distance 1",
          hits_of(r2, "戊") == [("translation", "abd", 1)], f"hits={hits_of(r2, '戊')}")


def case_9_multi_field_single_entry() -> None:
    """Query hitting source AND translation: one match, two hits, entry once."""
    g = make_glossary({"source": "Spirit", "translation": "Spirit Stone"})
    r = glossary.search(g, "spirit")
    check("9 multi-field: single match with source+translation hits in order",
          len(r["matches"]) == 1
          and hits_of(r, "Spirit")
          == [("source", "Spirit", 0), ("translation", "Spirit Stone", 0)],
          f"matches={len(r['matches'])} hits={hits_of(r, 'Spirit')}")


def case_10_ranking() -> None:
    """Substring entries sort before fuzzy ones; fuzzy sorts by distance;
    equal ranks keep glossary file order."""
    g = make_glossary(
        {"source": "甲", "translation": "Grand-Elder"},  # fuzzy 1, first in file
        {"source": "乙", "translation": "Grand Elder"},   # substring 0
    )
    r = glossary.search(g, "grand elder")
    check("10a ranking: substring entry sorts before fuzzy entry",
          sources(r) == ["乙", "甲"], f"sources={sources(r)}")
    # 'grand elder' would be a substring of 'Grand Elderly', so this case
    # uses a query that can only fuzzy-match either value.
    assert balance.levenshtein("grand elderly", "grand eldery") == 1
    assert balance.levenshtein("grand elderly", "grand elder") == 2
    g2 = make_glossary(
        {"source": "B1", "translation": "Grand Elder"},   # distance 2, first in file
        {"source": "B2", "translation": "Grand Eldery"},   # distance 1
    )
    r2 = glossary.search(g2, "grand elderly")
    check("10b ranking: smaller fuzzy distance first",
          sources(r2) == ["B2", "B1"], f"sources={sources(r2)}")
    g3 = make_glossary(
        {"source": "A1", "translation": "Grand-Elder"},
        {"source": "A2", "translation": "GrandxElder"},
        {"source": "A3", "translation": "Grand_Elder"},
    )
    r3 = glossary.search(g3, "grand elder")
    check("10c ranking: equal distances keep file order",
          sources(r3) == ["A1", "A2", "A3"], f"sources={sources(r3)}")


def case_11_retired() -> None:
    """Retired sources match the same way but land in 'retired', never in
    'matches', and don't change the live match count. ('spirit ston' would
    be a substring of 'Spirit Stones', so the fuzzy check uses a query two
    edits away that contains no substring hit.)"""
    assert balance.levenshtein("spirit stonze", "spirit stones") == 2
    g = make_glossary(
        {"source": "仙人", "translation": "immortal"},
        retired=["Spirit Stones"],
    )
    r = glossary.search(g, "spirit stonze")
    check("11a retired: fuzzy-only-retired query has no live matches",
          r["matches"] == [], f"matches={sources(r)}")
    check("11b retired: Spirit Stones returned with distance 2",
          r["retired"] == [("Spirit Stones", 2)], f"retired={r['retired']}")
    r2 = glossary.search(g, "immortel")
    check("11c retired: live fuzzy match unaffected by retired list",
          sources(r2) == ["仙人"] and r2["retired"] == [],
          f"sources={sources(r2)} retired={r2['retired']}")
    r3 = glossary.search(g, "spirit stones")
    check("11d retired: substring retired match reports distance 0",
          r3["matches"] == [] and r3["retired"] == [("Spirit Stones", 0)],
          f"matches={sources(r3)} retired={r3['retired']}")


def case_12_empty_query_and_purity() -> None:
    """Empty query returns an empty result; search() never mutates the
    glossary dict."""
    g = make_glossary(
        {"source": "灵石", "translation": "spirit stone"},
        retired=["仙人"],
    )
    r = glossary.search(g, "")
    check("12a empty query: empty matches and retired",
          r == {"matches": [], "retired": []}, f"result={r}")
    before = copy.deepcopy(g)
    glossary.search(g, "grand elder", max_distance=3)
    check("12b purity: glossary dict unchanged by search()", g == before)


def case_13_cjk() -> None:
    """CJK queries: substring on source; traditional variant via variants."""
    g = make_glossary({"source": "筑基", "variants": ["築基"],
                       "translation": "Foundation Establishment"})
    r = glossary.search(g, "筑基")
    check("13a CJK: 筑基 substring hit on source",
          hits_of(r, "筑基") == [("source", "筑基", 0)], f"hits={hits_of(r, '筑基')}")
    r2 = glossary.search(g, "築基")
    check("13b CJK: 築基 substring hit on variant",
          hits_of(r2, "筑基") == [("variant", "築基", 0)], f"hits={hits_of(r2, '筑基')}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_substring_each_field()
    case_2_case_insensitive()
    case_3_user_examples()
    case_4_reverse_direction()
    case_5_token_level_fuzzy()
    case_6_distance_tiers()
    case_7_substring_only_mode()
    case_8_short_query_skips_fuzzy()
    case_9_multi_field_single_entry()
    case_10_ranking()
    case_11_retired()
    case_12_empty_query_and_purity()
    case_13_cjk()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
