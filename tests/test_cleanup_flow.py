"""Tests for the glossary cleanup judgment flow end to end (run_chapter).

The flow under test (pipeline._cleanup_drift_signals +
_apply_pending_cleanup, driven by the BALANCE stage): a glossary term the
translation NEVER renders canonically (src >= 2, tgt == 0) becomes a
balance drift signal; with glossary_auto_cleanup on (the default), one
glossary-job call over glossary_cleanup.md judges each flagged term; terms
the judgment calls mundane are NOT retired on the spot -- the decisions are
deferred (pending_cleanup) and applied only after the FAITH gate accepts
the attempt, at the head of GLOSSARY_EXPAND, via glossary.retire() (which
records the canonical source under "retired" so seed()/expansion can never
re-add it). One run_chapter with a monkeypatched pipeline._chat (the
suite's established convention; the real mock_server is manual-kit only)
covers the whole chain: canned English translations make the target-side
count zero (the drift), the fake cleanup verdict retires the term, and the
deferred retirement lands only after the SUCCESS faithfulness verdict.

Covered: the run completes with outcome "translated"; the exact console
line `[CHAPTER_0001] [glossary] retired mundane term '灵石' (mundane mock
term)` prints between the FAITH and GLOSSARY_EXPAND init lines; glossary.json
after the run is exactly {"terms": [], "retired": ["灵石"]} (whole-file
equality -- the deferred retire and the GLOSSARY_EXPAND save must agree),
find() no longer resolves 灵石, and retired_sources() == {"灵石"}; the
per-invocation trace (the tier-1 orchestration timeline at
logs/run-*.jsonl, filtered by event type, never by
line index -- the run also writes attempt and balance_advisory events)
contains exactly one glossary_cleanup event with chapter
"CHAPTER_0001.md", removed == [{"source": "灵石", "reason": "mundane mock
term"}], kept == []; and the manifest marks the chapter translated.

Three further run_chapter cases share the harness: the truncating-chunk
escalation (a provider max_tokens far below translate_max_output_tokens --
the corrective retry after a cut mid-JSON response goes out at max_out,
never at the smaller provider cap, and the below-provider-cap config warn
prints exactly once across a multi-chapter run; pipeline._escalated_cap is
additionally checked directly); the zero-line source guard (a source
chapter with no body content is marked needs-review with the warn line and
NO LLM interaction, instead of assembling a content-free translation); and
the post-assemble manifest save guard (a PermissionError on that save
prints the warn, the outcome stays "translated", and the on-disk manifest
keeps the in-progress status written at chapter start).

The fake _chat's branch order is load-bearing (mock_server.py prompt
sniffing): the bare word "notes" appears in translation.md and recap.md,
so the notes check must use the QUOTED forms; "Flagged Terms" is the
glossary_cleanup.md marker; the numbered translate mirror (echo the
"### Source Data" array, test_chunking.py's convention) must not fire for
the recap prompt (recap.md has no Source Data section), and the recap
answer must be non-blank because story.py rejects a blank recap.

A final unit case pins pipeline._notes_report_line's richer shapes (the
flow case above only shows the zero-kept line): the category
parenthetical in NOTE_CATEGORIES order (an unknown category counts under
"other"), the fixed low_threshold -> overflow -> invalid drop order with
its low-confidence / overflow / invalid labels, and the
notes/<stem>.dropped.json pointer -- whose target tn.save_dropped really
writes, with the same drop reasons.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_cleanup_flow.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file
# (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config, glossary, logger, pipeline, project, tn  # noqa: E402

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


def capture(fn, *args, **kwargs):
    """fn(*args, **kwargs) with stdout captured; returns (result, output,
    exc) -- result is None when the call raised."""
    buf = io.StringIO()
    result = None
    exc: Exception | None = None
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except Exception as caught:  # noqa: BLE001 - the caller asserts on it
        exc = caught
    return result, buf.getvalue(), exc


def fake_chat(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None,
              chapter=None):
    """pipeline._chat replacement (mock_server.py prompt sniffing). Branch
    conditions and their order are load-bearing: the QUOTED '"notes"' /
    '"note"' forms must precede everything else that could match a notes
    schema prompt (translation.md and recap.md contain the bare word
    "notes"), "Flagged Terms" is glossary_cleanup.md's marker (the regex
    only extracts the flagged sources from its rendered term list), and
    '"terms"' is the GLOSSARY_EXPAND schema key. The translate mirror
    echoes the numbered "### Source Data" array (test_chunking.py's
    convention) with canned ENGLISH lines -- the missing canonical
    rendering is what makes the target-side count zero and trips the drift
    signal. The recap answer is non-blank because story.py rejects blank."""

    # FAITH (faithfulness.md asks for a "verdict")
    if "verdict" in prompt:
        return json.dumps({"verdict": "SUCCESS", "reasons": []},
                          ensure_ascii=False)
    # TN_GENERATE (tn_generate.md's schema) -- QUOTED forms only
    if '"notes"' in prompt or '"note"' in prompt:
        return json.dumps({"notes": []})
    # glossary cleanup judgment (glossary_cleanup.md's [Flagged Terms])
    if "Flagged Terms" in prompt:
        found = re.findall(r"- (\S+) translates to", prompt)
        return json.dumps(
            {"decisions": [{"source": t, "keep": False,
                            "reason": "mundane mock term"} for t in found]},
            ensure_ascii=False)
    # GLOSSARY_EXPAND (glossary_expand.md's schema)
    if '"terms"' in prompt:
        return json.dumps({"terms": []})
    # TRANSLATE: mirror the numbered [{"i", "t"}] source array
    if "### Source Data" in prompt:
        arr = json.loads(prompt.rsplit("### Source Data", 1)[1].strip())
        return json.dumps(
            {"title": "Mock Chapter Title",
             "lines": [{"i": x["i"],
                        "t": "Translated line %d." % x["i"] if x["t"].strip() else ""}
                       for x in arr]},
            ensure_ascii=False)
    # recap generation (recap.md's schema key; no Source Data section)
    if '"recap"' in prompt:
        return json.dumps({"recap": "Mock recap of the story so far."})
    raise AssertionError(f"unroutable prompt (job={job}): {prompt[:120]!r}")


def make_project(root: Path, name: str) -> Path:
    """Pipeline-shaped fixture (test_glossary_count's make_gate_project
    shape): one three-line source chapter (灵石 occurs 2x -> the drift
    signal's src >= 2), an accurate chapters.json manifest, config.json
    {"providers": {}} (one provider block serves every job via inheritance;
    glossary_auto_cleanup is on by default), a one-entry glossary written
    via glossary.save with NO other top-level keys -- category "item" is
    not guide-only and the multi-word translation avoids any fuzzy target
    match -- and the draft/ + translated/ + notes/ dirs pre-created
    (atomic_write_text/write_chapter never mkdir)."""
    proj = root / name
    proj.mkdir()
    for d in ("source", "translated", "draft", "notes"):
        (proj / d).mkdir()
    # No frontmatter, no trailing newline: read_chapter returns the text
    # verbatim, so the body is exactly the three lines.
    (proj / "source" / "CHAPTER_0001.md").write_text(
        "他捡起一块灵石。\n灵石发光了。\n第三行。", encoding="utf-8", newline="\n")
    (proj / "chapters.json").write_text(
        json.dumps([{"file": "CHAPTER_0001.md", "number": 1, "suffix": "",
                     "order": 0, "status": "pending"}],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    glossary.save(proj, {"terms": [{
        "source": "灵石", "variants": [], "translation": "spirit stone",
        "alt_translations": [], "category": "item",
        "definition": "A spirit stone.",
    }]})
    (proj / "config.json").write_text(
        json.dumps({"providers": {}}, indent=2) + "\n", encoding="utf-8")
    return proj


RETIRED_LINE = (
    "[CHAPTER_0001] [glossary] retired mundane term '灵石' (mundane mock term)\n"
)

# The run's full console output is deterministic: eight lines, the retired
# line landing between the FAITH and GLOSSARY_EXPAND init lines.
EXPECTED_OUTPUT = (
    "[CHAPTER_0001] [init] TRANSLATE (attempt 1)\n"
    "[CHAPTER_0001] [init] FAITH\n"
    + RETIRED_LINE +
    "[CHAPTER_0001] [init] GLOSSARY_EXPAND\n"
    "[CHAPTER_0001] [init] TN_GENERATE\n"
    "[CHAPTER_0001] [ok] notes: 0 kept\n"
    "[CHAPTER_0001] [ok] translated -> CHAPTER_0001.md "
    "(title: Mock Chapter Title, notes: 0)\n"
    "[CHAPTER_0001] [init] recap\n"
)


def case_cleanup_flow() -> None:
    """One full run_chapter: drift signal -> cleanup judgment -> deferred
    retirement -> retired/never-re-added glossary state + trace event."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = config.load_config(proj)
        # _run_path is process-global and pins the FIRST project that logs;
        # reset it so this sandbox's logs/ owns the run's trace.
        logger._run_path = None
        orig = pipeline._chat
        pipeline._chat = fake_chat
        try:
            outcome, out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md", cfg)
        finally:
            pipeline._chat = orig

        # ---- the run itself
        check("1a run: completes without error, outcome 'translated'",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        check("1b run: the exact retired-mundane-term console line",
              RETIRED_LINE in out, f"out={out!r}")
        check("1c run: the translated summary line printed",
              "[CHAPTER_0001] [ok] translated ->" in out, f"out={out!r}")
        check("1d run: full deterministic console output (8 lines, retired "
              "line after FAITH init, before GLOSSARY_EXPAND init)",
              out == EXPECTED_OUTPUT, f"out={out!r}")

        # ---- glossary state after the run
        after = glossary.load(proj)
        check("2a glossary: whole file is exactly empty terms + retired 灵石",
              after == {"terms": [], "retired": ["灵石"]}, f"after={after!r}")
        check("2b glossary: find() no longer resolves 灵石",
              glossary.find(after, "灵石") is None, f"terms={after.get('terms')!r}")
        check("2c glossary: retired_sources() == {'灵石'}",
              glossary.retired_sources(after) == {"灵石"},
              f"retired_sources={glossary.retired_sources(after)!r}")

        # ---- trace (filter by event type; never by line index)
        # glossary_cleanup / attempt / balance_advisory are tier-1
        # orchestration events, so they live in the tier-1 root bucket.
        events = []
        for log_path in sorted((proj / "logs").glob("run-*.jsonl")):
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
        cleanups = [e for e in events if e.get("event") == "glossary_cleanup"]
        check("3a trace: the run also wrote attempt/balance_advisory events "
              "(the filter above is not vacuous)",
              any(e.get("event") == "attempt" for e in events)
              and any(e.get("event") == "balance_advisory" for e in events),
              f"events={[e.get('event') for e in events]}")
        check("3b trace: exactly one glossary_cleanup event",
              len(cleanups) == 1, f"cleanups={cleanups!r}")
        check("3c trace: chapter/removed/kept exactly as _apply_pending_cleanup "
              "writes them",
              len(cleanups) == 1
              and cleanups[0].get("chapter") == "CHAPTER_0001.md"
              and cleanups[0].get("removed")
              == [{"source": "灵石", "reason": "mundane mock term"}]
              and cleanups[0].get("kept") == [],
              f"event={cleanups[0] if cleanups else None!r}")

        # ---- run outcome effects
        manifest = project.load_manifest(proj)
        entry = project.find_entry(manifest, "CHAPTER_0001.md") or {}
        check("4a outcome: manifest marks the chapter translated",
              entry.get("status") == "translated", f"entry={entry!r}")
        check("4b outcome: translated/CHAPTER_0001.md assembled",
              (proj / "translated" / "CHAPTER_0001.md").is_file())


def make_cap_project(root: Path, name: str) -> Path:
    """Two one-line source chapters, NO glossary.json (empty glossary, so
    no drift/cleanup noise), an accurate chapters.json manifest, config.json
    {"providers": {}}, and the draft/ + translated/ + notes/ dirs
    run_chapter persists state and output into (make_project's shape)."""
    proj = root / name
    proj.mkdir()
    for d in ("source", "translated", "draft", "notes"):
        (proj / d).mkdir()
    for i in (1, 2):
        # No trailing newline: read_chapter returns the text verbatim, so
        # the body is exactly one line (make_project's convention).
        (proj / "source" / f"CHAPTER_000{i}.md").write_text(
            f"第{i}行正文。", encoding="utf-8", newline="\n")
    (proj / "chapters.json").write_text(
        json.dumps([{"file": f"CHAPTER_000{i}.md", "number": i, "suffix": "",
                     "order": i - 1, "status": "pending"}
                    for i in (1, 2)],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proj / "config.json").write_text(
        json.dumps({"providers": {}}, indent=2) + "\n", encoding="utf-8")
    return proj


def case_truncated_retry_cap() -> None:
    """The corrective retry for a TRUNCATED chunk never re-sends at a
    smaller cap than the first attempt, and the below-provider-cap config
    warn prints exactly once across a multi-chapter run. The provider
    max_tokens (100) sits far below translate_max_output_tokens (the default):
    the first TRANSLATE response is cut mid-JSON, the retry must go out at
    max_out (escalated >= max_out), and chapter 2's run must not repeat the
    warn. _escalated_cap is additionally checked directly. The expected text
    interpolates config.DEFAULTS rather than hardcoding 8192, so raising the
    default (v009) cannot silently strand this check."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_cap_project(Path(td), "proj")
        cfg = config.load_config(proj)
        cfg["providers"]["translator"][0]["max_tokens"] = 100
        # _run_path is process-global and pins the FIRST project that logs;
        # reset it so this sandbox's logs/ owns the run's trace.
        logger._run_path = None
        pipeline._TOKEN_CAP_WARNED = False
        calls: list[int | None] = []
        orig = pipeline._chat

        def fake(project_dir, cfg, job, prompt, json_schema=None,
                 max_tokens=None, chapter=None):
            if job == "translator":
                calls.append(max_tokens)
                if len(calls) == 1:
                    return '{"title": "Mock Tit'  # cut mid-JSON -> truncated
                return json.dumps(
                    {"title": "Mock Chapter Title",
                     "lines": [{"i": 1, "t": "Translated line 1."}]},
                    ensure_ascii=False)
            if "verdict" in prompt:
                return json.dumps({"verdict": "SUCCESS", "reasons": []},
                                  ensure_ascii=False)
            if '"notes"' in prompt or '"note"' in prompt:
                return json.dumps({"notes": []})
            if '"terms"' in prompt:
                return json.dumps({"terms": []})
            if '"recap"' in prompt:
                return json.dumps({"recap": "Mock recap of the story so far."})
            raise AssertionError(f"unroutable prompt (job={job}): {prompt[:120]!r}")

        pipeline._chat = fake
        try:
            outcome1, out1, exc1 = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md", cfg)
            outcome2, out2, exc2 = capture(
                pipeline.run_chapter, proj, "CHAPTER_0002.md", cfg)
        finally:
            pipeline._chat = orig

        warn = ("[warn] config: providers.translator.max_tokens (100) is "
                f"below translate_max_output_tokens "
                f"({config.DEFAULTS['translate_max_output_tokens']}) - retries "
                "cannot raise the output cap")
        check("5a cap: both chapters translate",
              exc1 is None and outcome1 == "translated"
              and exc2 is None and outcome2 == "translated",
              f"outcomes={outcome1}/{outcome2} exc={exc1!r}/{exc2!r}")
        max_out = config.DEFAULTS["translate_max_output_tokens"]
        check("5b cap: truncated first response retried at max_out, not the "
              "smaller provider cap",
              calls == [max_out, max_out, max_out], f"calls={calls}")
        check("5c cap: warn prints once across the two-chapter run",
              out1.count(warn) == 1 and warn not in out2, f"out1={out1!r}")
        check("5d cap: helper keeps escalated >= max_out",
              pipeline._escalated_cap(max_out, 100) == max_out
              and pipeline._escalated_cap(max_out, max_out * 2)
              == int(round(max_out * 1.5))
              and pipeline._escalated_cap(1000, 1200) == 1200
              and pipeline._escalated_cap(1000, 500) == 1000,
              f"caps={pipeline._escalated_cap(max_out, 100)},"
              f"{pipeline._escalated_cap(max_out, max_out * 2)},"
              f"{pipeline._escalated_cap(1000, 1200)},"
              f"{pipeline._escalated_cap(1000, 500)}")


def make_empty_source_project(root: Path, name: str) -> Path:
    """One chapter whose source file has NO body content (empty file) --
    make_cap_project's shape with a single chapter and an empty body."""
    proj = root / name
    proj.mkdir()
    for d in ("source", "translated", "draft", "notes"):
        (proj / d).mkdir()
    (proj / "source" / "CHAPTER_0001.md").write_text(
        "", encoding="utf-8", newline="\n")
    (proj / "chapters.json").write_text(
        json.dumps([{"file": "CHAPTER_0001.md", "number": 1, "suffix": "",
                     "order": 0, "status": "pending"}],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proj / "config.json").write_text(
        json.dumps({"providers": {}}, indent=2) + "\n", encoding="utf-8")
    return proj


def case_zero_line_source() -> None:
    """A source chapter with zero body content is marked needs-review with
    the warn line and NO LLM interaction -- it never reaches chunking and
    never writes a content-free translation."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_empty_source_project(Path(td), "proj")
        cfg = config.load_config(proj)
        logger._run_path = None
        llm_calls: list[str] = []
        orig = pipeline._chat

        def fake(project_dir, cfg, job, prompt, json_schema=None,
                 max_tokens=None, chapter=None):
            llm_calls.append(job)
            raise AssertionError("LLM called for an empty source chapter")

        pipeline._chat = fake
        try:
            outcome, out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md", cfg)
        finally:
            pipeline._chat = orig

        check("6a empty source: outcome needs-review, no error",
              exc is None and outcome == "needs-review",
              f"outcome={outcome} exc={exc!r}")
        check("6b empty source: warn line names the chapter",
              "[CHAPTER_0001] [warn] CHAPTER_0001.md: source chapter has no "
              "content - marked needs-review" in out, f"out={out!r}")
        check("6c empty source: the LLM is never called",
              llm_calls == [], f"llm_calls={llm_calls}")
        check("6d empty source: no translated file, manifest needs-review",
              not (proj / "translated" / "CHAPTER_0001.md").exists()
              and (project.find_entry(project.load_manifest(proj),
                                      "CHAPTER_0001.md") or {}).get("status")
              == "needs-review",
              f"manifest={project.load_manifest(proj)!r}")


def case_manifest_save_failure() -> None:
    """A PermissionError on the post-assemble manifest save must not demote
    the finished chapter: the warn prints, the outcome stays "translated",
    and the on-disk manifest keeps the in-progress status written at chapter
    start. project.save_manifest is swapped for a wrapper that fails only
    once the manifest carries a "translated" status (the chapter-start
    in-progress save still goes through) -- the suite's monkeypatch style."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = config.load_config(proj)
        logger._run_path = None
        orig_save = project.save_manifest
        saves: list[str] = []

        def failing_save(project_dir, manifest):
            if any(e.get("status") == "translated" for e in manifest):
                saves.append("translated")
                raise PermissionError(13, "mocked denied")
            saves.append("in-progress")
            return orig_save(project_dir, manifest)

        project.save_manifest = failing_save
        orig = pipeline._chat
        pipeline._chat = fake_chat
        try:
            outcome, out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md", cfg)
        finally:
            pipeline._chat = orig
            project.save_manifest = orig_save

        check("7a manifest save failure: outcome stays 'translated'",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        check("7b manifest save failure: warn names the chapter and the "
              "written file",
              "[warn] manifest update failed for CHAPTER_0001.md: [Errno 13] "
              "mocked denied - chapter file is written; status stays "
              "in-progress" in out, f"out={out!r}")
        check("7c manifest save failure: in-progress save went through, "
              "translated save failed",
              saves == ["in-progress", "translated"], f"saves={saves}")
        check("7d manifest save failure: disk keeps in-progress, file exists",
              (project.find_entry(project.load_manifest(proj),
                                  "CHAPTER_0001.md") or {}).get("status")
              == "in-progress"
              and (proj / "translated" / "CHAPTER_0001.md").is_file(),
              f"manifest={project.load_manifest(proj)!r}")


def case_notes_report_line() -> None:
    """pipeline._notes_report_line's richer shapes (the flow case above only
    pins the zero-kept one): kept notes render the category parenthetical in
    NOTE_CATEGORIES order, drops render the fixed low_threshold -> overflow
    -> invalid order with their labels (low-confidence / overflow / invalid)
    and the notes/<stem>.dropped.json pointer -- and the pointer's target is
    the file tn.save_dropped really writes."""
    tag, stem = "[CHAPTER_0009]", "CHAPTER_0009"
    kept = [
        {"line": 3, "term": "灵根", "note": "Cultivation aptitude.",
         "category": "cultural"},
        {"line": 4, "term": "灵气", "note": "Spiritual energy.",
         "category": "idiom"},
        {"line": 5, "term": "灵泉", "note": "Spirit spring.",
         "category": "idiom"},
    ]
    dropped = [
        {"line": 0, "term": "灵石", "note": "Low confidence.",
         "category": "other", "threshold": "low",
         "reason": "low_threshold"},
        {"line": 1, "term": "道基", "note": "Also low.",
         "category": "other", "threshold": "low",
         "reason": "low_threshold"},
        {"line": 2, "term": "荒塔", "note": "Overflowed the cap.",
         "category": "other", "threshold": None, "reason": "overflow"},
    ]
    line = pipeline._notes_report_line(tag, stem, kept, dropped)
    check("8a report line: full rendered line verbatim (parenthetical, "
          "reason labels, dropped pointer)",
          line == "[CHAPTER_0009] [ok] notes: 3 kept (cultural 1, idiom 2)"
                  "; 3 dropped (2 low-confidence, 1 overflow) "
                  "-> notes/CHAPTER_0009.dropped.json",
          f"line={line!r}")

    # All three reasons render in the fixed DROP_REASONS order, and a kept
    # category outside NOTE_CATEGORIES counts under "other".
    dropped3 = dropped + [
        {"line": 6, "term": "坏项", "note": None, "category": None,
         "threshold": None, "reason": "invalid"},
    ]
    kept_other = [
        {"line": 7, "term": "灵兽", "note": "Beast.", "category": "monster"},
    ]
    line3 = pipeline._notes_report_line(tag, stem, kept_other, dropped3)
    check("8b report line: three reasons in the fixed order, unknown "
          "category lands in 'other'",
          line3 == "[CHAPTER_0009] [ok] notes: 1 kept (other 1)"
                   "; 4 dropped (2 low-confidence, 1 overflow, 1 invalid) "
                   "-> notes/CHAPTER_0009.dropped.json",
          f"line3={line3!r}")

    # The pointer's target is the artifact save_dropped really writes.
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        tn.save_dropped(proj, "CHAPTER_0009.md", dropped)
        pointer = "notes/CHAPTER_0009.dropped.json"
        check("8c report line: the dropped-file pointer target exists",
              (proj / pointer).is_file(), f"pointer={pointer}")
        document = json.loads((proj / pointer).read_text(encoding="utf-8"))
        check("8d report line: the artifact records the same drop reasons",
              [d["reason"] for d in document["dropped"]]
              == ["low_threshold", "low_threshold", "overflow"],
              f"dropped={document['dropped']!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_cleanup_flow()
    case_truncated_retry_cap()
    case_zero_line_source()
    case_manifest_save_failure()
    case_notes_report_line()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
