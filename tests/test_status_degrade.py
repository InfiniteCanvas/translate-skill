"""Tests for cmd_status's degraded-project tolerance (H6).

status must stay the one command that always works on a damaged project:
an unreadable glossary.json (here a JSON array, which glossary.load
rejects with ValueError) degrades to the inline
"{'glossary':>13}: [warn] unreadable (...)" line, a novel_info.json that
is not a JSON object (here "[]") is swallowed by the lenient
project.load_novel_info ({} -> "default fallback" style tier), and the
command still exits 0 with the full chapter table -- no traceback, no
[FAIL], the remaining sections intact.

cmd_status is driven in-process under redirect_stdout (test_sync's
pattern); the fixture keeps the manifest/source pair consistent so the
only damage is the two corrupt files under test.

Self-contained PASS/FAIL script (no pytest). The lib modules and
scripts/translate.py import pyyaml, requests, ebooklib and pillow, so run
via uv (deps declared inline below):

    uv run tests/test_status_degrade.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from translate import cmd_status

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


def make_degraded_project(root: Path) -> None:
    """A manifest/source pair that agrees, plus the two corrupt files:
    glossary.json is a JSON array (glossary.load wants an object) and
    novel_info.json is a JSON array (load_novel_info wants an object)."""
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "CHAPTER_0001.md").write_text(
        "第一章 初见\n\n林凡睁开双眼。\n", encoding="utf-8", newline="\n"
    )
    manifest = [
        {"order": 1, "file": "CHAPTER_0001.md", "status": "pending",
         "title": "初见"},
    ]
    (root / "chapters.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    (root / "glossary.json").write_text("[1,2]", encoding="utf-8", newline="\n")
    (root / "novel_info.json").write_text("[]", encoding="utf-8", newline="\n")


def case_1_status_degrades() -> None:
    """Corrupt glossary.json + non-object novel_info.json: exit 0, the
    [warn] unreadable line, and no traceback."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_degraded_project(root)

        buf = io.StringIO()
        error: BaseException | None = None
        try:
            with contextlib.redirect_stdout(buf):
                rc = cmd_status(argparse.Namespace(why=False), root)
        except BaseException as exc:
            error = exc
            rc = None
        out = buf.getvalue()

        check("1a status: exits 0", error is None and rc == 0,
              f"rc={rc} error={error!r}")
        check("1b status: no exception raised", error is None,
              f"{type(error).__name__}: {error}" if error else "")
        check("1c status: [warn] unreadable printed", "[warn] unreadable" in out,
              f"out={out!r}")
        check("1d status: no traceback in output", "Traceback" not in out)
        check("1e status: no [FAIL] line", "[FAIL]" not in out)
        check("1f status: chapter table still rendered",
              "CHAPTER_0001.md" in out and "pending" in out, f"out={out!r}")
        check("1g status: glossary line present in the section block",
              "glossary" in out)
        check("1h status: style section survives novel_info=[]",
              "default fallback" in out, f"out={out!r}")
        check("1i status: tn_history section still printed", "tn_history" in out)


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_status_degrades()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
