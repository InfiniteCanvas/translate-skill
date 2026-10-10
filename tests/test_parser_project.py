"""Tests for translate.py's --project argument positions.

The parser registers --project under three dests: the top-level
project_global (before the subcommand), the shared `common` parent's
project (right after the subcommand verb), and each nested glossary
action's project_action (after the action verb). Separate dests are the
point: since Python 3.7 argparse copies each subparser's fresh namespace
over the main one unconditionally, a shared dest would let a nested
default None clobber a --project already consumed by the outer parser.
resolve_project_dir() (called by main()) resolves nested > action-level >
global > ".".

Every nested glossary action (replace/set/merge/retire/search) must parse
and resolve the directory in all three positions; top-level commands
(status) work both ways; the review/util paths still parse; migrate (the
per-version upgrade command) parses in both --project positions with its
--force/--dry-run flags round-tripping.

The abbreviation case pins allow_abbrev=False on every parser layer: an
abbreviated --proj token is rejected everywhere --project is registered,
so a hand-edited review-report command cannot smuggle a project override
past fix.run_commands' exact --project guard (exact spellings only).

Parsing only -- no project directories are created or touched. translate.py
imports the whole lib package (requests, ebooklib, pillow, pyyaml).

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_parser_project.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import argparse
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import translate

PASSED = 0
FAILED: list[str] = []

DIR = str(Path(tempfile.gettempdir()) / "nt-parser-project")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def parse(argv: list[str]) -> argparse.Namespace | None:
    """_build_parser().parse_args(argv); None when argparse rejects it
    (SystemExit) -- no SystemExit may escape a valid invocation."""
    try:
        return translate._build_parser().parse_args(argv)
    except SystemExit as exc:
        return None


def resolve_dir(ns: argparse.Namespace) -> Path:
    """The real resolution: translate.resolve_project_dir (main() calls
    this) -- nested glossary action level > subcommand level > before the
    subcommand > "."."""
    return translate.resolve_project_dir(ns)


NESTED_ARGS = {
    "replace": ["--source", "灵根", "--translation", "spiritual root"],
    "set": ["--source", "灵根", "--definition", "Innate aptitude."],
    "merge": ["--keep", "灵根", "--remove", "道基"],
    "retire": ["--source", "灵根"],
    "search": ["灵根"],
}


def check_positions(case: str, action: str) -> None:
    """All three --project positions parse and resolve to DIR."""
    argvs = {
        "global": ["--project", DIR, "glossary", action, *NESTED_ARGS[action]],
        "action": ["glossary", "--project", DIR, action, *NESTED_ARGS[action]],
        "nested": ["glossary", action, "--project", DIR, *NESTED_ARGS[action]],
    }
    for i, (style, argv) in enumerate(argvs.items()):
        ns = parse(argv)
        ok = ns is not None and str(resolve_dir(ns)) == DIR
        detail = "parse failed" if ns is None else f"resolved={resolve_dir(ns)!s}"
        check(f"{case}{chr(ord('a') + i)} {action} {style} position: parses and resolves DIR",
              ok, detail)


def case_1_replace() -> None:
    """replace: three positions resolve; required flags round-trip."""
    check_positions("1", "replace")
    ns = parse(["glossary", "replace", "--project", DIR,
                "--source", "灵根", "--translation", "spiritual root"])
    check("1d replace: parsed fields intact (source/translation/command/action)",
          ns is not None and ns.command == "glossary" and ns.action == "replace"
          and ns.source == "灵根" and ns.translation == "spiritual root",
          f"ns={ns}")


def case_2_set() -> None:
    """set: three positions resolve."""
    check_positions("2", "set")


def case_3_merge() -> None:
    """merge: three positions resolve."""
    check_positions("3", "merge")


def case_4_retire() -> None:
    """retire: three positions resolve."""
    check_positions("4", "retire")


def case_5_search() -> None:
    """search: three positions resolve; positional TERM survives."""
    check_positions("5", "search")
    ns = parse(["glossary", "search", "--project", DIR, "灵根"])
    check("5d search: positional term parsed after --project",
          ns is not None and ns.term == "灵根"
          and ns.command == "glossary" and ns.action == "search",
          f"ns={ns}")


def case_6_precedence() -> None:
    """Closest-to-the-action dest wins: nested beats action-level beats
    global -- main()'s documented resolution order."""
    ns = parse(["glossary", "--project", "D-outer", "search",
                "--project", "D-nested", "灵根"])
    check("6a precedence: nested --project beats the action-level one",
          ns is not None and str(resolve_dir(ns)).endswith("D-nested"),
          f"ns={ns}")
    ns = parse(["--project", "D-global", "glossary", "--project", "D-outer",
                "search", "灵根"])
    check("6b precedence: action-level --project beats the global one",
          ns is not None and str(resolve_dir(ns)).endswith("D-outer"),
          f"ns={ns}")


def case_7_default() -> None:
    """No --project anywhere -> resolution falls through to '.' (cwd)."""
    ns = parse(["glossary", "search", "灵根"])
    check("7 default: resolves to the cwd when no --project is given",
          ns is not None and resolve_dir(ns) == Path(".").resolve(),
          f"resolved={resolve_dir(ns) if ns else None}")


def case_8_top_level() -> None:
    """Top-level commands accept --project before the verb and after it."""
    ns = parse(["--project", DIR, "status"])
    check("8a top level: '--project DIR status' resolves DIR",
          ns is not None and str(resolve_dir(ns)) == DIR,
          f"ns={ns}")
    ns = parse(["status", "--project", DIR])
    check("8b top level: 'status --project DIR' resolves DIR",
          ns is not None and str(resolve_dir(ns)) == DIR,
          f"ns={ns}")


def case_9_review_util() -> None:
    """review and util paths still parse (fix subject, --glossary report
    path, util replace flags)."""
    ns = parse(["review", "fix", "--glossary", "review-report.md"])
    check("9a review fix: parses with the report path default argument",
          ns is not None and ns.subject == "fix"
          and ns.glossary == "review-report.md", f"ns={ns}")
    ns = parse(["review", "fix", "--project", DIR, "--glossary", "report.md"])
    check("9b review fix: --project after the subject resolves DIR",
          ns is not None and str(resolve_dir(ns)) == DIR, f"ns={ns}")
    ns = parse(["review", "glossary", "--fix", "--batch-size", "10"])
    check("9c review glossary: --fix and --batch-size still parse",
          ns is not None and ns.subject == "glossary" and ns.fix is True
          and ns.batch_size == 10, f"ns={ns}")
    ns = parse(["util", "replace", "--source", "spirit root",
                "--target", "spiritual root"])
    check("9d util replace: parses with --source/--target",
          ns is not None and ns.action == "replace"
          and ns.source == "spirit root" and ns.target == "spiritual root",
          f"ns={ns}")


def case_10_migrate() -> None:
    """migrate: --project resolves before and after the verb; --force and
    --dry-run round-trip to True; the parser binds cmd_migrate."""
    ns = parse(["--project", DIR, "migrate"])
    check("10a migrate: '--project DIR migrate' resolves DIR",
          ns is not None and str(resolve_dir(ns)) == DIR, f"ns={ns}")
    ns = parse(["migrate", "--project", DIR])
    check("10b migrate: 'migrate --project DIR' resolves DIR",
          ns is not None and str(resolve_dir(ns)) == DIR, f"ns={ns}")
    ns = parse(["migrate", "--project", DIR, "--force", "--dry-run"])
    check("10c migrate: --force/--dry-run round-trip to True, func == cmd_migrate",
          ns is not None and ns.force is True and ns.dry_run is True
          and ns.func is translate.cmd_migrate, f"ns={ns}")


def case_11_no_abbrev() -> None:
    """allow_abbrev=False on every parser layer: an abbreviated --proj is
    rejected at each --project registration point (top-level, subcommand
    parent, glossary verb parent, nested action), in both space and '='
    forms -- run_commands' exact --project guard cannot be bypassed by a
    hand-edited abbreviation."""
    argvs = {
        "a top level": ["--proj", DIR, "status"],
        "b top level = form": ["--proj=" + DIR, "status"],
        "c subcommand level": ["status", "--proj", DIR],
        "d glossary verb level": ["glossary", "--proj", DIR, "search", "灵根"],
        "e nested action level": ["glossary", "search", "--proj", DIR, "灵根"],
        "f nested action = form": ["glossary", "search", "--proj=" + DIR, "灵根"],
    }
    for name, argv in argvs.items():
        check(f"11{name}: abbreviated --proj rejected", parse(argv) is None,
              f"argv={argv}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_replace()
    case_2_set()
    case_3_merge()
    case_4_retire()
    case_5_search()
    case_6_precedence()
    case_7_default()
    case_8_top_level()
    case_9_review_util()
    case_10_migrate()
    case_11_no_abbrev()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
