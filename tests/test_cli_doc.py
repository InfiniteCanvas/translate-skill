# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import argparse
import difflib
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gen_cli_doc
import translate

PASSED = 0
FAILED: list[str] = []

PROBE = "zzz-synthetic-probe"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def _with_extra_subcommand(name: str):
    """The real parser plus one synthetic top-level subcommand, so a test can
    prove the document is DERIVED from the tree rather than assembled from a
    list the generator happens to keep beside it."""
    real = translate._build_parser

    def build_with_extra():
        parser = real()
        sub = [a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction)][0]
        sub.add_parser(name, allow_abbrev=False,
                       help="synthetic probe subcommand")
        return parser

    return real, build_with_extra


def case_committed_file_matches_the_parser() -> None:
    """references/cli.md must be byte-identical to what the live parser
    describes. That is the whole point: a subcommand, a flag, a changed default
    or a reworded help string that nobody regenerated fails the gate here."""
    wanted = gen_cli_doc.build().encode("utf-8")
    path = gen_cli_doc.DOC_PATH
    check("1a cli.md exists", path.is_file(), f"path={path}")
    if not path.is_file():
        return
    current = path.read_bytes()
    check("1b cli.md matches the parser byte for byte", current == wanted,
          f"committed={len(current)}B generated={len(wanted)}B; "
          f"run 'uv run novel-translator/scripts/gen_cli_doc.py'")

    if current != wanted:
        diff = difflib.unified_diff(
            current.decode("utf-8", "replace").splitlines(),
            wanted.decode("utf-8", "replace").splitlines(),
            "committed", "generated", lineterm="", n=0)
        for line in list(diff)[:24]:
            print(f"    {line}")


def case_generation_is_deterministic() -> None:
    """Two runs, and two terminal widths, must produce the same bytes: the file
    is committed and byte-compared, so anything screen-dependent would make the
    gate machine-dependent. argparse wraps its usage line to COLUMNS."""
    first = gen_cli_doc.build()
    check("2a build() is stable across calls", first == gen_cli_doc.build())

    saved = os.environ.get("COLUMNS")
    try:
        os.environ["COLUMNS"] = "37"
        narrow = gen_cli_doc.build()
        os.environ["COLUMNS"] = "200"
        wide = gen_cli_doc.build()
    finally:
        if saved is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved
    check("2b build() ignores the terminal width", narrow == wide,
          "argparse wrapped its usage line to COLUMNS")
    check("2c build() at width 37 equals build() at the default width",
          narrow == first)


def case_every_registered_command_is_documented() -> None:
    """Cross-check the committed file against the live tree command by command,
    independently of the generator's own row building: a command that fell out
    of the walk would otherwise never be looked for."""
    commands = gen_cli_doc._commands(translate._build_parser())
    text = gen_cli_doc.DOC_PATH.read_text(encoding="utf-8")
    table = text.split("## Commands")[-1].split("## Detail")[0]

    missing_rows = [p for p, _x, _y, _z in commands if f"| `{p}` |" not in table]
    missing_sections = [p for p, _x, _y, _z in commands
                        if f"### `{p}`\n" not in text]
    check("3a every registered command has a row in the command table",
          not missing_rows, f"missing={missing_rows}")
    check("3b every registered command has its own section",
          not missing_sections, f"missing={missing_sections}")
    rows = [line for line in table.splitlines() if line.startswith("| `")]
    check("3c the command table holds exactly one row per registered command",
          len(rows) == len(commands),
          f"rows={len(rows)} commands={len(commands)}")

    nested = [p for p, _x, _y, d in commands if d > 1]
    check("3d nested commands are documented under their parent path",
          bool(nested) and all(f"`{p}`" in table for p in nested),
          f"nested={nested}")
    check("3e the tree has both top-level commands and nested actions",
          any(d == 1 for *_x, d in commands) and bool(nested),
          "a flat tree would not exercise the nested walk")


def case_a_new_subcommand_changes_the_document() -> None:
    """The derivation is real. Were the generator assembling rows from a list
    it kept itself, adding a subcommand would leave the document untouched and
    both this check and the shipped file would be satisfied by a stale
    artifact."""
    real, patched = _with_extra_subcommand(PROBE)
    try:
        translate._build_parser = patched
        probe = gen_cli_doc.build()
    finally:
        translate._build_parser = real
    committed = gen_cli_doc.DOC_PATH.read_text(encoding="utf-8")

    check("4a a new subcommand changes the generated document",
          probe != committed, "the document did not change")
    check("4b it reaches both the command table and its own section",
          f"| `{PROBE}` |" in probe and f"### `{PROBE}`\n" in probe)
    check("4c the parser is restored and the committed file still matches",
          translate._build_parser is real
          and gen_cli_doc.build().encode("utf-8")
          == gen_cli_doc.DOC_PATH.read_bytes())


def case_shared_parents_are_named() -> None:
    """Options declared once through parents=[...] are one action object shared
    by several parsers. The document has to say so, or `--no-verify` on a
    glossary action and `--no-verify` on `translate` read as one flag."""
    commands = gen_cli_doc._commands(translate._build_parser())
    shared = gen_cli_doc._shared_actions(commands)
    check("5a shared parent actions are found in the tree", bool(shared),
          f"shared={len(shared)}")
    names = sorted({a.option_strings[0] for a in shared if a.option_strings})
    section = gen_cli_doc.DOC_PATH.read_text(
        encoding="utf-8").split("## Shared options")[-1]
    check("5b every shared option is named in the shared-options section",
          all(f"**`{n}`**" in section for n in names), f"names={names}")
    check("5c the shared section records who accepts each option",
          "Accepted by" in section and "every command" in section)


def case_exit_codes_are_the_shipped_contract() -> None:
    """No exit code is derivable from an argparse tree, so this one table is
    hand-written -- but it still has to state what translate.main() returns,
    including the exit 3 the module docstring used to omit."""
    codes = {code for code, _m, _a in gen_cli_doc.EXIT_CODES}
    check("6a every exit code main() can return is documented",
          codes == {0, 1, 2, 3, 130}, f"codes={sorted(codes)}")
    section = gen_cli_doc.DOC_PATH.read_text(
        encoding="utf-8").split("## Exit codes")[-1]
    check("6b the exit 3 provider-failure exit is stated",
          "| 3 |" in section and "provider call failed" in section)
    check("6c the exit table points at the normative per-command matrix",
          "file-formats.md" in section)


def case_module_docstring_defers_to_the_generated_reference() -> None:
    """translate.py's docstring must not restate the command inventory: that
    hand-written list is the drift cli.md exists to delete. It keeps the exit
    codes, which argparse cannot supply."""
    doc = translate.__doc__ or ""
    check("7a the module docstring points at references/cli.md",
          "references/cli.md" in doc)
    check("7b the module docstring no longer carries a command inventory",
          "Subcommands:" not in doc,
          "the hand-written list is back")
    check("7c the module docstring still states the exit codes, including 3",
          "Exit codes:" in doc and "exit 3" in doc)


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    case_committed_file_matches_the_parser()
    case_generation_is_deterministic()
    case_every_registered_command_is_documented()
    case_a_new_subcommand_changes_the_document()
    case_shared_parents_are_named()
    case_exit_codes_are_the_shipped_contract()
    case_module_docstring_defers_to_the_generated_reference()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())