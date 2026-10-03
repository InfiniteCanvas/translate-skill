"""Tests for lib/logger.py: per-project JSONL run logs and retention.

log_event re-points the module-global run file whenever it sees a different
resolved project_dir, so two projects logged in one process each append into
their OWN logs/ (the old process-global pinned the FIRST project forever);
within one project the run file is created once and appended to, and the
active run always lives in the logged project's logs/. Clearing
logger._run_path (None) -- the reset contract test_cleanup_flow.py relies
on -- must keep working: the next event self-heals into a fresh run for
whatever project logs. Also pins the previously untested helpers:
_command_tag (first non-flag argv word, skipping --project's value in both
spellings, sanitized to [a-z0-9-], truncated to 24, "run" fallback),
_keep_count (config.json's log_llm_keep_runs, clamped >= 0, the DEFAULTS
value on any read failure), and _prune (keeps the NEWEST N llm-*.jsonl by
mtime, deletes the rest, touches nothing else).

Hermetic sandboxes under tempfile.TemporaryDirectory(); logging must never
raise, so failures here show up as missing/empty files, not exceptions.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_logger.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config  # noqa: E402
from lib import logger  # noqa: E402

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


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def reset() -> None:
    """The documented reset contract: only _run_path is cleared (the exact
    poke test_cleanup_flow.py uses) -- log_event must self-heal from that."""
    logger._run_path = None


def events_in(logs_dir: Path) -> list[dict]:
    """Every JSONL event across the run files, filename order."""
    out: list[dict] = []
    for log_path in sorted(logs_dir.glob("llm-*.jsonl")):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def run_name_ok(path: Path) -> bool:
    """llm-YYYYMMDD-HHMMSS-<tag>-<pid>.jsonl for THIS process."""
    return (re.match(r"^llm-\d{8}-\d{6}-.+-\d+\.jsonl$", path.name) is not None
            and path.name.endswith(f"-{os.getpid()}.jsonl"))


# ---------------------------------------------------------------------- cases


def case_1_command_tag() -> None:
    """_command_tag: first non-flag argv word, skipping --project's VALUE in
    both spellings; sanitized to [a-z0-9-], truncated to 24, "run" fallback."""
    cases = [
        (["translate", "--project", "/tmp/p"], "translate"),
        (["--project", "projdir", "translate"], "translate"),
        (["--project=projdir", "autobuild"], "autobuild"),
        (["--verbose", "translate"], "translate"),
        (["--project"], "run"),
        (["Fix_Chapter 1!"], "fixchapter1"),
        (["a" * 30], "a" * 24),
        (["翻译"], "run"),
        ([], "run"),
    ]
    orig = sys.argv
    try:
        for i, (argv, expected) in enumerate(cases):
            sys.argv = ["prog"] + argv
            got = logger._command_tag()
            check(f"1{chr(97 + i)} tag: {argv!r} -> {expected!r}",
                  got == expected, f"got={got!r}")
    finally:
        sys.argv = orig


def case_2_keep_count() -> None:
    """_keep_count: config.json's log_llm_keep_runs (int-coerced, clamped
    >= 0); the DEFAULTS value on a missing/corrupt config."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check("2a keep: missing config.json -> the default",
              logger._keep_count(root) == config.DEFAULTS["log_llm_keep_runs"], "")
        write_lf(root / "config.json", json.dumps({"log_llm_keep_runs": 2}))
        check("2b keep: explicit value honored", logger._keep_count(root) == 2, "")
        write_lf(root / "config.json", json.dumps({"log_llm_keep_runs": -3}))
        check("2c keep: negative clamped to 0", logger._keep_count(root) == 0, "")
        write_lf(root / "config.json", json.dumps({"log_llm_keep_runs": "3"}))
        check("2d keep: numeric string coerced", logger._keep_count(root) == 3, "")
        write_lf(root / "config.json", "{not json")
        check("2e keep: corrupt config -> the default",
              logger._keep_count(root) == config.DEFAULTS["log_llm_keep_runs"], "")


def case_3_prune() -> None:
    """_prune keeps the NEWEST N llm-*.jsonl runs by mtime and deletes the
    oldest; non-run files and the keep=0 wipe are pinned too."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "logs"
        base.mkdir()
        for i in range(5):
            p = base / f"llm-{i:05d}.jsonl"
            p.write_text("{}\n", encoding="utf-8")
            os.utime(p, (1000.0 + i * 100, 1000.0 + i * 100))  # oldest first
        (base / "other.txt").write_text("keep me", encoding="utf-8")

        logger._prune(base, 2)
        left = sorted(p.name for p in base.glob("llm-*.jsonl"))
        check("3a prune: the two NEWEST runs survive (llm-00003, llm-00004)",
              left == ["llm-00003.jsonl", "llm-00004.jsonl"], f"left={left}")
        check("3b prune: non-run files untouched",
              (base / "other.txt").read_text(encoding="utf-8") == "keep me", "")

        logger._prune(base, 0)
        check("3c prune: keep=0 wipes every run",
              list(base.glob("llm-*.jsonl")) == [], "")

        no_raise = True
        try:
            logger._prune(base / "missing", 2)
        except OSError:
            no_raise = False
        check("3d prune: missing base dir is a no-op", no_raise, "")


def case_4_two_projects() -> None:
    """Two project_dirs in one process: each appends into its OWN logs/ (no
    cross-project bleed), same-project events share one run file, the active
    run always lives in the logged project's logs/, and the reset contract
    (clear _run_path) keeps logging working for the next project."""
    with tempfile.TemporaryDirectory() as td:
        proj_a = Path(td) / "proj-a"
        proj_b = Path(td) / "proj-b"
        reset()
        logger.log_event(proj_a, {"event": "a1"})
        logger.log_event(proj_b, {"event": "b1"})
        logger.log_event(proj_a, {"event": "a2"})
        logger.log_event(proj_b, {"event": "b2"})

        a_events = events_in(proj_a / "logs")
        b_events = events_in(proj_b / "logs")
        check("4a projects: A's logs hold exactly A's events, in order",
              [e["event"] for e in a_events] == ["a1", "a2"], f"{a_events}")
        check("4b projects: B's logs hold exactly B's events, in order",
              [e["event"] for e in b_events] == ["b1", "b2"], f"{b_events}")
        check("4c projects: run filenames match llm-stamp-tag-pid.jsonl",
              all(run_name_ok(p) for p in (proj_a / "logs").glob("llm-*.jsonl"))
              and all(run_name_ok(p) for p in (proj_b / "logs").glob("llm-*.jsonl")),
              "")
        check("4d projects: every event carries an ISO ts",
              all("ts" in e for e in a_events + b_events), "")

        # Same project: the run file is created once and appended to.
        before = logger._run_path
        b_files = sorted((proj_b / "logs").glob("llm-*.jsonl"))
        logger.log_event(proj_b, {"event": "b3"})
        check("4e projects: same project appends to the SAME run file",
              logger._run_path == before
              and sorted((proj_b / "logs").glob("llm-*.jsonl")) == b_files
              and [e["event"] for e in events_in(proj_b / "logs")]
              == ["b1", "b2", "b3"],
              f"run={logger._run_path}")
        check("4f projects: the active run file lives in the logged project's logs/",
              logger._run_path is not None
              and logger._run_path.parent == proj_b.resolve() / "logs",
              f"run={logger._run_path}")

        # The reset contract: _run_path = None, then the next event still
        # lands in the right project (a same-second rerun may reuse the
        # filename, so only the project and the events are pinned).
        reset()
        logger.log_event(proj_b, {"event": "b4"})
        check("4g reset: clearing _run_path re-opens a run for the logged project",
              logger._run_path is not None
              and logger._run_path.parent == proj_b.resolve() / "logs"
              and [e["event"] for e in events_in(proj_b / "logs")]
              == ["b1", "b2", "b3", "b4"],
              f"run={logger._run_path}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_command_tag()
    case_2_keep_count()
    case_3_prune()
    case_4_two_projects()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
