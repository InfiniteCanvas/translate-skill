"""Tests for epub._slugify: ASCII-word slugs, Unicode-preserving fallback.

Motivating incident: a pure-CJK novel title slugged to the empty string and
every export collapsed to novel.epub. The fallback must preserve the
original-language title instead, while ASCII titles keep the hyphenated
lowercase slug (and title_translated — an English title — wins in build(),
which is not retested here).

Self-contained PASS/FAIL script (no pytest). epub.py imports ebooklib, so
run via uv (deps declared inline below):

    uv run tests/test_epub_slug.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["ebooklib>=0.18", "pillow>=10.0", "pyyaml>=6.0"]
# ///
from __future__ import annotations

import sys
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import epub  # noqa: E402

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


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    check("1 ascii: words joined lowercase",
          epub._slugify("Renegade Immortal") == "renegade-immortal",
          epub._slugify("Renegade Immortal"))
    check("2 ascii: punctuation dropped",
          epub._slugify("Re:Monster — Volume 2!") == "re-monster-volume-2",
          epub._slugify("Re:Monster — Volume 2!"))
    check("3 mixed: ASCII words win, CJK dropped",
          epub._slugify("無職転生 Mushoku Tensei") == "mushoku-tensei",
          epub._slugify("無職転生 Mushoku Tensei"))
    check("4 cjk: pure-CJK title preserved instead of 'novel'",
          epub._slugify("凡人修仙传") == "凡人修仙传", epub._slugify("凡人修仙传"))
    check("5 cjk: internal whitespace collapsed to hyphens",
          epub._slugify("穿越 千年") == "穿越-千年", epub._slugify("穿越 千年"))
    check("6 cjk: filesystem-unsafe characters stripped",
          epub._slugify('天?下\\无/敌:*"?<>|') == "天下无敌",
          epub._slugify('天?下\\无/敌:*"?<>|'))
    check("7 cjk: trailing dots/spaces trimmed (Windows)",
          epub._slugify("凡人修仙传 .") == "凡人修仙传", epub._slugify("凡人修仙传 ."))
    check("8 empty: falls back to 'novel'",
          epub._slugify("???") == "novel", epub._slugify("???"))

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
