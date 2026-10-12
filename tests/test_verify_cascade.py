#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
"""D3b: the cascade check that runs after a glossary change, and the FAITH
investigation that decides whether a rejection was glossary over-binding.

Covers three contracts the automation relies on. First, that a
glossary-changing command actually runs the drift check afterwards, scoped to
the chapters the edited term actually occurs in -- so it neither stays silent
nor pays for a whole-book scan on every keystroke-sized edit. Second, that the
check is advisory by default: a confirmation is reported and the command still
exits 0, so existing automation is not broken by a cascade being detected;
`--strict` is the opt-in that makes it non-zero, and `--no-verify` the opt-out
that skips the scan. Third, that the FAITH investigation's pre-filter is free
and decisive: a chapter that FAITH-rejects for reasons unrelated to the
glossary must reach no model at all, which is what keeps a default-on
investigation affordable.

The provider seam is stubbed at consensus.chat throughout, so no suite here
contacts a network.
"""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, consensus, config, fix, glossary, pipeline, project, verify
import translate

PASSED = 0
FAILED: list[str] = []

CFG = {
    "source_lang": "zh",
    "target_lang": "en",
    "contextual_glossary_cap": 200,
    "min_term_coverage": 0.25,
    "fuzzy_max_distance": 2,
    "auto_build_epub": False,
    "providers": {
        "translator": {"base_url": "http://fake/v1", "model": "m"},
        "consensus": {"base_url": "http://fake/v1", "model": "arb"},
    },
}

TERMS = {"terms": [
    {"source": "天道", "translation": "Heavenly Dao",
     "definition": "the way of heaven", "category": "technique"},
    {"source": "裴小丫", "translation": "Pei Xiaoya",
     "definition": "the heroine", "category": "person"},
]}

CHAPTER = "CHAPTER_0001.md"

SOURCE_BODY = (
    "天道无常，裴小丫望向远山。\n"
    "她想起师父说过的话。\n"
    "天道无常，草木亦然。\n"
    "风穿过林间。\n"
)

FAITHFUL_BODY = (
    "Heavenly Dao is unconstant, and Pei Xiaoya looked toward the far mountains.\n"
    "She recalled what her master had said.\n"
    "Heavenly Dao is unconstant, and so is grass and tree.\n"
    "The wind passed through the woods.\n"
)

UNDERSUPPLIED_BODY = (
    "She looked toward the far mountains.\n"
    "She recalled what her master had said.\n"
    "Grass and tree are unconstant too.\n"
    "The wind passed through the woods.\n"
)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_project(root: Path, translated_body: str) -> Path:
    proj = root / "proj"
    write(proj / "config.json", json.dumps(CFG, ensure_ascii=False, indent=2) + "\n")
    write(proj / "glossary.json",
          json.dumps(TERMS, ensure_ascii=False, indent=2) + "\n")
    write(proj / "source" / CHAPTER, SOURCE_BODY)
    write(proj / "translated" / CHAPTER, translated_body)
    write(proj / "chapters.json", json.dumps(
        [{"file": CHAPTER, "number": 1, "order": 0, "status": "translated"}],
        indent=2) + "\n")
    return proj


class Stub:
    """Swaps consensus.chat so no provider is contacted and every call is
    recorded as (job, chapter)."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, str | None]] = []

    def __enter__(self):
        self.orig = consensus.chat
        consensus.chat = self.chat
        return self

    def __exit__(self, *exc):
        consensus.chat = self.orig
        return False

    def chat(self, project_dir, cfg, job, prompt, json_schema=None,
             max_tokens=None, chapter=None):
        self.calls.append((job, chapter))
        return self.responder(prompt, chapter)


def confirms(source: str = "裴小丫", chapter: str = CHAPTER,
             mistranslates: bool = True):
    def responder(prompt, ch):
        return json.dumps({"verdicts": [
            {"source": source, "chapter": chapter, "mistranslates": mistranslates,
             "reason": "a different sense in this chapter"}]})
    return responder


def clears_all():
    return lambda prompt, ch: json.dumps({"verdicts": []})


def run_cli(argv: list[str], proj: Path, responder) -> tuple[int, str, Stub]:
    ns = translate._build_parser().parse_args(argv + ["--project", str(proj)])
    stub = Stub(responder)
    out, err = io.StringIO(), io.StringIO()
    with stub, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = translate.main(argv + ["--project", str(proj)])
    return code, out.getvalue() + err.getvalue(), stub


def case_a_glossary_edit_triggers_the_cascade_check() -> None:
    """Every glossary-changing verb runs the check afterwards, and the
    default is advisory: a confirmed cascade still exits 0."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "set", "--source", "裴小丫", "--definition", "a new sense"],
            proj, confirms())
        check("1a set: cascade check ran after the edit",
              "cascade check after glossary set" in out, out)
        check("1b set: it reported a real stage-2 confirmation",
              "1 term/chapter finding(s) confirmed" in out, out)
        check("1c set: advisory default keeps the exit code at 0",
              code == 0, f"code={code}")
        check("1d set: the contamination is reported to the human",
              "glossary contamination" in out, out)
        check("1e set: the model call was made on the verify job",
              [job for job, _ch in stub.calls] == ["verify"],
              f"calls={stub.calls}")

        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "replace", "--source", "天道", "--translation", "Dao"],
            proj, confirms())
        check("1f replace: cascade check ran after the edit",
              "cascade check after glossary replace" in out, out)
        check("1g replace: advisory default keeps the exit code at 0",
              code == 0, f"code={code}")

        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "retire", "--source", "裴小丫"], proj, confirms())
        check("1h retire: cascade check ran after the edit",
              "cascade check after glossary retire" in out, out)
        check("1i retire: advisory default keeps the exit code at 0",
              code == 0, f"code={code}")

        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "merge", "--keep", "天道", "--remove", "裴小丫"],
            proj, confirms())
        check("1j merge: cascade check ran after the edit",
              "cascade check after glossary merge" in out, out)
        check("1k merge: advisory default keeps the exit code at 0",
              code == 0, f"code={code}")


def case_strict_and_no_verify_are_the_two_switches() -> None:
    """--strict propagates the cascade into the exit code; --no-verify skips
    the scan entirely, making no model call at all."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "set", "--source", "裴小丫", "--definition", "x",
             "--strict"], proj, confirms())
        check("2a strict: a confirmed cascade exits non-zero",
              code == 1, f"code={code}")
        check("2b strict: the check still ran and reported",
              "cascade check after glossary set" in out, out)

        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "set", "--source", "裴小丫", "--definition", "x",
             "--no-verify"], proj, confirms())
        check("2c no-verify: no cascade check ran",
              "cascade check" not in out, out)
        check("2d no-verify: no model call was made",
              stub.calls == [], f"calls={stub.calls}")
        check("2e no-verify: the edit itself still applied",
              glossary.find(glossary.load(proj), "裴小丫")["definition"] == "x")

        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "set", "--source", "裴小丫", "--definition", "x",
             "--strict", "--no-verify"], proj, confirms())
        check("2f no-verify wins over strict: exit stays 0 with no calls",
              code == 0 and stub.calls == [], f"code={code} calls={stub.calls}")


def case_the_check_is_scoped_to_affected_chapters() -> None:
    """Only chapters the edited term actually occurs in are scanned: the
    check cannot pay for chapters the edit could not have reached."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "proj"
        write(proj / "config.json", json.dumps(CFG, ensure_ascii=False, indent=2) + "\n")
        write(proj / "glossary.json",
              json.dumps(TERMS, ensure_ascii=False, indent=2) + "\n")
        write(proj / "source" / CHAPTER, SOURCE_BODY)
        write(proj / "translated" / CHAPTER, UNDERSUPPLIED_BODY)
        other = "CHAPTER_0002.md"
        write(proj / "source" / other, "山间有风。\n草木无声。\n路很长。\n天亮了。\n")
        write(proj / "translated" / other, UNDERSUPPLIED_BODY)
        write(proj / "chapters.json", json.dumps([
            {"file": CHAPTER, "number": 1, "order": 0, "status": "translated"},
            {"file": other, "number": 2, "order": 1, "status": "translated"},
        ], indent=2) + "\n")

        code, out, stub = run_cli(
            ["glossary", "set", "--source", "裴小丫", "--definition", "x"],
            proj, confirms())
        check("3a scope: only the chapter carrying the term is scanned",
              "1 chapter(s) carry the edited term" in out, out)
        check("3b scope: the untouched chapter was never judged",
              all(ch == "CHAPTER_0001" for _job, ch in stub.calls),
              f"calls={stub.calls}")


def case_a_term_in_no_chapter_costs_nothing() -> None:
    """An edited term that occurs nowhere has no subject: the check reports
    that and makes no call, rather than scanning the book for nothing."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        glossary.save(proj, {"terms": TERMS["terms"] + [
            {"source": "太虚", "translation": "the void",
             "definition": "an unrelated term", "category": "technique"}]})
        code, out, stub = run_cli(
            ["glossary", "set", "--source", "太虚", "--definition", "x"],
            proj, confirms())
        check("4a no-chapter term: nothing was scanned",
              "occurs in no chapter" in out, out)
        check("4b no-chapter term: no model call was made",
              stub.calls == [], f"calls={stub.calls}")
        check("4c no-chapter term: exit stays 0 even with --strict",
              run_cli(["glossary", "set", "--source", "太虚", "--definition", "y",
                       "--strict"], proj, confirms())[0] == 0)


def case_noop_and_dry_run_never_trigger() -> None:
    """Nothing changed, so nothing can have cascaded: the no-op and dry-run
    paths must not spend a call."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        code, out, stub = run_cli(
            ["glossary", "set", "--source", "裴小丫", "--definition",
             glossary.find(glossary.load(proj), "裴小丫")["definition"]],
            proj, confirms())
        check("5a set no-op: no cascade check ran",
              "cascade check" not in out, out)
        check("5b set no-op: no model call was made",
              stub.calls == [], f"calls={stub.calls}")

        code, out, stub = run_cli(
            ["glossary", "replace", "--source", "天道", "--translation", "Dao",
             "--dry-run"], proj, confirms())
        check("5c replace --dry-run: no cascade check ran",
              "cascade check" not in out, out)
        check("5d replace --dry-run: no model call was made",
              stub.calls == [], f"calls={stub.calls}")

        code, out, stub = run_cli(
            ["glossary", "replace", "--source", "天道", "--translation",
             "Heavenly Dao"], proj, confirms())
        check("5e replace no-op: no cascade check ran",
              "cascade check" not in out, out)


def case_faith_investigation_is_free_before_any_call() -> None:
    """The pre-filter decides on stage 1 alone. A chapter FAITH-rejected for
    reasons unrelated to the glossary must reach no model at all."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        cfg = json.loads((proj / "config.json").read_text(encoding="utf-8"))
        g = glossary.load(proj)
        source_lines = (proj / "source" / CHAPTER).read_text(
            encoding="utf-8").splitlines()

        with Stub(clears_all()) as stub:
            result = verify.investigate_faith(
                proj, cfg, g, CHAPTER, source_lines,
                FAITHFUL_BODY.splitlines())
            check("6a faithful rendering: pre-filter declined the chapter",
                  result["considered"] is False, str(result))
            check("6b faithful rendering: no model call was made",
                  stub.calls == [], f"calls={stub.calls}")

        unfaithful_but_on_term = [
            "Heavenly Dao is unconstant, and Pei Xiaoya looked toward the far mountains.",
            "She recalled what her master had said, and then she flew to the moon.",
            "Heavenly Dao is unconstant, and so is grass and tree, which never existed.",
            "The wind passed through the woods.",
        ]
        with Stub(clears_all()) as stub:
            result = verify.investigate_faith(
                proj, cfg, g, CHAPTER, source_lines, unfaithful_but_on_term)
            check("6c non-glossary rejection: pre-filter declined the chapter",
                  result["considered"] is False, str(result))
            check("6d non-glossary rejection: no model call was made",
                  stub.calls == [], f"calls={stub.calls}")

        with Stub(clears_all()) as stub:
            result = verify.investigate_faith(
                proj, cfg, g, CHAPTER, source_lines,
                UNDERSUPPLIED_BODY.splitlines())
            check("6e over-binding case: the chapter reached the model",
                  result["considered"] is True, str(result))
            check("6f over-binding case: the declined term is the candidate",
                  result["candidates"] == ["裴小丫"], str(result["candidates"]))
            check("6g over-binding case: exactly one call was made",
                  len(stub.calls) == 1, f"calls={stub.calls}")


def case_faith_investigation_runs_the_shared_stage_two() -> None:
    """The investigation must answer with the same stage-2 contract a book
    scan does, since it is the same question asked about one chapter."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        cfg = json.loads((proj / "config.json").read_text(encoding="utf-8"))
        g = glossary.load(proj)
        source_lines = (proj / "source" / CHAPTER).read_text(
            encoding="utf-8").splitlines()

        with Stub(confirms()) as stub:
            result = verify.investigate_faith(
                proj, cfg, g, CHAPTER, source_lines,
                UNDERSUPPLIED_BODY.splitlines())
            check("7a a confirmation is returned as a finding",
                  result["findings"] and result["findings"][0]["mistranslates"],
                  str(result["findings"]))
            check("7b the confirmed entry is reported as contaminated",
                  [t["source"] for t in result["contaminated_terms"]] == ["裴小丫"],
                  str(result["contaminated_terms"]))
            check("7c it ran on the verify job's own template",
                  [job for job, _ch in stub.calls] == ["verify"],
                  f"calls={stub.calls}")

        with Stub(clears_all()) as stub:
            result = verify.investigate_faith(
                proj, cfg, g, CHAPTER, source_lines,
                UNDERSUPPLIED_BODY.splitlines())
            check("7d an all-clear produces no finding",
                  not any(f["mistranslates"] for f in result["findings"]),
                  str(result["findings"]))

        unpairable = UNDERSUPPLIED_BODY.splitlines()[:3]
        with Stub(clears_all()) as stub:
            result = verify.investigate_faith(
                proj, cfg, g, CHAPTER, source_lines, unpairable)
            check("7e unpairable lines are reported, not guessed",
                  result["errors"] and result["considered"] is False,
                  str(result))
            check("7f unpairable lines make no model call",
                  stub.calls == [], f"calls={stub.calls}")


def case_faith_hook_is_wired_and_suppressible() -> None:
    """The pipeline calls the investigation on a FAITH rejection unless the
    caller opted out, and the plumbing that carries that choice exists."""
    import inspect
    run_src = inspect.signature(pipeline.run_range)
    chapter_src = inspect.signature(pipeline.run_chapter)
    check("8a run_range carries verify_on_faith, defaulting on",
          "verify_on_faith" in run_src.parameters
          and run_src.parameters["verify_on_faith"].default is True)
    check("8b run_chapter carries verify_on_faith, defaulting on",
          "verify_on_faith" in chapter_src.parameters
          and chapter_src.parameters["verify_on_faith"].default is True)
    check("8c the rejection handler exists and is named for its job",
          hasattr(pipeline, "_investigate_glossary_rejection"))

    parser = translate._build_parser()
    ns = parser.parse_args(["translate", "--chapters", "1", "--no-verify"])
    check("8d translate exposes --no-verify for the investigation",
          ns.no_verify is True)
    ns = parser.parse_args(["translate", "--chapters", "1"])
    check("8e translate leaves the investigation on by default",
          ns.no_verify is False)
    ns = parser.parse_args(["retry", "--failed", "--no-verify"])
    check("8f retry exposes --no-verify for the investigation",
          ns.no_verify is True)


def case_review_fix_checks_once_for_the_batch() -> None:
    """A batch of N commands must not pay N cascade scans: the children are
    suppressed and one check runs for the whole batch."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "proj"
        write(proj / "config.json", json.dumps(CFG, ensure_ascii=False, indent=2) + "\n")
        write(proj / "glossary.json",
              json.dumps(TERMS, ensure_ascii=False, indent=2) + "\n")
        write(proj / "chapters.json", "[]\n")
        write(proj / "source" / CHAPTER, SOURCE_BODY)
        write(proj / "translated" / CHAPTER, UNDERSUPPLIED_BODY)
        write(proj / "review-report.md",
              "- Command: glossary set --source '裴小丫' --definition 'first'\n"
              "- Command: glossary set --source '天道' --definition 'second'\n")

        prepared = fix._prepare_argv(["glossary", "replace", "--source", "天道",
                                      "--translation", "Dao"])
        check("9a children get --no-verify so the batch checks once",
              "--no-verify" in prepared, str(prepared))
        check("9b children keep the existing --no-build contract",
              "--no-build" in prepared, str(prepared))
        check("9c an explicit --no-verify is not doubled",
              fix._prepare_argv(["glossary", "replace", "--no-verify",
                                 "--source", "天道", "--translation", "D"]
                                ).count("--no-verify") == 1)
        check("9d a non-glossary verb is left alone",
              "--no-verify" not in fix._prepare_argv(["glossary", "search", "x"]))

        check("9e the batch's changed terms are recovered for one scan",
              fix.changed_sources([
                  ["glossary", "set", "--no-verify", "--source", "裴小丫",
                   "--definition", "first"],
                  ["glossary", "set", "--no-verify", "--source", "天道",
                   "--definition", "second"],
              ]) == ["裴小丫", "天道"])
        check("9f a merge contributes both of its terms",
              fix.changed_sources([
                  ["glossary", "merge", "--no-verify", "--keep", "天道",
                   "--remove", "裴小丫"],
              ]) == ["天道", "裴小丫"])
        check("9g a retired term is included: its chapters changed balance too",
              fix.changed_sources([
                  ["glossary", "retire", "--no-verify", "--source", "裴小丫"],
              ]) == ["裴小丫"])

        specs, _count = fix.parse_report(proj / "review-report.md")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = fix.run_commands(proj, SCRIPTS / "translate.py", specs)
        check("9h both commands applied, neither failed",
              result["applied"] == 2 and result["failed"] == 0, str(result))
        check("9i the applied argv is returned for the batch's cascade check",
              len(result["argv"]) == 2, str(result["argv"]))


def case_verify_project_scope_is_optional_and_back_compatible() -> None:
    """The scope argument must be additive: omitted, the scan is whole-book
    exactly as D3a shipped it."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        cfg = json.loads((proj / "config.json").read_text(encoding="utf-8"))
        with Stub(clears_all()) as stub:
            whole = verify.verify_project(proj, cfg)
            check("10a no scope: every chapter is examined",
                  whole["examined"] == 1, str(whole["examined"]))
            calls_whole = len(stub.calls)
        with Stub(clears_all()) as stub:
            empty = verify.verify_project(proj, cfg, chapters=set())
            check("10b an empty scope examines nothing",
                  empty["examined"] == 0 and empty["calls"] == 0, str(empty))
            check("10c an empty scope makes no model call",
                  stub.calls == [], f"calls={stub.calls}")
        with Stub(clears_all()) as stub:
            scoped = verify.verify_project(proj, cfg, chapters={CHAPTER})
            check("10d a matching scope examines that chapter",
                  scoped["examined"] == 1, str(scoped["examined"]))
            check("10e a matching scope costs what the unscoped scan costs",
                  len(stub.calls) == calls_whole, f"{stub.calls} vs {calls_whole}")


def case_flags_are_rejected_where_they_do_not_apply() -> None:
    """--strict and --no-verify only mean something for the mutating
    subjects, so naming them elsewhere is a usage error, not a silent no-op."""
    parser = translate._build_parser()
    for subject in ("glossary", "notes"):
        ns = parser.parse_args(["review", subject, "--strict"])
        try:
            translate.cmd_review(ns, Path("."))
            check(f"11a review {subject} --strict is a usage error", False,
                  "no CliError")
        except translate.CliError:
            check(f"11a review {subject} --strict is a usage error", True)
        except Exception as exc:
            check(f"11a review {subject} --strict is a usage error", False,
                  f"{type(exc).__name__}: {exc}")


def case_cascade_never_writes_or_flips() -> None:
    """The cascade check reports; it does not act. Only the standalone
    `verify` command flips a chapter, so an automatic check cannot schedule a
    re-translation nobody asked for."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        manifest_before = (proj / "chapters.json").read_bytes()
        glossary_before = (proj / "glossary.json").read_bytes()
        run_cli(["glossary", "set", "--source", "裴小丫", "--definition", "x"],
                proj, confirms())
        check("12a the cascade check did not touch the manifest",
              (proj / "chapters.json").read_bytes() == manifest_before)
        check("12b the cascade check did not write the glossary",
              (proj / "glossary.json").read_bytes() != glossary_before
              and glossary.find(glossary.load(proj), "裴小丫")["definition"] == "x")
        statuses = [e["status"] for e in project.load_manifest(proj)]
        check("12c the chapter status was not flipped by the cascade check",
              statuses == ["translated"], str(statuses))
        check("12d no verify.jsonl report was written by the cascade check",
              not (proj / "logs" / "verify.jsonl").is_file())


def case_cascade_is_logged_as_its_own_event() -> None:
    """The automatic check is a run the maintainer must be able to find in
    the trace log, distinct from a manual `verify` run."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), UNDERSUPPLIED_BODY)
        run_cli(["glossary", "set", "--source", "裴小丫", "--definition", "x"],
                proj, confirms())
        events = []
        for path in sorted((proj / "logs").rglob("*.jsonl")):
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        cascades = [e for e in events if e.get("event") == "verify_cascade"]
        check("13a the cascade check wrote a verify_cascade event",
              len(cascades) == 1, str([e.get("event") for e in events]))
        if cascades:
            check("13b the event names the origin and the terms",
                  cascades[0].get("origin") == "glossary set"
                  and cascades[0].get("terms") == ["裴小丫"], str(cascades[0]))
            check("13c the event records what was confirmed",
                  cascades[0].get("confirmed_chapters") == [CHAPTER],
                  str(cascades[0]))


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    case_a_glossary_edit_triggers_the_cascade_check()
    case_strict_and_no_verify_are_the_two_switches()
    case_the_check_is_scoped_to_affected_chapters()
    case_a_term_in_no_chapter_costs_nothing()
    case_noop_and_dry_run_never_trigger()
    case_faith_investigation_is_free_before_any_call()
    case_faith_investigation_runs_the_shared_stage_two()
    case_faith_hook_is_wired_and_suppressible()
    case_review_fix_checks_once_for_the_batch()
    case_verify_project_scope_is_optional_and_back_compatible()
    case_flags_are_rejected_where_they_do_not_apply()
    case_cascade_never_writes_or_flips()
    case_cascade_is_logged_as_its_own_event()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())