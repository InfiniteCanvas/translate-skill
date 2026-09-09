"""Tests for command-default resolution: config keys behind None parser defaults.

`review --batch-size` / `review --glossary` and `glossary search
--max-distance` all default to None at the parser; their commands resolve
the effective value at run time as CLI flag > config key > config.DEFAULTS
(review_batch_size / review_report_path / fuzzy_max_distance), with
validation after resolution (--batch-size must be >= 1, --max-distance
>= 0). review.write_report() takes the report filename from
cfg["review_report_path"] (default review-report.md) both for the write
location and for the "Next steps" lines, and cmd_review_fix reads the
report back through the same key via _load_config_lenient (None when
config.json is missing -> DEFAULTS).

Covered: the three parser defaults are None and explicit flags still
parse; review-glossary batch-size precedence (config value, CLI override,
DEFAULTS 40 fallback, config 0 -> CliError); search max-distance
resolution (config 0, CLI override, DEFAULTS 2 fallback, config -1 ->
CliError); write_report honors review_report_path for the file location
and the Next-steps interpolation; review fix (dry-run) reads the
config-named report, honors an explicit --glossary, falls back to
review-report.md without a config.json, and reports a missing report as
CliError.

Mechanics mirror the sibling suites: cmd_* entry points are called with
plain Namespaces and captured stdout (test_migrate style), the parser is
exercised through translate._build_parser().parse_args (test_parser_project
style), and review.review_glossary / review.write_report / glossary.search
are monkeypatched by attribute swap with orig/restore in try/finally (no
unittest.mock) so no model call or real report write can happen. Every
fixture lives in a TemporaryDirectory; the review-fix cases run --dry-run
(parse + list only, no subprocess). translate.py imports the whole lib
package (requests, ebooklib, pillow, pyyaml).

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_command_defaults.py
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

# scripts/ (and therefore lib/) lives at novel-translator/scripts relative
# to this file (CWD-independent); translate.py puts it on sys.path itself.
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import translate  # noqa: E402
from lib import config, glossary, review  # noqa: E402

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


@contextlib.contextmanager
def swapped(module, name, fake):
    """Swap module.<name> for `fake` (the CLI resolves these as module
    attributes at call time, so the swap takes effect); restore in finally."""
    orig = getattr(module, name)
    setattr(module, name, fake)
    try:
        yield
    finally:
        setattr(module, name, orig)


def run_cli(fn, ns: argparse.Namespace, project_dir: Path):
    """fn(ns, project_dir) with stdout captured; returns (code, output,
    exc) -- code is None when the call raised (the caller asserts on exc)."""
    buf = io.StringIO()
    code: int | None = None
    exc: Exception | None = None
    try:
        with contextlib.redirect_stdout(buf):
            code = fn(ns, project_dir)
    except Exception as caught:  # noqa: BLE001 - the caller asserts on it
        exc = caught
    return code, buf.getvalue(), exc


def parse(argv: list[str]) -> argparse.Namespace | None:
    """_build_parser().parse_args(argv); None when argparse rejects it
    (SystemExit) -- no SystemExit may escape a valid invocation."""
    try:
        return translate._build_parser().parse_args(argv)
    except SystemExit:
        return None


def make_project(root: Path, name: str, cfg_extra: dict | None = None,
                 entries: list | None = None) -> Path:
    """Minimal project: config.json {"providers": {}} + cfg_extra and a
    non-empty glossary.json (probe guard + review need one on disk)."""
    proj = root / name
    proj.mkdir()
    cfg = {"providers": {}}
    cfg.update(cfg_extra or {})
    (proj / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    glossary.save(proj, {"terms": list(entries or [
        {"source": "灵石", "translation": "spirit stone", "category": "item"},
    ])})
    return proj


# Minimal result dict cmd_review consumes (same keys as the real
# review.review_glossary return; empty findings keep the console loop quiet).
FAKE_RESULT = {"batches": 1, "findings": [], "entries": 1, "batch_errors": []}


def case_1_parser_defaults() -> None:
    """The three configurable flags default to None at the parser -- the
    commands' 'flag given' vs 'fall back to config' distinction -- and
    explicit flags still parse."""
    ns = parse(["review", "glossary"])
    check("1a parser: 'review glossary' -> batch_size None and glossary None",
          ns is not None and ns.batch_size is None and ns.glossary is None,
          f"ns={ns}")
    ns = parse(["glossary", "search", "foo"])
    check("1b parser: 'glossary search foo' -> max_distance None",
          ns is not None and ns.max_distance is None, f"ns={ns}")
    ns = parse(["review", "glossary", "--batch-size", "7"])
    check("1c parser: explicit --batch-size 7 parses",
          ns is not None and ns.batch_size == 7, f"ns={ns}")
    ns = parse(["review", "glossary", "--glossary", "custom.md"])
    check("1d parser: explicit --glossary custom.md parses",
          ns is not None and ns.glossary == "custom.md", f"ns={ns}")
    ns = parse(["glossary", "search", "foo", "--max-distance", "1"])
    check("1e parser: explicit --max-distance 1 parses",
          ns is not None and ns.max_distance == 1, f"ns={ns}")


def case_2_review_batch_size() -> None:
    """cmd_review resolves batch_size as CLI --batch-size > config
    review_batch_size > DEFAULTS (40); a resolved value < 1 -> CliError.
    review_glossary is swapped to capture what the command handed down;
    write_report is swapped so no real report lands anywhere."""
    calls: list[int] = []
    writes: list[dict] = []

    def fake_review(project_dir, cfg, batch_size):
        calls.append(batch_size)
        return dict(FAKE_RESULT)

    def fake_write_report(project_dir, **kwargs):
        writes.append(kwargs)
        return Path(project_dir) / "review-report.md"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg5 = make_project(root, "cfg5", {"review_batch_size": 5})
        cli7 = make_project(root, "cli7", {"review_batch_size": 5})
        nocfg = make_project(root, "nocfg")
        zero = make_project(root, "zero", {"review_batch_size": 0})
        ns = lambda bs: argparse.Namespace(  # noqa: E731 - tiny ns factory
            subject="glossary", fix=False, batch_size=bs, glossary=None)

        with swapped(review, "review_glossary", fake_review), \
                swapped(review, "write_report", fake_write_report):
            code, out, exc = run_cli(translate.cmd_review, ns(None), cfg5)
            check("2a review: config review_batch_size 5 used when no CLI flag",
                  exc is None and calls == [5] and "up to 5" in out,
                  f"calls={calls} exc={exc!r} out={out!r}")
            check("2b review: run completes (exit 0, ok line, one report write)",
                  code == 0 and "[ok] glossary review complete" in out
                  and len(writes) == 1, f"code={code} out={out!r}")

            _code, _out, exc = run_cli(translate.cmd_review, ns(7), cli7)
            check("2c review: CLI --batch-size 7 overrides config 5",
                  exc is None and calls == [5, 7], f"calls={calls} exc={exc!r}")

            _code, _out, exc = run_cli(translate.cmd_review, ns(None), nocfg)
            check("2d review: config without the key -> DEFAULTS 40",
                  exc is None and calls == [5, 7, 40]
                  and config.DEFAULTS["review_batch_size"] == 40,
                  f"calls={calls} exc={exc!r}")

            _code, _out, exc = run_cli(translate.cmd_review, ns(None), zero)
            check("2e review: config batch size 0 -> CliError before any model call",
                  isinstance(exc, translate.CliError)
                  and "positive integer" in str(exc) and calls == [5, 7, 40],
                  f"exc={exc!r} calls={calls}")


def case_3_search_distance() -> None:
    """_cmd_glossary_search resolves max_distance as CLI --max-distance >
    config fuzzy_max_distance > DEFAULTS (2); a resolved value < 0 ->
    CliError, and search() is never called on that path. glossary.search is
    swapped to capture the kwarg; empty matches -> exit 1 (grep convention)."""
    calls: list[int] = []

    def fake_search(g, term, max_distance=None):
        calls.append(max_distance)
        return {"matches": [], "retired": []}

    # Fixture entry shape from tests/test_glossary_search.py: the probe
    # 'grand-elder' sits 1 edit from 'Grand Elder'.
    entries = [{"source": "大长老", "translation": "Grand Elder"}]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        zero = make_project(root, "zero", {"fuzzy_max_distance": 0}, entries)
        nocfg = make_project(root, "nocfg", entries=entries)
        neg = make_project(root, "neg", {"fuzzy_max_distance": -1}, entries)
        ns = lambda md: argparse.Namespace(  # noqa: E731 - tiny ns factory
            max_distance=md, term="grand-elder")

        with swapped(glossary, "search", fake_search):
            code, out, exc = run_cli(
                translate._cmd_glossary_search, ns(None), zero)
            check("3a search: config fuzzy_max_distance 0 used when no CLI flag",
                  exc is None and calls == [0], f"calls={calls} exc={exc!r}")
            check("3b search: fake returned no matches -> exit 1, no-match line",
                  code == 1 and "no matches for 'grand-elder'" in out,
                  f"code={code} out={out!r}")

            _code, _out, exc = run_cli(
                translate._cmd_glossary_search, ns(1), zero)
            check("3c search: CLI --max-distance 1 overrides config 0",
                  exc is None and calls == [0, 1], f"calls={calls} exc={exc!r}")

            _code, _out, exc = run_cli(
                translate._cmd_glossary_search, ns(None), nocfg)
            check("3d search: config without the key -> DEFAULTS 2",
                  exc is None and calls == [0, 1, 2]
                  and config.DEFAULTS["fuzzy_max_distance"] == 2,
                  f"calls={calls} exc={exc!r}")

            _code, _out, exc = run_cli(
                translate._cmd_glossary_search, ns(None), neg)
            check("3e search: config -1 -> CliError (>= 0), search never called",
                  isinstance(exc, translate.CliError) and ">= 0" in str(exc)
                  and calls == [0, 1, 2], f"exc={exc!r} calls={calls}")


def case_4_report_path() -> None:
    """write_report writes to <project>/cfg['review_report_path'] (default
    review-report.md == REPORT_NAME) and interpolates the resolved name
    into the Next steps lines; the returned path is the written file."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        terms = [
            {"source": "灵根", "variants": [], "translation": "spirit root",
             "alt_translations": [], "definition": "Innate aptitude.",
             "category": "other", "origin": "seeded", "first_seen_chapter": None},
            {"source": "天雷宗", "variants": [], "translation": "river town",
             "alt_translations": [], "definition": "A sect.", "category": "org",
             "origin": "model", "first_seen_chapter": 1},
        ]
        findings = [
            {"source": "天雷宗", "kind": "mistranslation", "severity": "warn",
             "reason": "bad rendering of the sect name",
             "suggestion": "Heavenly Thunder Sect",
             "action": "Rename the sect's translation.", "origin": "model"},
        ]

        custom = review.write_report(
            root, findings=findings, terms=terms, applied=[], skipped=[],
            ran_fix=False, batches=1, batch_errors=[],
            cfg={"source_lang": "zh", "target_lang": "en",
                 "review_report_path": "custom-report.md"})
        check("4a report: custom key -> written at custom-report.md, path returned",
              custom == root / "custom-report.md" and custom.is_file(),
              f"path={custom}")
        text = custom.read_text(encoding="utf-8")
        check("4b report: Next steps name the custom report file",
              "review fix --glossary custom-report.md" in text,
              "custom name missing from Next steps")
        check("4c report: the default name never leaks into the custom report",
              "review-report.md" not in text, "stale default name present")

        default = review.write_report(
            root, findings=findings, terms=terms, applied=[], skipped=[],
            ran_fix=False, batches=1, batch_errors=[],
            cfg={"source_lang": "zh", "target_lang": "en"})
        check("4d report: no key -> written at review-report.md (REPORT_NAME)",
              default == root / review.REPORT_NAME
              and default.name == "review-report.md" and default.is_file(),
              f"path={default}")
        check("4e report: default report's Next steps name review-report.md",
              default.read_text(encoding="utf-8")
              .count("review fix --glossary review-report.md") == 1,
              "default name missing from Next steps")


# Minimal legacy-format report (test_fix_guard.py case_6 fixture verbatim):
# zero - Command: bullets -> fix.parse_report synthesizes the retire command
# from the finding block.
LEGACY_REPORT = (
    "# Glossary Review Report\n"
    "\n"
    "### [1] warn / mundane / 灵根\n"
    "\n"
    "- Reason: ordinary word, not a novel-specific term\n"
    "- Tier: model\n"
)


def case_5_fix_reads_config() -> None:
    """cmd_review_fix (dry-run: parse + list, no subprocess) reads the
    report named by config review_report_path; --glossary overrides it;
    no config.json -> DEFAULTS review-report.md; a missing report is a
    CliError. A decoy report at the default name proves WHICH file was
    read: the dry-run listing names the finding's source term."""
    def dry_run(proj: Path, glossary_arg: str | None):
        return run_cli(
            translate.cmd_review_fix,
            argparse.Namespace(fix=False, glossary=glossary_arg,
                               dry_run=True, exit_on_error=False),
            proj)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "proj"
        proj.mkdir()
        (proj / "config.json").write_text(json.dumps(
            {"providers": {}, "review_report_path": "custom-report.md"},
            ensure_ascii=False), encoding="utf-8")
        glossary.save(proj, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        (proj / "custom-report.md").write_text(
            LEGACY_REPORT, encoding="utf-8")
        # Decoy at the default name: only custom-report.md may be read
        # while the config points at the custom name.
        (proj / "review-report.md").write_text(
            LEGACY_REPORT.replace("灵根", "石头"), encoding="utf-8")

        code, out, exc = dry_run(proj, None)
        check("5a fix: config review_report_path -> the custom report is parsed",
              exc is None and code == 0 and "灵根" in out and "石头" not in out,
              f"code={code} exc={exc!r} out={out!r}")
        check("5b fix: dry-run lists the synthesized retire command + summary",
              "[1] mundane 灵根" in out
              and "glossary retire --source '灵根'" in out
              and "dry-run: 1 command(s), 0 finding(s) need a decision" in out,
              f"out={out!r}")

        code, out, exc = dry_run(proj, "review-report.md")
        check("5c fix: explicit --glossary overrides the config report path",
              exc is None and code == 0 and "石头" in out and "灵根" not in out,
              f"code={code} exc={exc!r} out={out!r}")

        _code, _out, exc = dry_run(proj, "missing-report.md")
        check("5d fix: missing report -> CliError (report not found)",
              isinstance(exc, translate.CliError) and "not found" in str(exc),
              f"exc={exc!r}")

    # No config.json at all: _load_config_lenient -> None -> the DEFAULTS
    # report name is read anyway (util/fix must run without a config).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bare = root / "bare"
        bare.mkdir()
        glossary.save(bare, {"terms": [
            {"source": "灵根", "variants": [], "translation": "spirit root"},
        ]})
        (bare / "review-report.md").write_text(LEGACY_REPORT, encoding="utf-8")
        code, out, exc = dry_run(bare, None)
        check("5e fix: no config.json -> DEFAULTS review-report.md still read",
              exc is None and code == 0 and "灵根" in out and "石头" not in out,
              f"code={code} exc={exc!r} out={out!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_parser_defaults()
    case_2_review_batch_size()
    case_3_search_distance()
    case_4_report_path()
    case_5_fix_reads_config()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
