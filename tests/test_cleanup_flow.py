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
line `[Chapter_0001] [glossary] retired mundane term '灵石' (mundane mock
term)` prints between the FAITH and GLOSSARY_EXPAND init lines; glossary.json
after the run is exactly {"terms": [], "retired": ["灵石"]} (whole-file
equality -- the deferred retire and the GLOSSARY_EXPAND save must agree),
find() no longer resolves 灵石, and retired_sources() == {"灵石"}; the
per-invocation trace (logs/llm-*.jsonl, filtered by event type, never by
line index -- the run also writes attempt and balance_advisory events)
contains exactly one glossary_cleanup event with chapter
"Chapter_0001.md", removed == [{"source": "灵石", "reason": "mundane mock
term"}], kept == []; and the manifest marks the chapter translated.

The fake _chat's branch order is load-bearing (mock_server.py prompt
sniffing): the bare word "notes" appears in translation.md and recap.md,
so the notes check must use the QUOTED forms; "Flagged Terms" is the
glossary_cleanup.md marker; the numbered translate mirror (echo the
"### Source Data" array, test_chunking.py's convention) must not fire for
the recap prompt (recap.md has no Source Data section), and the recap
answer must be non-blank because story.py rejects a blank recap.

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

from lib import config, glossary, logger, pipeline, project  # noqa: E402

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


def fake_chat(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None):
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
    (proj / "source" / "Chapter_0001.md").write_text(
        "他捡起一块灵石。\n灵石发光了。\n第三行。", encoding="utf-8", newline="\n")
    (proj / "chapters.json").write_text(
        json.dumps([{"file": "Chapter_0001.md", "number": 1, "suffix": "",
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
    "[Chapter_0001] [glossary] retired mundane term '灵石' (mundane mock term)\n"
)

# The run's full console output is deterministic: eight lines, the retired
# line landing between the FAITH and GLOSSARY_EXPAND init lines.
EXPECTED_OUTPUT = (
    "[Chapter_0001] [init] TRANSLATE (attempt 1)\n"
    "[Chapter_0001] [init] FAITH\n"
    + RETIRED_LINE +
    "[Chapter_0001] [init] GLOSSARY_EXPAND\n"
    "[Chapter_0001] [init] TN_GENERATE\n"
    "[Chapter_0001] [ok] notes: 0 kept\n"
    "[Chapter_0001] [ok] translated -> Chapter_0001.md "
    "(title: Mock Chapter Title, notes: 0)\n"
    "[Chapter_0001] [init] recap\n"
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
                pipeline.run_chapter, proj, "Chapter_0001.md", cfg)
        finally:
            pipeline._chat = orig

        # ---- the run itself
        check("1a run: completes without error, outcome 'translated'",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        check("1b run: the exact retired-mundane-term console line",
              RETIRED_LINE in out, f"out={out!r}")
        check("1c run: the translated summary line printed",
              "[Chapter_0001] [ok] translated ->" in out, f"out={out!r}")
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
        events = []
        for log_path in sorted((proj / "logs").glob("llm-*.jsonl")):
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
              and cleanups[0].get("chapter") == "Chapter_0001.md"
              and cleanups[0].get("removed")
              == [{"source": "灵石", "reason": "mundane mock term"}]
              and cleanups[0].get("kept") == [],
              f"event={cleanups[0] if cleanups else None!r}")

        # ---- run outcome effects
        manifest = project.load_manifest(proj)
        entry = project.find_entry(manifest, "Chapter_0001.md") or {}
        check("4a outcome: manifest marks the chapter translated",
              entry.get("status") == "translated", f"entry={entry!r}")
        check("4b outcome: translated/Chapter_0001.md assembled",
              (proj / "translated" / "Chapter_0001.md").is_file())


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_cleanup_flow()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
