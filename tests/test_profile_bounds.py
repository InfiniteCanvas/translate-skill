"""Tests for profile.generate_profile's sampling and char-budget bounds.

generate_profile samples up to `sample_chapters` random source chapters,
concatenates their bodies until roughly `sample_chars` characters are
collected (truncating the last one at a line boundary when a newline
survives past the midpoint), fills templates/style_profile.md, and asks
the "profile" provider. The sample text is the prompt material the whole
style profile is judged on, so its boundaries are pinned here:

- the sampling cap: with a deterministic random.sample stub, exactly
  `sample_chapters` chapters are sampled (the unpicked markers never
  appear in the prompt); the cap clamps BOTH ways -- above the available
  count every chapter is sampled, and a zero/negative request still
  samples exactly one (the max(1, ...) clamp);
- the char budget: a chapter longer than the remaining budget is
  truncated -- at a line boundary when the cut's last newline sits past
  the midpoint, else exactly at the budget -- and once the budget is
  exhausted the loop breaks (later chapters never appear); the embedded
  sample's length always respects the budget constant;
- the empty/underflow cases raise ProfileError before any LLM call: no
  source chapters at all, and chapters whose bodies are all empty.

The LLM boundary is faked the suite's way: profile.client.chat is swapped
for a canned responder that records the prompt (the sample is extracted
between the template's stable [Source Sample] / [Task] markers), and
random.sample is swapped only where determinism is load-bearing.

Self-contained PASS/FAIL script (no pytest). lib.profile transitively
imports the whole lib package -- run via uv (deps declared inline below):

    uv run tests/test_profile_bounds.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file
# (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config  # noqa: E402
from lib import profile as profile_mod  # noqa: E402

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


def make_project(root: Path, name: str, bodies: dict[str, str] | None = None,
                 with_source: bool = True) -> Path:
    """Profile-shaped fixture (test_cleanup_flow's style): source/ chapters
    written verbatim (no frontmatter, no trailing newline), an accurate
    chapters.json manifest, and config.json {"providers": {}} (the canned
    chat never touches a provider). with_source=False builds the no-source
    underflow fixture."""
    proj = root / name
    proj.mkdir()
    if with_source:
        (proj / "source").mkdir()
    for fname, body in (bodies or {}).items():
        (proj / "source" / fname).write_text(body, encoding="utf-8",
                                             newline="\n")
    (proj / "chapters.json").write_text(
        json.dumps([{"file": f, "number": i + 1, "suffix": "", "order": i,
                     "status": "pending"}
                    for i, f in enumerate(sorted(bodies or {}))],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (proj / "config.json").write_text(
        json.dumps({"providers": {}}, indent=2) + "\n", encoding="utf-8")
    return proj


def extract_sample(prompt: str) -> str:
    """The {{sample_text}} the filled template embedded, between its
    stable [Source Sample] / [Task] markers."""
    return prompt.split("[Source Sample]\n\n", 1)[1].split("\n\n[Task]", 1)[0]


def run_generate(proj: Path, cfg: dict, sample_chapters: int,
                 sample_chars: int) -> tuple[dict | None, str, Exception | None]:
    """generate_profile with the canned chat installed (records the prompt);
    returns (result, prompt, exc)."""
    captured: dict = {}
    orig_chat = profile_mod.client.chat

    def fake_chat(provider_cfg, prompt, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({"style_summary": "Stub summary.",
                           "background": "Stub background."},
                          ensure_ascii=False)

    profile_mod.client.chat = fake_chat
    try:
        result, exc = None, None
        try:
            result = profile_mod.generate_profile(
                proj, cfg, sample_chapters, sample_chars)
        except Exception as caught:  # noqa: BLE001 - the caller asserts on it
            exc = caught
    finally:
        profile_mod.client.chat = orig_chat
    return result, captured.get("prompt", ""), exc


def case_sampling_cap() -> None:
    """Exactly sample_chapters chapters are sampled (deterministic via a
    random.sample stub picking the head), the unpicked markers never reach
    the prompt, and the cap clamps in both directions."""
    bodies = {f"Chapter_000{i}.md": f"第{i}章采样标记行。"
              for i in range(1, 7)}

    # A: cap 3 of 6 available, deterministic stub -> chapters 1-3 only.
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", bodies)
        cfg = config.load_config(proj)
        orig_sample = profile_mod.random.sample
        profile_mod.random.sample = lambda pop, k: list(pop)[:k]
        try:
            _result, prompt, exc = run_generate(
                proj, cfg, sample_chapters=3, sample_chars=100_000)
        finally:
            profile_mod.random.sample = orig_sample
        check("1a cap: generation succeeds", exc is None, f"exc={exc!r}")
        sample = extract_sample(prompt)
        check("1b cap: exactly the capped chapters sampled (1-3 present)",
              all(f"第{i}章采样标记行。" in sample for i in (1, 2, 3)),
              f"sample={sample!r}")
        check("1c cap: unpicked chapters (4-6) never appear",
              all(f"第{i}章采样标记行。" not in sample for i in (4, 5, 6)),
              f"sample={sample!r}")
        check("1d cap: every sampled chapter is within the range "
              "(one line each, all present)",
              sample == "第1章采样标记行。\n\n第2章采样标记行。\n\n"
                        "第3章采样标记行。",
              f"sample={sample!r}")

    # B: cap 10 above the 3 available -> the clamp samples every chapter
    #    (real random.sample: all are picked, no determinism needed).
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj",
                            {f"Chapter_000{i}.md": f"第{i}章采样标记行。"
                             for i in range(1, 4)})
        cfg = config.load_config(proj)
        _result, prompt, exc = run_generate(
            proj, cfg, sample_chapters=10, sample_chars=100_000)
        check("1e clamp up: generation succeeds", exc is None, f"exc={exc!r}")
        sample = extract_sample(prompt)
        check("1f clamp up: all 3 available chapters sampled",
              all(f"第{i}章采样标记行。" in sample for i in (1, 2, 3)),
              f"sample={sample!r}")

    # C: a zero request still samples exactly one chapter (max(1, ...)).
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj",
                            {f"Chapter_000{i}.md": f"第{i}章采样标记行。"
                             for i in range(1, 4)})
        cfg = config.load_config(proj)
        _result, prompt, exc = run_generate(
            proj, cfg, sample_chapters=0, sample_chars=100_000)
        check("1g clamp down: generation succeeds", exc is None,
              f"exc={exc!r}")
        sample = extract_sample(prompt)
        marker = "采样标记行。"
        check("1h clamp down: exactly one chapter sampled",
              sample.count(marker) == 1
              and sample in [f"第{i}章采样标记行。" for i in (1, 2, 3)],
              f"sample={sample!r}")


def case_char_budget() -> None:
    """The accumulated sample respects the char budget: a too-long chapter
    is truncated at a line boundary when the cut's last newline sits past
    the midpoint, exactly at the budget when it does not, and the loop
    breaks once the budget is exhausted."""

    # A: line-boundary truncation -- 6 lines of 10 A's, budget 25: the cut
    #    lands at 25 with a newline at 21 (past the 12 midpoint), so the
    #    sample ends after the second full line.
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", {
            "Chapter_0001.md": "\n".join(["A" * 10] * 6),
        })
        cfg = config.load_config(proj)
        _result, prompt, exc = run_generate(
            proj, cfg, sample_chapters=1, sample_chars=25)
        check("2a budget: generation succeeds", exc is None, f"exc={exc!r}")
        sample = extract_sample(prompt)
        check("2b budget: truncated at the line boundary (two full lines)",
              sample == "A" * 10 + "\n" + "A" * 10, f"sample={sample!r}")
        check("2c budget: the sample length respects the budget constant",
              len(sample) <= 25, f"len={len(sample)}")
        check("2d budget: the partial third line never appears",
              "A" * 10 + "\n" + "A" * 10 + "\n" not in sample,
              f"sample={sample!r}")

    # B: no newline past the midpoint (single 40-char line, budget 25) ->
    #    truncated exactly at the budget; and the NEXT chapter never
    #    appears because the loop breaks at remaining <= 0. The sample
    #    order is pinned (head-first stub) so chapter 1 fills the budget.
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj", {
            "Chapter_0001.md": "A" * 40,
            "Chapter_0002.md": "B" * 40,
        })
        cfg = config.load_config(proj)
        orig_sample = profile_mod.random.sample
        profile_mod.random.sample = lambda pop, k: list(pop)[:k]
        try:
            _result, prompt, exc = run_generate(
                proj, cfg, sample_chapters=2, sample_chars=25)
        finally:
            profile_mod.random.sample = orig_sample
        check("2e budget break: generation succeeds", exc is None,
              f"exc={exc!r}")
        sample = extract_sample(prompt)
        check("2f budget break: exactly the budget of chars from chapter 1",
              sample == "A" * 25, f"sample={sample!r}")
        check("2g budget break: chapter 2 is absent once the budget is "
              "exhausted",
              "B" not in sample, f"sample={sample!r}")
        check("2h budget break: the length never exceeds the budget",
              len(sample) <= 25, f"len={len(sample)}")


def case_underflow_errors() -> None:
    """The empty/underflow cases raise ProfileError before any LLM call:
    no source chapters at all, and chapters whose bodies are all empty."""
    # A: no source/ directory -> no chapters found.
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "no-source", with_source=False)
        cfg = config.load_config(proj)
        result, _prompt, exc = run_generate(
            proj, cfg, sample_chapters=3, sample_chars=4000)
        check("3a underflow: no chapters -> ProfileError",
              result is None and isinstance(exc, profile_mod.ProfileError),
              f"result={result} exc={exc!r}")
        check("3b underflow: the error names the empty source scan",
              exc is not None
              and "no source chapters found in source/" in str(exc),
              f"exc={exc!r}")

    # B: chapters exist but every body is empty -> no sample text.
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "empty-bodies", {
            "Chapter_0001.md": "",
            "Chapter_0002.md": "  \n  ",
        })
        cfg = config.load_config(proj)
        result, _prompt, exc = run_generate(
            proj, cfg, sample_chapters=3, sample_chars=4000)
        check("3c underflow: empty bodies -> ProfileError",
              result is None and isinstance(exc, profile_mod.ProfileError),
              f"result={result} exc={exc!r}")
        check("3d underflow: the error names the textless sample",
              exc is not None
              and "sampled chapters contained no text" in str(exc),
              f"exc={exc!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_sampling_cap()
    case_char_budget()
    case_underflow_errors()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
