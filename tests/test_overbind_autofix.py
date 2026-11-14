"""Tests for the FAITH-rejection remediation policy (run_chapter).

The policy under test (pipeline._record_term_declines + _contested_term +
_handle_overbind, driven by the FAITH stage): a rejection whose review reasons
retranslation cannot fix now has two structural exits, and one ordinary one.

- Glossary over-binding, confirmed by the stage-2 investigation with a
  "retire" action (glossary_overbind.md, the OVERBIND schema): the entry is
  retired on the spot via glossary.retire(), the retry translates against the
  corrected glossary, and when the retirement lands on the attempt the cap is
  about to enforce, ONE fix-retry is granted past max_attempts
  (state["glossary_fix_retry"], consumed by the give-up branch, capped at one
  per chapter via glossary_fix_retries).
- A compliance oscillation ("term whipsaw"): declines recorded per FAITH
  failure (state["term_declines"], free arithmetic, no model call) form a
  sawtooth -- declined, complied-with (and still rejected), declined again --
  and the chapter stops immediately as needs-review with the adjudication
  diagnosis, before any further attempt. A whipsaw term that was just
  retired is exempt: its constraint is gone, so the loop continues on the
  fix-retry instead.
- Confirmed terms with a "keep" action: warn only, glossary untouched, the
  loop continues and gives up at the cap as before.

The provider seams follow the suite's established convention: pipeline._chat
monkeypatched for the reviewer/translator/glossary/annotator jobs (the fake's
branch order is load-bearing, mock_server.py prompt sniffing) and
consensus.chat swapped for the verify job the investigation calls (the
test_verify_cascade Stub shape) -- nothing between run_chapter and the
provider seam is replaced.

Covered per case: the exact console lines ("retired over-bound term", the
human-decision warn, the whipsaw FAIL block with the diagnosis and the
feedback echo, the fix-retry grant line), glossary.json whole-file state
after each shape of run, the term_declines / glossary_overbind_fix /
term_whipsaw trace events, the manifest status, the retry's TRANSLATE prompt
no longer carrying the retired pin (the corrected glossary really reached
the attempt), and that the whipsaw stop costs no retirement and no extra
attempt beyond the one that proved it.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_overbind_autofix.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0", "openai>=1.60", "anthropic>=1.0"]
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

from lib import config, consensus, glossary, logger, pipeline, project, verify

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


CHAPTER = "CHAPTER_0001.md"
SOURCE_BODY = "他捡起一块灵石。\n灵石发光了。\n第三行。"
# The decline shape: the canonical rendering "spirit stone" appears nowhere,
# so the term lands in balance's underused rows (src 2, floor 1, rendered 0).
DECLINING_LINES = ["Translated line 1.", "Translated line 2.", "Translated line 3."]
# The compliance shape: the canonical rendering present once meets the floor.
COMPLYING_LINES = ["It is a spirit stone.", "Translated line 2.", "Translated line 3."]


def make_project(root: Path, *, max_attempts: int = 3) -> Path:
    """Pipeline-shaped fixture (test_cleanup_flow's shape): one three-line
    source chapter, a one-entry glossary (灵石 -> "spirit stone"), a manifest
    entry pending, and the draft/ + translated/ + notes/ dirs pre-created."""
    proj = root / "proj"
    proj.mkdir()
    for d in ("source", "translated", "draft", "notes"):
        (proj / d).mkdir()
    (proj / "source" / CHAPTER).write_text(SOURCE_BODY, encoding="utf-8", newline="\n")
    (proj / "chapters.json").write_text(
        json.dumps([{"file": CHAPTER, "number": 1, "suffix": "",
                     "order": 0, "status": "pending"}],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    glossary.save(proj, {"terms": [{
        "source": "灵石", "variants": [], "translation": "spirit stone",
        "alt_translations": [], "category": "item",
        "definition": "A spirit stone.",
    }]})
    (proj / "config.json").write_text(
        json.dumps({"max_attempts": max_attempts,
                    "providers": {"translator": {"base_url": "http://test.invalid/v1"}}},
                   indent=2) + "\n", encoding="utf-8")
    return proj


def run(proj: Path, chat, consensus_chat=None, **kwargs):
    """run_chapter with the provider seams swapped; returns (outcome, stdout,
    verify_prompts, exc). verify_prompts collects the investigation's prompts."""
    verify_prompts: list[str] = []
    orig_consensus = consensus.chat

    def consensus_stub(project_dir, cfg, job, prompt, json_schema=None,
                       max_tokens=None, chapter=None):
        assert job == "verify", f"unexpected consensus job: {job}"
        verify_prompts.append(prompt)
        return consensus_chat(prompt, chapter)

    logger._run_path = None
    real_chat = pipeline._chat
    pipeline._chat = chat
    outcome, out, exc = None, None, None
    buf = io.StringIO()
    try:
        consensus.chat = consensus_stub
        with contextlib.redirect_stdout(buf):
            outcome = pipeline.run_chapter(proj, CHAPTER, config.load_config(proj),
                                           **kwargs)
    except Exception as caught:
        exc = caught
    finally:
        pipeline._chat = real_chat
        consensus.chat = orig_consensus
    return outcome, buf.getvalue(), verify_prompts, exc


def reviewer_script(verdicts: list[tuple[bool, list[str]]]):
    """pipeline._chat fake: the reviewer fails/succeeds per attempt in order;
    every other job answers in the test_cleanup_flow shapes. Translator calls
    are collected so the retry's glossary section can be inspected."""
    translator_prompts: list[str] = []
    calls = {"reviewer": 0}

    def chat(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None,
             chapter=None):
        if "verdict" in prompt and '"terms"' not in prompt:
            i = calls["reviewer"]
            calls["reviewer"] += 1
            ok, reasons = verdicts[i] if i < len(verdicts) else verdicts[-1]
            return json.dumps({"verdict": "SUCCESS" if ok else "FAILURE",
                               "reasons": reasons}, ensure_ascii=False)
        if '"notes"' in prompt or '"note"' in prompt:
            return json.dumps({"notes": []})
        if "Flagged Terms" in prompt:
            found = [s.split()[0] for s in
                     prompt.rsplit("[Flagged Terms]", 1)[1]
                     .rsplit("[Sample Source Lines]", 1)[0].split("\n")
                     if s.strip().startswith("- ")]
            return json.dumps(
                {"decisions": [{"source": t, "keep": True,
                                "reason": "named entity"} for t in found]},
                ensure_ascii=False)
        if '"terms"' in prompt:
            return json.dumps({"terms": []})
        if "### Source Data" in prompt:
            translator_prompts.append(prompt)
            arr = json.loads(prompt.rsplit("### Source Data", 1)[1].strip())
            return json.dumps(
                {"title": "Mock Chapter Title",
                 "lines": [{"i": x["i"], "t": x["t"]} for x in arr]},
                ensure_ascii=False)
        if '"recap"' in prompt:
            return json.dumps({"recap": "Mock recap of the story so far."})
        raise AssertionError(f"unroutable prompt (job={job}): {prompt[:120]!r}")

    return chat, translator_prompts


def verify_script(verdict_rows: list[dict]):
    """consensus.chat responder for the verify job: one verdicts payload no
    matter how many batches call."""
    def responder(prompt, chapter):
        return json.dumps({"verdicts": verdict_rows}, ensure_ascii=False)
    return responder


def read_events(proj: Path) -> list[dict]:
    events: list[dict] = []
    for path in sorted((proj / "logs").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def case_a_confirmed_overbind_retires_and_retry_passes() -> None:
    """The retire auto-fix: investigation confirms with action=retire, the
    entry is retired on the spot, the retry translates against the corrected
    glossary and the chapter lands as translated."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td))
        chat, translator_prompts = reviewer_script(
            [(False, ["the review invented an event"]), (True, [])])
        outcome, out, verify_prompts, exc = run(
            proj, chat, verify_script([
                {"source": "灵石", "chapter": CHAPTER, "mistranslates": True,
                 "action": "retire", "reason": "over-bound mock entry"}]))

        check("A1 the chapter ran to translated",
              exc is None and outcome == "translated", f"{outcome} {exc!r}")
        check("A2 the retirement reached the console",
              "[glossary] retired over-bound term '灵石' (over-bound mock entry)"
              in out and "glossary auto-fix applied" in out, out)
        g = glossary.load(proj)
        check("A3 glossary.json is exactly the retired shape",
              g.get("terms") == [] and g.get("retired") == ["灵石"],
              json.dumps(g, ensure_ascii=False))
        check("A4 the retry's TRANSLATE prompt dropped the retired pin",
              len(translator_prompts) == 2
              and "spirit stone" in translator_prompts[0]
              and "spirit stone" not in translator_prompts[1],
              f"{len(translator_prompts)} prompts")
        check("A5 the investigation asked the model once (attempt 1 only)",
              len(verify_prompts) == 1, f"{len(verify_prompts)} prompts")
        events = read_events(proj)
        fixes = [e for e in events if e.get("event") == "glossary_overbind_fix"]
        check("A6 the fix event carries the source and its reason",
              len(fixes) == 1 and fixes[0].get("removed") == [
                  {"source": "灵石", "reason": "over-bound mock entry"}],
              str(fixes))
        declines = [e for e in events if e.get("event") == "term_declines"]
        check("A7 the decline was recorded once, on the failed attempt",
              len(declines) == 1 and declines[0].get("declined") == ["灵石"],
              str(declines))
        check("A8 no fix-retry grant was needed (cap was not reached)",
              "retrying once with the corrected glossary" not in out, out)
        manifest = project.load_manifest(proj)
        entry = project.find_entry(manifest, CHAPTER)
        check("A9 the manifest marks the chapter translated",
              entry and entry.get("status") == "translated", str(entry))
        check("A10 the draft state is gone after assembly",
              not (proj / "draft" / "CHAPTER_0001.state.json").is_file())


def case_b_whipsaw_stops_the_loop() -> None:
    """The oscillation stop: declined, complied (and still rejected), declined
    again -- the chapter stops as needs-review with the adjudication
    diagnosis, the glossary untouched, and no attempt beyond the one that
    proved the contradiction."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td))
        calls = {"n": 0}

        def chat(project_dir, cfg, job, prompt, json_schema=None,
                 max_tokens=None, chapter=None):
            if "verdict" in prompt and '"terms"' not in prompt:
                return json.dumps({"verdict": "FAILURE",
                                   "reasons": ["the review rejects this rendering"]},
                                  ensure_ascii=False)
            if '"notes"' in prompt or '"note"' in prompt:
                return json.dumps({"notes": []})
            if "Flagged Terms" in prompt:
                return json.dumps({"decisions": []})
            if '"terms"' in prompt:
                return json.dumps({"terms": []})
            if "### Source Data" in prompt:
                calls["n"] += 1
                lines = COMPLYING_LINES if calls["n"] == 2 else DECLINING_LINES
                arr = json.loads(prompt.rsplit("### Source Data", 1)[1].strip())
                return json.dumps(
                    {"title": "Mock Chapter Title",
                     "lines": [{"i": x["i"], "t": lines[x["i"] - 1]} for x in arr]},
                    ensure_ascii=False)
            if '"recap"' in prompt:
                return json.dumps({"recap": "Mock recap of the story so far."})
            raise AssertionError(f"unroutable prompt (job={job}): {prompt[:120]!r}")

        outcome, out, verify_prompts, exc = run(
            proj, chat, verify_script([]))

        check("B1 the chapter stopped as needs-review",
              exc is None and outcome == "needs-review", f"{outcome} {exc!r}")
        check("B2 the whipsaw FAIL block reached the console",
              "glossary pin contested: '灵石' -> 'spirit stone'" in out
              and "stopped retranslation - marked needs-review" in out, out)
        check("B3 the diagnosis is in the feedback echo",
              out.count("[FAIL] feedback:") >= 1
              and "no retranslation can satisfy both" in out, out)
        check("B4 the loop stopped at the attempt that proved it (no 4th)",
              calls["n"] == 3, f"translator calls={calls['n']}")
        g = glossary.load(proj)
        check("B5 the glossary was not touched",
              [t["source"] for t in g.get("terms", [])] == ["灵石"]
              and not g.get("retired"), json.dumps(g, ensure_ascii=False))
        check("B6 the investigation ran on the two declining attempts only",
              len(verify_prompts) == 2, f"{len(verify_prompts)} prompts")
        events = read_events(proj)
        whipsaws = [e for e in events if e.get("event") == "term_whipsaw"]
        check("B7 the whipsaw event names the contested term and history",
              len(whipsaws) == 1 and whipsaws[0].get("term") == "灵石"
              and whipsaws[0].get("pinned") == "spirit stone"
              and whipsaws[0].get("decline_history") == [["灵石"], [], ["灵石"]],
              str(whipsaws))
        manifest = project.load_manifest(proj)
        entry = project.find_entry(manifest, CHAPTER)
        check("B8 the manifest marks the chapter needs-review",
              entry and entry.get("status") == "needs-review", str(entry))
        ends = [e for e in events if e.get("event") == "chapter_end"]
        check("B9 chapter_end closed as needs-review after 3 attempts",
              ends and ends[0].get("outcome") == "needs-review"
              and ends[0].get("attempts") == 3, str(ends))
        state = json.loads(
            (proj / "draft" / "CHAPTER_0001.state.json").read_text(encoding="utf-8"))
        check("B10 the saved state carries the diagnosis in feedback",
              any("glossary pin contested" in item
                  for item in state.get("feedback", [])),
              str(state.get("feedback")))


def case_c_keep_action_warns_and_leaves_the_glossary() -> None:
    """A confirmation with a keep action: warn only, entry untouched, the
    loop gives up at the cap exactly as it did before this policy existed."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), max_attempts=2)
        chat, _ = reviewer_script(
            [(False, ["the review invented an event"]),
             (False, ["the review invented an event again"])])
        outcome, out, _, exc = run(
            proj, chat, verify_script([
                {"source": "灵石", "chapter": CHAPTER, "mistranslates": True,
                 "action": "keep", "reason": "a narrower fix than deletion"}]))

        check("C1 the chapter gave up as needs-review",
              exc is None and outcome == "needs-review", f"{outcome} {exc!r}")
        check("C2 the keep action warned without retiring",
              "glossary over-binding needs a human decision" in out
              and "retired over-bound term" not in out, out)
        g = glossary.load(proj)
        check("C3 the glossary still carries the entry",
              [t["source"] for t in g.get("terms", [])] == ["灵石"]
              and not g.get("retired"), json.dumps(g, ensure_ascii=False))
        check("C4 no fix-retry grant fired",
              "retrying once with the corrected glossary" not in out, out)


def case_d_fix_retry_granted_past_the_cap() -> None:
    """The grant: a retirement landing on the final attempt's failure buys
    exactly one attempt past max_attempts, once per chapter."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), max_attempts=1)
        chat, _ = reviewer_script(
            [(False, ["the review invented an event"]),
             (False, ["the review invented an event again"])])
        outcome, out, _, exc = run(
            proj, chat, verify_script([
                {"source": "灵石", "chapter": CHAPTER, "mistranslates": True,
                 "action": "retire", "reason": "over-bound mock entry"}]))

        check("D1 the chapter still ends needs-review after the retry failed",
              exc is None and outcome == "needs-review", f"{outcome} {exc!r}")
        check("D2 the grant line reached the console",
              "retrying once with the corrected glossary" in out, out)
        events = read_events(proj)
        ends = [e for e in events if e.get("event") == "chapter_end"]
        check("D3 chapter_end counts both attempts (1 granted + the original)",
              ends and ends[0].get("attempts") == 2, str(ends))
        g = glossary.load(proj)
        check("D4 the retirement stands",
              g.get("terms") == [] and g.get("retired") == ["灵石"],
              json.dumps(g, ensure_ascii=False))


def case_e_whipsaw_exempts_a_just_retired_term() -> None:
    """A whipsaw whose contested term was retired by the same rejection's
    auto-fix does not stop the loop: the constraint is gone, the fix-retry
    owns the outcome."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), max_attempts=1)
        calls = {"n": 0}

        def chat(project_dir, cfg, job, prompt, json_schema=None,
                 max_tokens=None, chapter=None):
            if "verdict" in prompt and '"terms"' not in prompt:
                return json.dumps({"verdict": "FAILURE",
                                   "reasons": ["the review rejects this rendering"]},
                                  ensure_ascii=False)
            if '"notes"' in prompt or '"note"' in prompt:
                return json.dumps({"notes": []})
            if "Flagged Terms" in prompt:
                return json.dumps({"decisions": []})
            if '"terms"' in prompt:
                return json.dumps({"terms": []})
            if "### Source Data" in prompt:
                calls["n"] += 1
                lines = COMPLYING_LINES if calls["n"] == 2 else DECLINING_LINES
                arr = json.loads(prompt.rsplit("### Source Data", 1)[1].strip())
                return json.dumps(
                    {"title": "Mock Chapter Title",
                     "lines": [{"i": x["i"], "t": lines[x["i"] - 1]} for x in arr]},
                    ensure_ascii=False)
            if '"recap"' in prompt:
                return json.dumps({"recap": "Mock recap of the story so far."})
            raise AssertionError(f"unroutable prompt (job={job}): {prompt[:120]!r}")

        outcome, out, _, exc = run(
            proj, chat, verify_script([
                {"source": "灵石", "chapter": CHAPTER, "mistranslates": True,
                 "action": "retire", "reason": "over-bound mock entry"}]))

        check("E1 the retirement dissolved the contradiction: no whipsaw stop",
              "stopped retranslation" not in out, out)
        check("E2 the fix-retry ran (grant line present)",
              "retrying once with the corrected glossary" in out, out)
        check("E3 the chapter ends needs-review after the granted retry failed",
              exc is None and outcome == "needs-review", f"{outcome} {exc!r}")
        events = read_events(proj)
        check("E4 no term_whipsaw event was written",
              not [e for e in events if e.get("event") == "term_whipsaw"],
              str([e.get("event") for e in events]))


def case_f_unit_shapes() -> None:
    """_contested_term's sawtooth contract and the action aggregation in
    _merge/_contamination (keep-wins across batches and chapters)."""
    contested = pipeline._contested_term
    check("F1 decline -> comply -> decline is contested",
          contested([["甲"], [], ["甲"]]) == "甲", "")
    check("F2 decline -> decline has no compliance in between",
          contested([["甲"], ["甲"]]) is None, "")
    check("F3 an unknown attempt (None) breaks no chain but cannot be the "
          "compliance",
          contested([["甲"], None, ["甲"]]) is None, "")
    check("F4 a different term's decline is not a compliance for this one",
          contested([["甲"], [], ["乙"]]) is None, "")
    check("F5 the latest contested term wins",
          contested([["甲"], [], ["甲"], ["乙"], [], ["乙"]]) == "乙", "")
    check("F6 empty history is uncontested",
          contested([]) is None, "")

    merged: dict[tuple[str, str], dict] = {}
    verify._merge(merged, [
        {"source": "甲", "pinned": "A", "chapter": "c1", "mistranslates": True,
         "action": "retire", "reason": "r1"}])
    verify._merge(merged, [
        {"source": "甲", "pinned": "A", "chapter": "c1", "mistranslates": True,
         "action": "keep", "reason": "r2"}])
    check("F7 keep wins over retire across batches",
          merged[("甲", "c1")]["action"] == "keep"
          and merged[("甲", "c1")]["mistranslates"] is True
          and merged[("甲", "c1")]["reason"] == "r1", str(merged))

    records = verify._contamination([
        {"source": "甲", "pinned": "A", "chapter": "c1", "mistranslates": True,
         "action": "retire", "reason": "r1"},
        {"source": "甲", "pinned": "A", "chapter": "c2", "mistranslates": True,
         "action": "retire", "reason": "r2"}])
    check("F8 unanimous retire aggregates to retire",
          records[0]["action"] == "retire", str(records))
    records = verify._contamination([
        {"source": "甲", "pinned": "A", "chapter": "c1", "mistranslates": True,
         "action": "retire", "reason": "r1"},
        {"source": "甲", "pinned": "A", "chapter": "c2", "mistranslates": True,
         "action": "keep", "reason": "r2"}])
    check("F9 one keep demotes the aggregation",
          records[0]["action"] == "keep", str(records))
    records = verify._contamination([
        {"source": "甲", "pinned": "A", "chapter": "c1", "mistranslates": True,
         "reason": "r1"}])
    check("F10 an action-free (book-scan) verdict stays keep",
          records[0]["action"] == "keep", str(records))


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    case_a_confirmed_overbind_retires_and_retry_passes()
    case_b_whipsaw_stops_the_loop()
    case_c_keep_action_warns_and_leaves_the_glossary()
    case_d_fix_retry_granted_past_the_cap()
    case_e_whipsaw_exempts_a_just_retired_term()
    case_f_unit_shapes()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
