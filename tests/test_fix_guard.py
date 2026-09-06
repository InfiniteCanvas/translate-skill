"""Tests for fix.invalid_translation_reason() and run_commands' skip guard.

`review fix` shells each report command out to the CLI where nothing would
otherwise re-check model suggestions; invalid_translation_reason() is the
in-process guard: a `glossary replace`/`glossary set` argv whose
--translation value still contains CJK for a CJK-source entry (looked up
by source or variants) must be skipped with "suggestion not in target
language", a blank value with "empty translation". The guard mirrors
review.apply_fixes(): it only fires for CJK-source entries, and never for
other verbs, missing --translation flags, or unknown sources -- the
subprocess reports those cases itself.

The final case runs run_commands() end-to-end against a temp project: the
invalid spec is skipped in-process (skipped_invalid=1, no subprocess, no
glossary write) while the benign read-only spec (glossary search) runs and
applies.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_fix_guard.py
"""

import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import fix, glossary  # noqa: E402

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


def make_glossary() -> dict:
    """灵根 (variants 靈根) is CJK-source; Excalibur is not."""
    return {"terms": [
        {"source": "灵根", "variants": ["靈根"], "translation": "spirit root"},
        {"source": "Excalibur", "variants": [], "translation": "the sword"},
    ]}


def reason(argv: list[str]) -> str | None:
    return fix.invalid_translation_reason(argv, make_glossary())


def case_1_cjk_suggestion() -> None:
    """CJK suggestion on a CJK-source entry -> the target-language reason,
    for both covered verbs and both flag spellings."""
    check("1a replace: CJK --translation flagged",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "灵力"]) == "suggestion not in target language")
    check("1b set: CJK --translation flagged",
          reason(["glossary", "set", "--source", "灵根",
                  "--translation", "灵力"]) == "suggestion not in target language")
    check("1c mixed-script suggestion still contains CJK -> flagged",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "spirit 灵力"]) == "suggestion not in target language")
    check("1d '--translation=灵力' (= form) flagged",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation=灵力"]) == "suggestion not in target language")
    check("1e '--source=灵根' (= form) resolves the entry too",
          reason(["glossary", "replace", "--source=灵根",
                  "--translation", "灵力"]) == "suggestion not in target language")


def case_2_valid_suggestions() -> None:
    """English suggestions and lookup via the variant are handled."""
    check("2a English --translation -> None",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "spiritual root"]) is None)
    check("2b variant lookup (靈根) with CJK suggestion -> flagged",
          reason(["glossary", "replace", "--source", "靈根",
                  "--translation", "道基"]) == "suggestion not in target language")
    check("2c variant lookup with English suggestion -> None",
          reason(["glossary", "set", "--source", "靈根",
                  "--translation", "spirit essence"]) is None)


def case_3_empty_and_missing() -> None:
    """Blank --translation is its own reason; a missing --translation flag
    is not guarded (definitions may quote source text)."""
    check("3a blank --translation -> 'empty translation'",
          reason(["glossary", "replace", "--source", "灵根",
                  "--translation", "  "]) == "empty translation")
    check("3b no --translation at all (set --definition) -> None",
          reason(["glossary", "set", "--source", "灵根",
                  "--definition", "又称为灵力"]) is None)


def case_4_out_of_scope() -> None:
    """Unknown sources, non-covered verbs, and non-CJK-source entries are
    the subprocess's business, not the guard's."""
    check("4a unknown source -> None",
          reason(["glossary", "replace", "--source", "道基",
                  "--translation", "灵力"]) is None)
    check("4b glossary merge -> None",
          reason(["glossary", "merge", "--keep", "a", "--remove", "b"]) is None)
    check("4c glossary retire -> None",
          reason(["glossary", "retire", "--source", "x"]) is None)
    check("4d util replace -> None",
          reason(["util", "replace", "--source", "a",
                  "--target", "b"]) is None)
    check("4e non-CJK source (Excalibur) with CJK suggestion -> None "
          "(guard mirrors apply_fixes: CJK-source entries only)",
          reason(["glossary", "replace", "--source", "Excalibur",
                  "--translation", "灵力"]) is None)


def case_5_run_commands_integration() -> None:
    """run_commands skips the invalid spec in-process (no subprocess, no
    glossary write) and still runs the benign read-only spec."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        before = (root / "glossary.json").read_bytes()
        specs = [
            fix.CommandSpec(
                raw="glossary replace --source 灵根 --translation 灵力",
                argv=["glossary", "replace", "--source", "灵根",
                      "--translation", "灵力"],
                line_no=1, finding={}),
            fix.CommandSpec(
                raw="glossary search 灵根",   # benign, read-only, exit 0
                argv=["glossary", "search", "灵根"],
                line_no=2, finding={}),
        ]
        result = fix.run_commands(root, SCRIPTS / "translate.py", specs)
        check("5a run_commands: invalid spec skipped (skipped_invalid=1)",
              result["skipped_invalid"] == 1, f"result={result}")
        check("5b run_commands: exactly one subprocess started (specs_run=1)",
              result["specs_run"] == 1, f"result={result}")
        check("5c run_commands: benign spec succeeded (applied 1, failed 0)",
              result["applied"] == 1 and result["failed"] == 0,
              f"result={result}")
        check("5d run_commands: glossary.json byte-unchanged",
              (root / "glossary.json").read_bytes() == before)


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_cjk_suggestion()
    case_2_valid_suggestions()
    case_3_empty_and_missing()
    case_4_out_of_scope()
    case_5_run_commands_integration()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
