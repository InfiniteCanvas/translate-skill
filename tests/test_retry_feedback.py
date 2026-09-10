"""Tests for gate-rejection retry feedback: pipeline._feedback_section and
the state["rejected"] snapshot consumed by run_chapter's TRANSLATE prompt.

When a gate (FAITH) rejects an attempt that produced a full translation,
run_chapter snapshots it as state["rejected"] = list(lines) in the failure
handler before looping back to TRANSLATE. The TRANSLATE prompt's
{{feedback_section}} is rendered by the module-level _feedback_section:

- no feedback at all -> "" (attempt 1's prompt is clean);
- feedback without a rejected translation (first attempt failed in
  TRANSLATE itself, or rejected_lines empty/None) -> the bullets-only
  "from scratch" NOTE;
- feedback plus a rejected translation -> the same NOTE with the
  "fix the flagged problems" wording, then the feedback bullets and a
  [Rejected Previous Attempt] block holding the rejected lines rendered
  in the same numbered-JSON protocol as the source data
  ([{"i": <1-based global line number>, "t": <line>}, ...],
  ensure_ascii=False), sliced to the chunk's half-open source range
  [lo, hi) so a chunked retry sees exactly its own rejected lines.

Covered: the unit table above (empty, bullets-only for None and [],
rejected branch with the exact numbered JSON pinning 1-based i and
unescaped CJK, chunk slicing preserving global i including the empty
slice which still renders the marker); the give-up integration
(max_attempts 2, two FAITH failures -> "needs-review", the state file
survives with rejected == the last translation and feedback == both
reasons in order, the second translate prompt carries the rejected
section, the manifest entry is marked needs-review); and the
retry-then-success integration (default max_attempts 3, FAILURE then
SUCCESS -> "translated" with the exact 6-call sequence translate ->
verdict -> translate -> verdict -> terms -> notes, where attempt 1's
translate prompt has no rejected NOTE and attempt 2's carries the NOTE,
the feedback bullet, the marker, and the rejected line text; the
translated chapter lands and the state file is cleaned up).

Mechanics mirror the sibling suites: every fixture lives in a
TemporaryDirectory sandbox built with test_sync's write_source pattern
(one chapter under source/, plain body lines without frontmatter, plus
an accurate chapters.json manifest, config.json {"providers": {}}, an
empty glossary.json, and the draft/ + translated/ dirs run_chapter
persists state and output into), and all model calls go through a
monkeypatched
pipeline._chat whose responses are sniffed from the prompt content
exactly like tests/mock_server.py: "verdict" -> FAITH reviewer,
'"terms"' -> glossary expansion, '"notes"'/'"note"' -> note generation,
anything else -> the TRANSLATE call. pipeline.py imports the whole lib
package (requests, ebooklib, pillow, pyyaml).

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_retry_feedback.py
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

# lib/ lives at novel-translator/scripts relative to this file
# (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config, pipeline, project  # noqa: E402

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


def sniff(prompt: str) -> str:
    """Which pipeline role a prompt targets (mock_server.py convention)."""
    if "verdict" in prompt:
        return "verdict"
    if '"terms"' in prompt:
        return "terms"
    if '"notes"' in prompt or '"note"' in prompt:
        return "notes"
    return "translate"


def make_fake_chat(calls: list[dict], verdicts: list[tuple[str, list[str]]]):
    """pipeline._chat replacement: records {"job", "prompt"} per call and
    answers from the scripted verdict queue / canned role responses."""

    def fake(project_dir, cfg, job, prompt, json_schema=None, max_tokens=None):
        calls.append({"job": job, "prompt": prompt})
        if "verdict" in prompt:
            verdict, reasons = verdicts.pop(0)
            return json.dumps({"verdict": verdict, "reasons": reasons},
                              ensure_ascii=False)
        if '"terms"' in prompt:
            return json.dumps({"terms": []})
        if '"notes"' in prompt or '"note"' in prompt:
            return json.dumps({"notes": []})
        return json.dumps(
            {"title": "Mock Title",
             "lines": [{"i": i, "t": f"Translated line {i}."}
                       for i in range(1, 4)]},
            ensure_ascii=False)

    return fake


def write_source(root: Path, name: str, text: str) -> None:
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    with open(source / name, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# One 3-line chapter, plain body lines without frontmatter (the shape the
# sibling suites' BODIES use): a 3-line source keeps the default
# translate_max_output_tokens far from chunking, so every chapter is one
# translate call and [lo, hi) is the whole chapter. No trailing newline:
# read_chapter's no-frontmatter path returns the file text verbatim, so a
# trailing "\n" would split into a phantom 4th empty source line.
BODIES = {
    "Chapter_0001.md": "第一行。\n第二行。\n第三行。",
}
MANIFEST = [
    {"file": fname, "number": i + 1, "suffix": "", "order": i, "status": "pending"}
    for i, fname in enumerate(sorted(BODIES))
]


def make_project(root: Path, name: str, cfg_extra: dict | None = None) -> Path:
    """Minimal project: the fixture chapter under source/, an accurate
    chapters.json manifest, config.json {"providers": {}} + cfg_extra, an
    empty glossary.json, and the draft/ + translated/ dirs the pipeline
    persists state and output into."""
    proj = root / name
    proj.mkdir()
    for fname, body in BODIES.items():
        write_source(proj, fname, body)
    (proj / "chapters.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cfg = {"providers": {}}
    cfg.update(cfg_extra or {})
    (proj / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proj / "glossary.json").write_text(
        json.dumps({"terms": []}, ensure_ascii=False) + "\n", encoding="utf-8")
    (proj / "draft").mkdir()
    (proj / "translated").mkdir()
    return proj


# ---------------------------------------------------------------------- cases


def case_1_unit_empty() -> None:
    """No feedback -> the section is empty regardless of a rejected
    translation: attempt 1's TRANSLATE prompt must stay clean."""
    check("1a unit: no feedback, no rejected -> ''",
          pipeline._feedback_section([], None) == "",
          f"got {pipeline._feedback_section([], None)!r}")
    check("1b unit: no feedback, rejected present -> still ''",
          pipeline._feedback_section([], ["x"]) == "",
          f"got {pipeline._feedback_section([], ['x'])!r}")


def case_2_unit_bullets_only() -> None:
    """Feedback without a rejected translation (rejected_lines None or [])
    renders the exact bullets-only 'from scratch' NOTE, never the marker."""
    expected = (
        "NOTE: A previous translation attempt was rejected. "
        "Address every issue below and translate the entire chapter "
        "again from scratch:\n- Line 2: wrong term"
    )
    got = pipeline._feedback_section(["Line 2: wrong term"], None)
    check("2a unit: rejected None -> exact bullets-only string",
          got == expected, f"got {got!r}")
    check("2b unit: bullets-only has no rejected marker",
          "[Rejected Previous Attempt]" not in got, f"got {got!r}")
    got2 = pipeline._feedback_section(["Line 2: wrong term"], [])
    check("2c unit: rejected [] -> same bullets-only string",
          got2 == expected, f"got {got2!r}")


def case_3_unit_rejected_branch() -> None:
    """With a rejected translation the section carries the bullet, the
    [Rejected Previous Attempt] marker, and the rejected lines as numbered
    JSON with 1-based i and unescaped CJK (ensure_ascii=False)."""
    got = pipeline._feedback_section(
        ["Line 2: wrong term"], ["旧译一", "旧译二"])
    check("3a unit: feedback bullet present", "- Line 2: wrong term" in got,
          f"got {got!r}")
    check("3b unit: [Rejected Previous Attempt] marker present",
          "[Rejected Previous Attempt]" in got, f"got {got!r}")
    check("3c unit: exact numbered JSON (1-based i, ensure_ascii=False)",
          '[{"i": 1, "t": "旧译一"}, {"i": 2, "t": "旧译二"}]' in got,
          f"got {got!r}")


def case_4_unit_chunk_slicing() -> None:
    """[lo, hi) slices the rejected lines but keeps global 1-based i: a
    chunked retry sees exactly its own slice; an empty slice still renders
    the marker with an empty array."""
    got = pipeline._feedback_section(
        ["fb"], ["a", "b", "c", "d"], lo=2, hi=4)
    check("4a unit: lo=2 hi=4 -> lines c/d keep global i 3/4",
          '[{"i": 3, "t": "c"}, {"i": 4, "t": "d"}]' in got, f"got {got!r}")
    got_all = pipeline._feedback_section(["fb"], ["a", "b", "c", "d"])
    check("4b unit: lo=0 hi=None -> all four lines, i 1..4",
          '[{"i": 1, "t": "a"}, {"i": 2, "t": "b"}, '
          '{"i": 3, "t": "c"}, {"i": 4, "t": "d"}]' in got_all,
          f"got {got_all!r}")
    got_empty = pipeline._feedback_section(["fb"], ["a", "b", "c", "d"], lo=4)
    check("4c unit: empty slice still renders the marker with []",
          "[Rejected Previous Attempt]" in got_empty and got_empty.endswith("[]"),
          f"got {got_empty!r}")


def case_5_retry_then_success() -> None:
    """FAILURE then SUCCESS under the default max_attempts 3: the outcome is
    "translated", the call sequence is exactly translate -> verdict ->
    translate -> verdict -> terms -> notes, attempt 1's translate prompt is
    clean while attempt 2's carries the NOTE, the feedback bullet, the
    marker, and the rejected line text, and the artifacts settle (chapter
    written, state file removed)."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        calls: list[dict] = []
        verdicts = [("FAILURE", ["Line 2: wrong term"]), ("SUCCESS", [])]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, verdicts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "Chapter_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("5a retry: run_chapter returns 'translated'",
              exc is None and outcome == "translated", f"exc={exc!r}")
        kinds = [sniff(c["prompt"]) for c in calls]
        check("5b retry: exactly 6 model calls",
              len(calls) == 6, f"kinds={kinds}")
        check("5c retry: sequence translate->verdict->translate->verdict"
              "->terms->notes",
              kinds == ["translate", "verdict", "translate", "verdict",
                        "terms", "notes"], f"kinds={kinds}")
        check("5d retry: verdict called twice",
              kinds.count("verdict") == 2, f"kinds={kinds}")
        check("5e retry: attempt 1 translate prompt has no rejected NOTE",
              "NOTE: A previous translation attempt was rejected"
              not in calls[0]["prompt"], f"prompt={calls[0]['prompt']!r}")
        p2 = calls[2]["prompt"]
        check("5f retry: attempt 2 translate prompt has the rejected NOTE",
              "NOTE: A previous translation attempt was rejected" in p2,
              f"prompt={p2!r}")
        check("5g retry: attempt 2 prompt carries the feedback bullet",
              "- Line 2: wrong term" in p2, f"prompt={p2!r}")
        check("5h retry: attempt 2 prompt carries the rejected marker",
              "[Rejected Previous Attempt]" in p2, f"prompt={p2!r}")
        check("5i retry: attempt 2 prompt reproduces the rejected line",
              "Translated line 2." in p2, f"prompt={p2!r}")
        check("5j retry: translated chapter exists",
              (proj / "translated" / "Chapter_0001.md").is_file())
        check("5k retry: draft state file removed on success",
              not (proj / "draft" / "Chapter_0001.state.json").exists())


def case_6_give_up() -> None:
    """Two FAITH failures under max_attempts 2: the outcome is
    "needs-review", the state file survives holding rejected == the last
    translation and both feedback reasons in order, the second translate
    prompt carries the rejected section, and the manifest entry is marked
    needs-review."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", {"max_attempts": 2})
        calls: list[dict] = []
        verdicts = [("FAILURE", ["reason one"]), ("FAILURE", ["reason two"])]
        orig = pipeline._chat
        pipeline._chat = make_fake_chat(calls, verdicts)
        try:
            outcome, _out, exc = capture(
                pipeline.run_chapter, proj, "Chapter_0001.md",
                config.load_config(proj))
        finally:
            pipeline._chat = orig
        check("6a give-up: run_chapter returns 'needs-review'",
              exc is None and outcome == "needs-review", f"exc={exc!r}")
        state_path = proj / "draft" / "Chapter_0001.state.json"
        check("6b give-up: draft state file still exists", state_path.is_file())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        check("6c give-up: state rejected == the 3 translated lines",
              state["rejected"] == ["Translated line 1.", "Translated line 2.",
                                    "Translated line 3."],
              f"rejected={state.get('rejected')!r}")
        check("6d give-up: state feedback keeps both reasons in order",
              state["feedback"] == ["reason one", "reason two"],
              f"feedback={state.get('feedback')!r}")
        translates = [c["prompt"] for c in calls if sniff(c["prompt"]) == "translate"]
        check("6e give-up: both translate prompts recorded, second carries "
              "the rejected section",
              len(translates) == 2
              and "NOTE: A previous translation attempt was rejected"
              in translates[1]
              and "[Rejected Previous Attempt]" in translates[1],
              f"n={len(translates)} p2={translates[1] if len(translates) > 1 else None!r}")
        entry = project.find_entry(project.load_manifest(proj), "Chapter_0001.md")
        check("6f give-up: manifest entry status needs-review",
              entry is not None and entry.get("status") == "needs-review",
              f"entry={entry!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_unit_empty()
    case_2_unit_bullets_only()
    case_3_unit_rejected_branch()
    case_4_unit_chunk_slicing()
    case_5_retry_then_success()
    case_6_give_up()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
