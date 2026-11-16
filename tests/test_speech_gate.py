"""The speech gate inside the pipeline: what it refuses, what it records.

    uv run tests/test_speech_gate.py

Drives run_chapter end to end with pipeline._chat stubbed (this repo's
established convention) and asserts the three things the gate is for:

- a chapter whose translation drops a speech the source opens is REFUSED,
  and the gate's feedback names the paragraph and both texts;
- a chapter whose translation only trips a report-tier check is DELIVERED,
  and the finding is recorded where the operator's tools read it;
- the translator is told the source's speech structure, and a run whose
  refusal rate is out of bounds stops instead of grinding on.

Hermetic sandboxes under tempfile.TemporaryDirectory(). Self-contained
PASS/FAIL script (no pytest).
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "requests>=2.31", "openai>=1.60", "anthropic>=1.0"]
# ///
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config, findings, logger, pipeline, project
from lib.errors import PipelineError

PASSED = 0
FAILED: list[str] = []

SOURCE = """---
chapter_number: {n}
chapter_title: "第{n}章 开始"
---
“你听我说。
这件事不能急。
你明白吗？”
他站在门口，没有再往前走。
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def reset() -> None:
    logger._run_path = None


def make_project(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("source", "translated", "draft", "notes"):
        (root / name).mkdir(exist_ok=True)
    (root / "source" / "CHAPTER_0001.md").write_text(body, encoding="utf-8", newline="\n")
    (root / "chapters.json").write_text(
        json.dumps([{"file": "CHAPTER_0001.md", "number": 1, "order": 0,
                     "status": "pending"}], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    block = {"base_url": "http://x", "model": "m", "api_key_env": "K",
             "timeout_s": 5, "max_retries": 1}
    (root / "config.json").write_text(
        json.dumps({"providers": {"translator": [block], "reviewer": [block],
                                  "glossary": [block], "annotator": [block],
                                  "recap": [block], "consensus": [block]},
                    "source_lang": "zh", "target_lang": "en",
                    "auto_build_epub": False, "max_attempts": 1},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


PROMPTS: list[str] = []


def fake_chat(project_dir, cfg, job, prompt, json_schema=None,
              max_tokens=None, chapter=None):
    PROMPTS.append(prompt)
    numbered = [int(n) for n in re.findall(r'"i":\s*(\d+)', prompt)]
    wanted = sorted(set(numbered)) or [1]
    if '"lines"' in prompt and '"title"' in prompt:
        return json.dumps({"title": "Chapter",
                           "lines": [{"i": i, "t": f"t{i}"} for i in wanted]})
    if '"verdict"' in prompt:
        return json.dumps({"verdict": "SUCCESS", "reasons": []})
    if '"notes"' in prompt:
        return json.dumps({"notes": []})
    if '"terms"' in prompt:
        return json.dumps({"terms": []})
    if '"recap"' in prompt:
        return json.dumps({"recap": "r"})
    return "{}"


def run(root: Path) -> str:
    reset()
    PROMPTS.clear()
    pipeline._chat = fake_chat
    cfg = config.load_config(root, SCRIPTS.parent, require_base_url=False)
    return pipeline.run_chapter(root, "CHAPTER_0001.md", cfg)


def state_feedback(root: Path) -> str:
    """The gate feedback the refused chapter kept in its state file."""
    path = root / "draft" / "CHAPTER_0001.state.json"
    if not path.is_file():
        return ""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return " ".join(loaded.get("feedback") or [])


def case_the_translator_is_told() -> None:
    """The prompt carries the source's speech structure."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_project(root, SOURCE.format(n=1))
        run(root)
        check("the prompt carries the speech structure",
              any("open a quotation where the source opens one" in p for p in PROMPTS),
              repr(PROMPTS[:1])[:400])
        check("the structure names the speech's run",
              any("opens at 1" in p and "closes at 3" in p for p in PROMPTS),
              repr(PROMPTS[:1])[:400])


def case_a_dropped_speech_is_refused() -> None:
    """A translation that drops the speech the source opens is refused."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_project(root, SOURCE.format(n=1))
        outcome = run(root)
        check("the chapter is refused", outcome == "needs-review", outcome)
        feedback = state_feedback(root)
        check("the feedback names the paragraph", "Line 1" in feedback, feedback[:200])
        check("the feedback carries both texts",
              "你听我说" in feedback and "t1" in feedback, feedback[:200])


def case_a_report_tier_finding_is_recorded() -> None:
    """A report-tier finding is recorded and the chapter is delivered."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # a source whose only speech is complete within one paragraph: no
        # speech spans paragraphs, so nothing can be refused here
        body = ("---\nchapter_number: 1\nchapter_title: \"第1章 开始\"\n---\n"
                "“你听我说。”\n这件事不能急。\n他站在门口，没有再往前走。\n")
        make_project(root, body)
        pipeline._chat = fake_chat
        # the translation quotes a word where the source has no mark
        original = fake_chat

        def quoting_chat(project_dir, cfg, job, prompt, json_schema=None,
                         max_tokens=None, chapter=None):
            numbered = [int(n) for n in re.findall(r'"i":\s*(\d+)', prompt)]
            wanted = sorted(set(numbered)) or [1]
            if '"lines"' in prompt and '"title"' in prompt:
                lines = []
                for i in wanted:
                    text = f't{i}'
                    if i == 3:
                        text = 'He stood in the "doorway".'
                    lines.append({"i": i, "t": text})
                return json.dumps({"title": "Chapter", "lines": lines})
            return original(project_dir, cfg, job, prompt, json_schema,
                            max_tokens, chapter)

        reset()
        PROMPTS.clear()
        pipeline._chat = quoting_chat
        cfg = config.load_config(root, SCRIPTS.parent, require_base_url=False)
        outcome = pipeline.run_chapter(root, "CHAPTER_0001.md", cfg)
        check("the chapter is delivered", outcome == "translated", outcome)
        records = findings.read_findings(root)
        check("the finding is recorded",
              any(r["check"] == "C4" and r["chapter"] == "CHAPTER_0001.md"
                  for r in records), repr(records))
        check("the record is a report-tier finding",
              all(r["tier"] == "report" for r in records), repr(records))


def case_the_run_stops_when_the_check_misfires() -> None:
    """A chapter whose refusal rate is out of bounds stops the run."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # every paragraph opens a speech the translation never opens
        body = "---\nchapter_title: \"第1章\"\n---\n" + "\n".join(
            f"“第{i}句话" for i in range(1, 21)) + "\n"
        make_project(root, body)
        try:
            run(root)
        except PipelineError as exc:
            check("the run stops with the reason",
                  "refusal" in str(exc).lower() and "bound" in str(exc).lower(),
                  str(exc)[:200])
        else:
            check("the run stops with the reason", False, "no PipelineError")


def main() -> int:
    for case in (case_the_translator_is_told, case_a_dropped_speech_is_refused,
                 case_a_report_tier_finding_is_recorded,
                 case_the_run_stops_when_the_check_misfires):
        case()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
