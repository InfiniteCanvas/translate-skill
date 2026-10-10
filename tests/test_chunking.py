"""Tests for the TRANSLATE chunking rework: greedy per-line token-budget
packing (_pack_chunks / _line_output_cost), the escalating corrective retry
for truncating chunks, the oversized-line fail-fast, and per-chunk
persistence/resume via state["chunks"].

Packing replaces the old line-count-balanced (divmod) splitter: chunks are
sized by _line_output_cost (CJK x1.0, other chars /4, +10 per line; +256
once per chunk) against
budget = floor(0.8 * pack_cap), each chunk taking >= 1
line. A single line whose cost exceeds the budget is isolated as a
singleton chunk called directly at the escalated cap
(max(wire_cap, min(round(1.5 * pack_cap), providers.translator max_tokens)));
a line that
cannot fit even the escalated cap fails fast with actionable feedback and
ZERO LLM calls. Packing is deterministic given (source, config), which the
feedback slicing (the _feedback_section [lo, hi) slice of the rejected
snapshot) and the crash resume both depend on.

Two caps, deliberately distinct since v012:
  pack_cap = min(translate_max_output_tokens, the tightest translator block's
                max_tokens) -- the SIZING budget, so no block truncates its part
  wire_cap = translate_max_output_tokens -- the ceiling SENT, which client.chat
                then lowers per block to that block's own max_tokens
Collapsing them is what let a chapter be packed for 256k and sent to a
provider that returns 128k. In these fixtures the provider blocks resolve to
DEFAULT_MAX_TOKENS, far above the cap, so the ceiling never binds and
pack_cap == wire_cap == MAX_OUT; the split is exercised in
test_token_cap_ceiling.py.

Covered: (1) packing determinism and shape (multi-chapter source -> non-
empty chunks whose estimated cost fits the budget, bounds identical across
computations, small source -> exactly 1 chunk, per-line cost pinned to
literals: LINE -> 30, the 10-line chapter -> 300); (2) oversized-line fail-fast through run_chapter
(feedback names the line, zero model calls); (3) the singleton oversized
line is called directly at the escalated cap; (4) escalation semantics
(missing tail indices -> corrective retry at the escalated cap, duplicate
index -> retry at the same cap); (5) per-chunk persistence and resume
(seeded chunks -> only the remaining chunks are called, the resume console
line prints, title preserved, lines complete; a fully-completed chunk list
skips the loop; packing drift discards the seeds with a [warn]); (6) a
gate failure clears state["chunks"] so the retry retranslates the whole
chapter; (7) THE PINNED INVARIANT: after a gate rejection, chunk k's retry
prompt shows exactly the rejected lines inside that chunk's CURRENT packing
bounds -- never the whole chapter.

Mechanics mirror tests/test_retry_feedback.py: TemporaryDirectory sandbox
projects (source chapter without frontmatter, accurate chapters.json,
config.json, empty glossary.json, draft/ + translated/), and a
monkeypatched pipeline._chat that records {"job", "prompt", "max_tokens"}
per call and answers translate calls by echoing the numbered source data
parsed out of the prompt's "### Source Data" section (mock_server.py
conventions). Chunking is forced with a small translate_max_output_tokens
(500; escalated 750) so a 20-CJK-char line (cost 30) packs 4 to a chunk
(room = 400 - 256 = 144).

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_chunking.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config, pipeline, project

PASSED = 0
FAILED: list[str] = []

MAX_OUT = 500
ESCALATED = 750
LINE = "中" * 20


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
    except Exception as caught:
        exc = caught
    return result, buf.getvalue(), exc


def sniff(prompt: str) -> str:
    """Which pipeline role a prompt targets (mock_server.py convention)."""
    if "verdict" in prompt:
        return "verdict"
    if '"recap"' in prompt:
        return "recap"
    if '"terms"' in prompt:
        return "terms"
    if '"notes"' in prompt or '"note"' in prompt:
        return "notes"
    return "translate"


def source_entries(prompt: str) -> list[dict]:
    """The numbered source lines of a translate prompt (its '### Source
    Data' JSON array)."""
    tail = prompt.rsplit("### Source Data", 1)[1].strip()
    return json.loads(tail)


def rejected_section(prompt: str) -> str:
    """The [Rejected Previous Attempt] block of a retry prompt (up to the
    '### Source Data' header). Chunk prompts legitimately carry the previous
    chunk's final translated lines as continuity context, so leak checks
    must scope to this block -- the feedback slicing's actual output."""
    if "[Rejected Previous Attempt]" not in prompt:
        return ""
    return (prompt.rsplit("[Rejected Previous Attempt]", 1)[1]
            .rsplit("### Source Data", 1)[0])


def echo_response(entries: list[dict]) -> str:
    """A structurally valid translation echoing every input index."""
    return json.dumps(
        {"title": "Mock Title",
         "lines": [{"i": e["i"], "t": f"Translated line {e['i']}."}
                   for e in entries]},
        ensure_ascii=False)


def make_fake_chat(calls: list[dict],
                   translate_scripts: list | None = None,
                   verdicts: list[tuple[str, list[str]]] | None = None):
    """pipeline._chat replacement: records {"job", "prompt", "max_tokens"}
    per call. Translate calls consume one entry of translate_scripts (a
    callable numbered-source -> response string); when the queue is empty
    (or None) they echo coverage. Verdicts pop from verdicts (default
    SUCCESS); terms/notes/recap answer empty."""

    def fake(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None,
            chapter=None):
        calls.append({"job": job, "prompt": prompt, "max_tokens": max_tokens})
        if "verdict" in prompt:
            verdict, reasons = (
                verdicts.pop(0) if verdicts else ("SUCCESS", []))
            return json.dumps({"verdict": verdict, "reasons": reasons},
                              ensure_ascii=False)
        if '"recap"' in prompt:
            return json.dumps({"recap": "Mock recap of the story so far."})
        if '"terms"' in prompt:
            return json.dumps({"terms": []})
        if '"notes"' in prompt or '"note"' in prompt:
            return json.dumps({"notes": []})
        entries = source_entries(prompt)
        if translate_scripts:
            return translate_scripts.pop(0)(entries)
        return echo_response(entries)

    return fake


def make_project(root: Path, name: str, lines: list[str],
                 cfg_extra: dict | None = None) -> Path:
    """Minimal project (test_retry_feedback's shape): one chapter under
    source/ built from plain body lines (no frontmatter, no trailing
    newline), an accurate chapters.json manifest, config.json
    {"providers": {}} + cfg_extra, an empty glossary.json, and the draft/ +
    translated/ dirs run_chapter persists state and output into."""
    proj = root / name
    proj.mkdir()
    (proj / "source").mkdir(parents=True, exist_ok=True)
    with open(proj / "source" / "CHAPTER_0001.md", "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    (proj / "chapters.json").write_text(
        json.dumps([{"file": "CHAPTER_0001.md", "number": 1, "suffix": "",
                     "order": 0, "status": "pending"}],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cfg = {"providers": {}, "translate_max_output_tokens": MAX_OUT}
    cfg.update(cfg_extra or {})
    (proj / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proj / "glossary.json").write_text(
        json.dumps({"terms": []}, ensure_ascii=False) + "\n", encoding="utf-8")
    (proj / "draft").mkdir()
    (proj / "translated").mkdir()
    return proj


def seed_state(proj: Path, chunks: list[list[str]] | None, title=None) -> None:
    """Write a draft state file as a crashed mid-TRANSLATE run would have
    left it (completed chunks persisted after their per-chunk save)."""
    state = {
        "stage": "TRANSLATE", "attempt": 0, "feedback": [],
        "title": title, "lines": None, "chunks": chunks, "notes": None,
        "rejected": None, "updated_at": "", "pipeline": 2,
    }
    (proj / "draft" / "CHAPTER_0001.state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def translate_calls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if sniff(c["prompt"]) == "translate"]


def case_1_packing() -> None:
    """Determinism and shape: a multi-chapter source packs into non-empty
    chunks whose estimated cost fits the budget, identically across two
    computations; a small source is exactly one chunk; the per-line cost
    itself is pinned (LINE -> 30, the 10-line chapter -> 300)."""
    big = [LINE] * 10
    plan = pipeline._pack_chunks(big, MAX_OUT, ESCALATED, MAX_OUT)
    again = pipeline._pack_chunks(big, MAX_OUT, ESCALATED, MAX_OUT)
    check("1a packing: bounds identical across two computations",
          plan == again, f"plan={plan} again={again}")
    check("1b packing: multi-chapter source splits (4/4/2)",
          [(lo, hi) for lo, hi, _cap in plan]
          == [(0, 4), (4, 8), (8, 10)],
          f"plan={plan}")
    check("1c packing: every chunk non-empty (W2)",
          all(hi > lo for lo, hi, _cap in plan), f"plan={plan}")
    budget = MAX_OUT * 4 // 5
    fits = all(
        sum(pipeline._line_output_cost(ln) for ln in big[lo:hi]) + 256 <= budget
        for lo, hi, _cap in plan
    )
    check("1d packing: each chunk's estimated cost fits the budget",
          fits, f"budget={budget} plan={plan}")
    check("1e packing: chunks tile the chapter exactly",
          [lo for lo, _hi, _cap in plan] == [0, 4, 8] and plan[-1][1] == 10,
          f"plan={plan}")
    small = ["第一行。", "第二行。", "第三行。"]
    plan_small = pipeline._pack_chunks(small, MAX_OUT, ESCALATED, MAX_OUT)
    check("1f packing: small source -> exactly 1 chunk",
          [(lo, hi) for lo, hi, _cap in plan_small] == [(0, 3)],
          f"plan={plan_small}")
    check("1g packing: per-line cost pinned (LINE -> 30, chapter -> 300)",
          pipeline._line_output_cost(LINE) == 30
          and sum(pipeline._line_output_cost(ln) for ln in big) == 300,
          f"LINE={pipeline._line_output_cost(LINE)}")
    check("1h packing: whole chapter under the budget -> one chunk",
          [(lo, hi) for lo, hi, _cap in
           pipeline._pack_chunks(small, 8192, 12288, 8192)] == [(0, 3)])


def case_2_oversized_fail_fast() -> None:
    """A source line whose estimated output exceeds even the escalated cap
    fails the TRANSLATE stage with feedback naming the line -- and zero LLM
    calls are burned (the raise happens in packing, before any request)."""
    lines = ["中" * 485, LINE]
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        calls: list[dict] = []
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("2a fail-fast: no exception escapes run_chapter",
              exc is None, f"exc={exc!r}")
        check("2b fail-fast: outcome needs-review after 3 attempt failures",
              outcome == "needs-review", f"outcome={outcome}")
        check("2c fail-fast: ZERO model calls were made",
              len(calls) == 0, f"calls={calls}")
        state = json.loads(
            (proj / "draft" / "CHAPTER_0001.state.json")
            .read_text(encoding="utf-8"))
        fb = state.get("feedback") or []
        check("2d fail-fast: feedback names the oversized line",
              any(
                  "source line 1 alone exceeds the output budget "
                  "(estimated 495 tokens > 750 cap); "
                  "split or shorten the line manually" in item
                  for item in fb),
              f"feedback={fb}")
        check("2e fail-fast: one feedback entry per failed attempt",
              len([f for f in fb if f.startswith("TRANSLATE failed:")]) == 3,
              f"feedback={fb}")


def case_3_singleton_escalated() -> None:
    """A line whose cost exceeds the packing budget but fits the escalated
    cap is isolated as a singleton chunk whose FIRST call already uses the
    escalated max_tokens (no guaranteed-truncation warm-up call)."""
    lines = ["中" * 420] + [LINE] * 3
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        calls: list[dict] = []
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("3a singleton: chapter translates",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        tcalls = translate_calls(calls)
        check("3b singleton: exactly 2 chunks called",
              len(tcalls) == 2, f"n={len(tcalls)}")
        if len(tcalls) == 2:
            check("3c singleton: oversized line called at the escalated cap",
                  tcalls[0]["max_tokens"] == ESCALATED,
                  f"max_tokens={tcalls[0]['max_tokens']}")
            check("3d singleton: oversized chunk covers only line 1",
                  [e["i"] for e in source_entries(tcalls[0]["prompt"])] == [1],
                  "prompt covers more than line 1")
            check("3e singleton: normal chunk called at the normal cap",
                  tcalls[1]["max_tokens"] == MAX_OUT,
                  f"max_tokens={tcalls[1]['max_tokens']}")
            check("3f singleton: normal chunk covers lines 2-4",
                  [e["i"] for e in source_entries(tcalls[1]["prompt"])]
                  == [2, 3, 4],
                  "prompt covers wrong lines")


def case_4_escalating_retry() -> None:
    """Truncation signature escalates: a response missing tail line indices
    gets its corrective retry at the escalated cap, while a duplicate-index
    response retries at the same cap. Both recover on the retry."""
    lines = [LINE] * 8
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        calls: list[dict] = []
        scripts = [
            echo_response,
            lambda entries: json.dumps(
                {"title": "Mock Title", "lines":
                 [{"i": e["i"], "t": f"Translated line {e['i']}."}
                  for e in entries[:-1]]}, ensure_ascii=False),
            echo_response,
        ]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, translate_scripts=scripts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        tcalls = translate_calls(calls)
        check("4a escalation: truncating chunk recovers and translates",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        check("4b escalation: 3 translate calls (chunk, chunk, retry)",
              len(tcalls) == 3, f"n={len(tcalls)}")
        if len(tcalls) == 3:
            check("4c escalation: retry for the truncated chunk uses the "
                  "escalated cap",
                  tcalls[2]["max_tokens"] == ESCALATED
                  and tcalls[1]["max_tokens"] == MAX_OUT,
                  f"caps={[c['max_tokens'] for c in tcalls]}")
            check("4d escalation: retry prompt names the missing line",
                  "missing line(s) 8" in tcalls[2]["prompt"],
                  "prompt lacks the missing-index problem")

    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        calls: list[dict] = []
        scripts = [
            echo_response,
            lambda entries: json.dumps(
                {"title": "Mock Title", "lines":
                 ([{"i": entries[0]["i"],
                    "t": "Translated line 5."}]
                  + [{"i": e["i"], "t": f"Translated line {e['i']}."}
                     for e in entries])}, ensure_ascii=False),
            echo_response,
        ]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, translate_scripts=scripts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        tcalls = translate_calls(calls)
        check("4e same-cap: duplicate-index chunk recovers and translates",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        check("4f same-cap: 3 translate calls",
              len(tcalls) == 3, f"n={len(tcalls)}")
        if len(tcalls) == 3:
            check("4g same-cap: duplicate-index retry keeps the same cap",
                  tcalls[1]["max_tokens"] == MAX_OUT
                  and tcalls[2]["max_tokens"] == MAX_OUT,
                  f"caps={[c['max_tokens'] for c in tcalls]}")
            check("4h same-cap: retry prompt names the duplicate index",
                  "duplicated line index(es) 5" in tcalls[2]["prompt"],
                  "prompt lacks the duplicate-index problem")


def case_5_persistence_resume() -> None:
    """Per-chunk persistence: a state seeded with the first k chunk
    translations + the stashed title resumes at part k+1 (only the
    remaining chunks are called), the resume console line prints, and the
    final lines list is complete with the title preserved."""
    lines = [LINE] * 10
    seeds = [[f"Seed line {i}." for i in range(1, 5)],
             [f"Seed line {i}." for i in range(5, 9)]]
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        seed_state(proj, seeds, title="Seeded Title")
        calls: list[dict] = []
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls)
        try:
            outcome, out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        tcalls = translate_calls(calls)
        check("5a resume: chapter translates",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        check("5b resume: only the remaining chunk is called",
              len(tcalls) == 1
              and [e["i"] for e in source_entries(tcalls[0]["prompt"])]
              == [9, 10],
              f"n={len(tcalls)}")
        check("5c resume: the resume console line prints",
              "[CHAPTER_0001] [init] resuming translation at part 3/3 "
              "(8 lines already done)" in out,
              f"out={out!r}")
        lines_json = json.loads(
            (proj / "draft" / "CHAPTER_0001.lines.json")
            .read_text(encoding="utf-8"))
        check("5d resume: state lines complete (seeds + fresh tail)",
              lines_json["lines"]
              == [f"Seed line {i}." for i in range(1, 9)]
              + ["Translated line 9.", "Translated line 10."],
              f"lines={lines_json.get('lines')}")
        check("5e resume: stashed title preserved",
              lines_json["title"] == "Seeded Title",
              f"title={lines_json.get('title')!r}")
        check("5f resume: translated chapter carries every line",
              (proj / "translated" / "CHAPTER_0001.md").is_file()
              and "Seed line 8." in
              (proj / "translated" / "CHAPTER_0001.md").read_text("utf-8"),
              "translated file incomplete")
        check("5g resume: state file cleaned up on success",
              not (proj / "draft" / "CHAPTER_0001.state.json").exists())

    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        seed_state(proj,
                   [[f"Seed line {i}." for i in range(1, 5)],
                    [f"Seed line {i}." for i in range(5, 9)],
                    ["Seed line 9.", "Seed line 10."]],
                   title="Seeded Title")
        calls: list[dict] = []
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("5h complete: loop skipped, chapter finishes with 0 translate "
              "calls",
              exc is None and outcome == "translated"
              and len(translate_calls(calls)) == 0,
              f"outcome={outcome} exc={exc!r} "
              f"n={len(translate_calls(calls))}")
        lines_json = json.loads(
            (proj / "draft" / "CHAPTER_0001.lines.json")
            .read_text(encoding="utf-8"))
        check("5i complete: seeded lines flow through unmodified",
              lines_json["lines"] == [f"Seed line {i}." for i in range(1, 11)],
              f"lines={lines_json.get('lines')}")

    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        seed_state(proj, [["Seed line 1.", "Seed line 2."]],
                   title="Seeded Title")
        calls: list[dict] = []
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls)
        try:
            outcome, out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("5j drift: mismatched seeds discarded, chapter retranslates",
              exc is None and outcome == "translated"
              and len(translate_calls(calls)) == 3,
              f"outcome={outcome} exc={exc!r} "
              f"n={len(translate_calls(calls))}")
        check("5k drift: the packing-drift warn prints",
              "[warn] saved chunks do not match the current packing "
              "(source or config changed?) - retranslating from scratch"
              in out,
              f"out={out!r}")


def case_6_gate_failure_clears_chunks() -> None:
    """A gate failure (VALIDATE) clears state["chunks"]: the retry
    retranslates the whole chapter instead of resuming from stale chunks."""
    lines = ["你好。", "", "再见。"]
    filler = lambda entries: json.dumps(
        {"title": "Mock Title",
         "lines": [{"i": e["i"],
                    "t": "Filled." if e["t"] == ""
                    else f"Translated line {e['i']}."} for e in entries]},
        ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines, {"max_attempts": 2})
        calls: list[dict] = []
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, translate_scripts=[filler] * 4)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("6a gate-clear: VALIDATE failure -> needs-review",
              exc is None and outcome == "needs-review",
              f"outcome={outcome} exc={exc!r}")
        check("6b gate-clear: both attempts reached the gate (2 translate "
              "calls, no corrective retry)",
              len(translate_calls(calls)) == 2,
              f"n={len(translate_calls(calls))}")
        state = json.loads(
            (proj / "draft" / "CHAPTER_0001.state.json")
            .read_text(encoding="utf-8"))
        check("6c gate-clear: saved state has chunks == []",
              state.get("chunks") == [], f"chunks={state.get('chunks')!r}")
        check("6d gate-clear: stage reset to TRANSLATE",
              state.get("stage") == "TRANSLATE", f"stage={state.get('stage')!r}")
        check("6e gate-clear: feedback carries the VALIDATE issue",
              any("Line 2: source line is empty but translation is not"
                  in item for item in state.get("feedback") or []),
              f"feedback={state.get('feedback')}")


def case_7_pinned_feedback_slicing() -> None:
    """THE PINNED INVARIANT: with a source sized for >= 2 chunks, a gate
    rejection followed by a retry shows chunk k's feedback section holding
    EXACTLY the rejected lines within that chunk's current packing bounds
    [lo, hi) -- not the whole chapter -- and the chunk bounds themselves
    are identical between the rejected attempt and the retry."""
    lines = [LINE] * 8
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", lines)
        calls: list[dict] = []
        verdicts = [("FAILURE", ["Line 2: wrong term"]), ("SUCCESS", [])]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, verdicts=verdicts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "CHAPTER_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("7a pinned: chapter translates after the rejection",
              exc is None and outcome == "translated",
              f"outcome={outcome} exc={exc!r}")
        tcalls = translate_calls(calls)
        check("7b pinned: 4 translate calls (2 attempts x 2 chunks)",
              len(tcalls) == 4, f"n={len(tcalls)}")
        if len(tcalls) != 4:
            return
        a1c1, a1c2, a2c1, a2c2 = (c["prompt"] for c in tcalls)
        check("7c pinned: chunk 1 bounds identical across attempts",
              source_entries(a1c1) == source_entries(a2c1)
              and [e["i"] for e in source_entries(a2c1)] == [1, 2, 3, 4],
              "attempt-2 chunk 1 payload differs")
        check("7d pinned: chunk 2 bounds identical across attempts",
              source_entries(a1c2) == source_entries(a2c2)
              and [e["i"] for e in source_entries(a2c2)] == [5, 6, 7, 8],
              "attempt-2 chunk 2 payload differs")
        check("7e pinned: chunk 1 retry carries the rejected section",
              '"i": 1, "t": "Translated line 1."' in rejected_section(a2c1)
              and '"i": 4, "t": "Translated line 4."' in rejected_section(a2c1),
              "chunk 1 rejected slice wrong")
        check("7f pinned: chunk 1 retry shows NOTHING past its hi bound",
              all(f"Translated line {i}." not in rejected_section(a2c1)
                  for i in (5, 6, 7, 8)),
              "chunk 1 rejected section leaks later lines")
        check("7g pinned: chunk 2 retry carries its own rejected slice",
              '"i": 5, "t": "Translated line 5."' in rejected_section(a2c2)
              and '"i": 8, "t": "Translated line 8."' in rejected_section(a2c2),
              "chunk 2 rejected slice wrong")
        check("7h pinned: chunk 2 retry shows NOTHING before its lo bound",
              all(f"Translated line {i}." not in rejected_section(a2c2)
                  for i in (1, 2, 3, 4)),
              "chunk 2 rejected section leaks earlier lines")
        check("7i pinned: both retry prompts carry the gate feedback bullet",
              "- Line 2: wrong term" in a2c1 and "- Line 2: wrong term" in a2c2,
              "feedback bullet missing from a retry prompt")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_packing()
    case_2_oversized_fail_fast()
    case_3_singleton_escalated()
    case_4_escalating_retry()
    case_5_persistence_resume()
    case_6_gate_failure_clears_chunks()
    case_7_pinned_feedback_slicing()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
