"""Regression tests for glossary.contextual() cross-entry longest-first counting.

contextual() must count with a single regex alternation over ALL entries'
matchable strings (source + variants), sorted longest-first: each text
occurrence credits only the entry/entries owning the longest string that
matches at that position. Motivating incident: body text 修仙界 wrongly
credited the entry 仙界 even though the 修仙 match consumes those
characters, so a shorter term shadowed by another entry's in-place match
must receive no credit.

Also pins count_in_text()'s unchanged per-entry substring semantics (used
by seed()) and balance.check()'s drift-signal tier.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_glossary_counting.py
"""

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


def make_glossary(*entries: dict) -> dict:
    return {"terms": list(entries)}


def counts(pairs: list) -> dict:
    """source -> count map from contextual() output."""
    return {e.get("source", ""): c for e, c in pairs}


def present(pairs: list, source: str) -> bool:
    return any(e.get("source") == source for e, _c in pairs)


def count_of(pairs: list, entry: dict) -> int:
    """Count for one specific entry object (identity; sources may repeat)."""
    return next((c for e, c in pairs if e is entry), 0)


def case_1_nested_compound() -> None:
    """The incident: 仙界 inside 修仙界 must get no credit."""
    g = make_glossary(
        {"source": "修仙", "translation": "immortal cultivation"},
        {"source": "仙界", "translation": "Immortal Realm"},
    )
    body = "从而回到修仙界。\n修仙界灵气充裕。\n修仙，成为仙人。"
    pairs = glossary.contextual(g, body, cap=10)
    c = counts(pairs)
    check("1a nested: 仙界 absent from pairs (shadowed by 修仙)",
          not present(pairs, "仙界"), f"pairs={c}")
    check("1b nested: 修仙 count 3", c.get("修仙") == 3, f"pairs={c}")


def case_2_standalone() -> None:
    """仙界 on its own still counts; 修仙 absent when it never occurs."""
    g = make_glossary(
        {"source": "修仙", "translation": "immortal cultivation"},
        {"source": "仙界", "translation": "Immortal Realm"},
    )
    body = "他飞升去了仙界。仙界广阔无垠。"
    pairs = glossary.contextual(g, body, cap=10)
    c = counts(pairs)
    check("2a standalone: 仙界 count 2", c.get("仙界") == 2, f"pairs={c}")
    check("2b standalone: 修仙 absent from pairs", not present(pairs, "修仙"),
          f"pairs={c}")


def case_3_longer_entry_wins() -> None:
    """Cross-entry: 凡人界域 consumes the 界域 overlap."""
    g = make_glossary(
        {"source": "凡人界域", "translation": "mortal realm"},
        {"source": "界域", "translation": "domain"},
    )
    body = "凡人界域又被称为绝域。"
    pairs = glossary.contextual(g, body, cap=10)
    c = counts(pairs)
    check("3a longer: 凡人界域 count 1", c.get("凡人界域") == 1, f"pairs={c}")
    check("3b longer: 界域 absent from pairs", not present(pairs, "界域"),
          f"pairs={c}")


def case_4_shared_exact_string() -> None:
    """Two entries owning the same source each get credit per occurrence."""
    e1 = {"source": "昆仑", "translation": "Kunlun"}
    e2 = {"source": "昆仑", "translation": "Kunlun Mountain"}
    g = make_glossary(e1, e2)
    body = "昆仑之巅，昆仑山下。"
    pairs = glossary.contextual(g, body, cap=10)
    check("4a shared: first 昆仑 owner count 2", count_of(pairs, e1) == 2,
          f"pairs={counts(pairs)}")
    check("4b shared: second 昆仑 owner count 2", count_of(pairs, e2) == 2,
          f"pairs={counts(pairs)}")


def case_5_variants_within_entry() -> None:
    """Within-entry longest-first: 小丫 inside 裴小丫 counts once, standalone once."""
    entry = {"source": "裴小丫", "translation": "Pei Xiaoya", "variants": ["小丫"]}
    g = make_glossary(entry)
    body = "裴小丫点头。小丫笑了。"
    pairs = glossary.contextual(g, body, cap=10)
    check("5 variants: 裴小丫/小丫 entry count 2", count_of(pairs, entry) == 2,
          f"pairs={counts(pairs)}")


def case_6_count_in_text_unchanged() -> None:
    """count_in_text stays per-entry substring counting (no cross-entry awareness)."""
    entry = {"source": "仙界", "translation": "Immortal Realm"}
    got = glossary.count_in_text(entry, "修仙界修仙界")
    check("6 count_in_text: 仙界 in 修仙界修仙界 == 2", got == 2, f"got={got}")


def case_7_cap_and_sort() -> None:
    """cap truncates after the (-count, source) sort; ties break by source asc."""
    g = make_glossary(
        {"source": "灵石", "translation": "spirit stone"},
        {"source": "外门", "translation": "outer sect"},
        {"source": "内门", "translation": "inner sect"},
        {"source": "弟子", "translation": "disciple"},
    )
    body = "灵石、灵石、灵石。内门与外门争锋。内门弟子进入外门。"
    full = glossary.contextual(g, body, cap=10)
    order = [e.get("source", "") for e, _c in full]
    check("7a cap/sort: full order 灵石,内门,外门,弟子",
          order == ["灵石", "内门", "外门", "弟子"], f"order={order}")
    top = glossary.contextual(g, body, cap=1)
    check("7b cap/sort: cap=1 returns only 灵石 (count 3)",
          len(top) == 1 and top[0][0].get("source") == "灵石" and top[0][1] == 3,
          f"top={[(e.get('source'), c) for e, c in top]}")


def case_8_balance_drift() -> None:
    """balance.check: drift signal iff src >= 2 and canonical rendering absent."""
    xianjie = {"source": "仙界", "translation": "Immortal Realm"}
    pairs = [(xianjie, 2)]
    src_body = "他飞升去了仙界。仙界广阔无垠。"

    lines_a = [
        "He returned to the immortal cultivation world.",
        "Spiritual energy filled the immortal cultivation world.",
    ]
    drift_a, _warn_a, _info_a = balance.check(pairs, src_body, lines_a)
    check("8a balance: exactly 1 drift signal when rendering absent",
          len(drift_a) == 1, f"drift={drift_a}")
    if drift_a:
        sig = drift_a[0]
        check("8b balance: message mentions 仙界 and Immortal Realm",
              "仙界" in sig.get("message", "") and "Immortal Realm" in sig.get("message", ""),
              f"message={sig.get('message', '')!r}")
        check("8c balance: signal counts src 2 / tgt 0",
              sig.get("src_count") == 2 and sig.get("tgt_count") == 0,
              f"signal={sig}")
    else:
        check("8b balance: message mentions 仙界 and Immortal Realm", False,
              "no drift signal to inspect")
        check("8c balance: signal counts src 2 / tgt 0", False,
              "no drift signal to inspect")

    lines_b = [
        "He ascended to the Immortal Realm.",
        "Past the Immortal Realm's edge lies mist.",
    ]
    drift_b, _warn_b, _info_b = balance.check(pairs, src_body, lines_b)
    check("8d balance: zero drift signals when rendering present",
          len(drift_b) == 0, f"drift={drift_b}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_nested_compound()
    case_2_standalone()
    case_3_longer_entry_wins()
    case_4_shared_exact_string()
    case_5_variants_within_entry()
    case_6_count_in_text_unchanged()
    case_7_cap_and_sort()
    case_8_balance_drift()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
