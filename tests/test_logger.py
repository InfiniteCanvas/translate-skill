"""Tests for lib/logger.py: the two-tier run logs, routing, gates and retention.

Tier 1 is the orchestration timeline at logs/run-<run_id>.jsonl and never
carries a body; tier 2 is one file per chapter at
logs/chapters/<stem>/run-<run_id>.jsonl, with chapter-less model IO falling
into logs/project/. Each event routes by name (default tier 1) and is gated
against flags resolved ONCE per invocation.

run_id is computed once per resolved project, so a tier-1 file and every
chapter's tier-2 file of one run share it and the tiers join. Two projects
logged in one process each keep their own files, and the active run always
lives in the logged project's logs/. Clearing logger._run_path -- the reset
contract test_cleanup_flow.py and test_consensus.py rely on -- must keep
working for BOTH tiers: the next event clears every cached path and the
counters and self-heals into fresh runs.

Also pins: _command_tag (first non-flag argv word, skipping --project's value
in both spellings, sanitized to [a-z0-9-], truncated to 24, "run" fallback),
_keep_count (config.json's log_llm_keep_runs, clamped >= 0, the DEFAULTS value
on any read failure), and _prune (keeps the NEWEST N run-*.jsonl by mtime,
never index.jsonl / report.md / epub-build.log / legacy llm-*.jsonl).

Hermetic sandboxes under tempfile.TemporaryDirectory(); logging must never
raise, so failures here show up as missing/empty files, not exceptions.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_logger.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config
from lib import logger
from lib import project

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


def write_cfg(root: Path, **flags) -> None:
    write_lf(root / "config.json", json.dumps(flags))


def reset() -> None:
    """The documented reset contract: only _run_path is cleared (the exact
    poke test_cleanup_flow.py uses) -- log_event must self-heal from that."""
    logger._run_path = None


def run_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("run-*.jsonl") if p.is_file())


def events_in(directory: Path) -> list[dict]:
    """Every JSONL event across one bucket's run files, filename order."""
    out: list[dict] = []
    for log_path in run_files(directory):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def tier2_dir(root: Path, stem: str) -> Path:
    return root / "logs" / "chapters" / stem


def run_name_ok(path: Path) -> bool:
    """run-YYYYMMDD-HHMMSS-<tag>-<pid>.jsonl for THIS process."""
    return (re.match(r"^run-\d{8}-\d{6}-.+-\d+\.jsonl$", path.name) is not None
            and path.name.endswith(f"-{os.getpid()}.jsonl"))


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


def case_3_routing() -> None:
    """Every event routes to exactly one tier; the default is tier 1; every
    line carries ts and run_id; one run_id spans all three buckets."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        logger.log_event(root, {"event": "run_start", "command": "translate"})
        logger.log_event(root, {"event": "chapter_start",
                                "chapter": "CHAPTER_0007"})
        logger.log_event(root, {"event": "llm_request", "chapter": "CHAPTER_0007",
                                "prompt": "hi", "call_id": "a"})
        logger.log_event(root, {"event": "llm_response", "chapter": "CHAPTER_0007",
                                "response": "yo", "call_id": "a"})
        logger.log_event(root, {"event": "chunk", "chapter": "CHAPTER_0007"})
        logger.log_event(root, {"event": "totally_unknown_event"})
        logger.log_event(root, {"event": "llm_request", "prompt": "p",
                                "call_id": "b"})

        tier1 = events_in(root / "logs")
        chapter = events_in(tier2_dir(root, "CHAPTER_0007"))
        project_bucket = events_in(root / "logs" / "project")
        check("3a routing: tier 1 holds the structural + unknown event",
              [e["event"] for e in tier1]
              == ["run_start", "chapter_start", "totally_unknown_event"],
              f"{[e['event'] for e in tier1]}")
        check("3b routing: tier 2 holds only model IO for the chapter",
              [e["event"] for e in chapter]
              == ["llm_request", "llm_response", "chunk"],
              f"{[e['event'] for e in chapter]}")
        check("3c routing: chapter-less tier-2 event lands in the project bucket",
              [e["event"] for e in project_bucket] == ["llm_request"],
              f"{[e['event'] for e in project_bucket]}")
        check("3d routing: every line carries ts and run_id",
              all("ts" in e and "run_id" in e
                  for e in tier1 + chapter + project_bucket), "")
        ids = {e["run_id"] for e in tier1 + chapter + project_bucket}
        check("3e routing: ONE run_id spans the tier-1 and chapter files",
              len(ids) == 1, f"{ids}")
        check("3f routing: the chapter directory is the file stem",
              tier2_dir(root, "CHAPTER_0007").is_dir(), "")
        check("3g routing: tier-1 file is run-<run_id>.jsonl",
              [p.name for p in run_files(root / "logs")]
              == [f"run-{next(iter(ids))}.jsonl"], "")
        check("3h routing: tier 1 carries no body key",
              not any("prompt" in e or "response" in e for e in tier1), "")
        check("3i routing: run files are named run-stamp-tag-pid.jsonl",
              all(run_name_ok(p)
                  for p in run_files(root / "logs")
                  + run_files(tier2_dir(root, "CHAPTER_0007"))
                  + run_files(root / "logs" / "project")), "")


def case_4_stem_keying() -> None:
    """The chapter key is the file stem, verbatim, with no sanitizing.

    Since the all-caps CHAPTER_NNNN.md rule, every name discover() admits
    already ends in a single unique stem, so the stem map is injective over
    the reachable set by construction. The scheme's only residual merge would
    be a case/extension variant, and the case-sensitive CHAPTER_RE does not
    admit those -- nor do they coexist on a case-insensitive filesystem."""
    admitted = ["CHAPTER_0042.md", "CHAPTER_0001.md", "CHAPTER_0043.md"]
    lookalikes = ["Chapter_4: the start.md", "0007.md", "CHAPTER_7.md",
                  "Chapter_0007.md", "CHAPTER_0042.MD", "chapter_0042.md",
                  "CHAPTER_0042a.md", "CHAPTER_00442.md"]
    matched = [n for n in admitted + lookalikes if project.CHAPTER_RE.match(n)]
    check("4a stem: CHAPTER_RE admits exactly the canonical all-caps names",
          matched == admitted, f"{matched}")
    keys = [logger._chapter_key(n) for n in matched]
    check("4b stem: the stem scheme is injective over admitted names",
          len(set(keys)) == len(keys), f"{keys}")
    check("4c stem: the key is the bare stem",
          keys[0] == "CHAPTER_0042", f"{keys[0]}")
    check("4d stem: passing the stem itself is idempotent",
          logger._chapter_key("CHAPTER_0042") == "CHAPTER_0042", "")
    check("4e stem: no chapter means the project bucket",
          logger._chapter_key(None) is None
          and logger._chapter_key("") is None, "")
    check("4f stem: every admitted name is already filesystem-safe",
          all(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", k) for k in keys), f"{keys}")
    check("4g stem: the stem matches the draft/ and notes/ artifact key",
          keys[0] == Path(admitted[0]).stem, "")


def case_5_prompt_bodies() -> None:
    """log_prompt_bodies:false drops the text, keeps the call accountable,
    and records the character counts instead."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_prompt_bodies=False)
        reset()
        logger.log_event(root, {"event": "llm_request", "chapter": "CHAPTER_0001",
                                "prompt": "x" * 17, "call_id": "c",
                                "model": "m"})
        logger.log_event(root, {"event": "llm_response", "chapter": "CHAPTER_0001",
                                "response": "y" * 9, "call_id": "c",
                                "finish_reason": "stop",
                                "usage": {"total_tokens": 5},
                                "elapsed_s": 1.5, "error": None})
        events = events_in(tier2_dir(root, "CHAPTER_0001"))
        req, resp = events[0], events[1]
        check("5a bodies: prompt replaced by prompt_chars",
              req.get("prompt_chars") == 17 and "prompt" not in req, f"{req}")
        check("5b bodies: response replaced by response_chars",
              resp.get("response_chars") == 9 and "response" not in resp, f"{resp}")
        check("5c bodies: the rest of the line is untouched",
              resp.get("finish_reason") == "stop"
              and resp.get("usage") == {"total_tokens": 5}
              and resp.get("elapsed_s") == 1.5
              and resp.get("call_id") == "c", f"{resp}")


def case_6_flags_resolved_once() -> None:
    """Flags resolve once per invocation, not once per event."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        calls: list[int] = []
        original = logger._resolve_flags

        def counting(project_dir: Path):
            calls.append(1)
            return original(project_dir)

        logger._resolve_flags = counting  # type: ignore[assignment]
        try:
            for i in range(20):
                logger.log_event(root, {"event": f"e{i}"})
                logger.log_event(root, {"event": "chunk",
                                        "chapter": "CHAPTER_0001"})
        finally:
            logger._resolve_flags = original  # type: ignore[assignment]
        check("6a flags: 40 events resolve the config exactly once",
              len(calls) == 1, f"resolved {len(calls)} times")


def case_7_prune() -> None:
    """_prune keeps the NEWEST N run-*.jsonl by mtime and deletes the oldest;
    index.jsonl, report.md, epub-build.log, legacy llm-*.jsonl and the
    chapters/ tree are all out of reach. keep=0 wipes runs only."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "logs"
        (base / "chapters" / "CHAPTER_0001").mkdir(parents=True)
        for i in range(5):
            p = base / f"run-{i:05d}.jsonl"
            p.write_text("{}\n", encoding="utf-8")
            os.utime(p, (1000.0 + i * 100, 1000.0 + i * 100))
        for name in ("index.jsonl", "report.md", "epub-build.log",
                     "llm-00001.jsonl"):
            (base / name).write_text("keep me", encoding="utf-8")

        logger._prune(base, 2)
        left = sorted(p.name for p in base.glob("run-*.jsonl"))
        check("7a prune: the two NEWEST runs survive",
              left == ["run-00003.jsonl", "run-00004.jsonl"], f"left={left}")
        check("7b prune: index.jsonl / report.md / epub-build.log / legacy survive",
              all((base / n).read_text(encoding="utf-8") == "keep me"
                  for n in ("index.jsonl", "report.md", "epub-build.log",
                            "llm-00001.jsonl")), "")
        check("7c prune: the chapters/ tree is untouched",
              (base / "chapters" / "CHAPTER_0001").is_dir(), "")

        logger._prune(base, 0)
        check("7d prune: keep=0 wipes every run but nothing else",
              list(base.glob("run-*.jsonl")) == []
              and (base / "index.jsonl").exists(), "")

        no_raise = True
        try:
            logger._prune(base / "missing", 2)
        except OSError:
            no_raise = False
        check("7e prune: missing base dir is a no-op", no_raise, "")


def case_8_retention() -> None:
    """Retention is per bucket: the root and project bucket keep
    log_llm_keep_runs, each chapter directory keeps log_chapter_keep_runs
    independently, and index.jsonl is never pruned."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_llm_keep_runs=2, log_chapter_keep_runs=1)

        def seed(directory: Path, n: int) -> None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "index.jsonl").write_text("{}\n", encoding="utf-8")
            for i in range(n):
                p = directory / f"run-{i:05d}.jsonl"
                p.write_text("{}\n", encoding="utf-8")
                os.utime(p, (1000.0 + i * 100, 1000.0 + i * 100))

        for _ in range(3):
            reset()
            seed(root / "logs", 4)
            seed(root / "logs" / "project", 4)
            seed(tier2_dir(root, "CHAPTER_0001"), 4)
            seed(tier2_dir(root, "CHAPTER_0002"), 4)
            logger.log_event(root, {"event": "run_start"})
            logger.log_event(root, {"event": "chunk", "chapter": "CHAPTER_0001"})
            logger.log_event(root, {"event": "llm_request",
                                    "chapter": "CHAPTER_0002", "prompt": "p"})
            logger.log_event(root, {"event": "llm_request", "prompt": "p"})

        check("8a retention: the root keeps log_llm_keep_runs",
              len(run_files(root / "logs")) == 2,
              f"{[p.name for p in run_files(root / 'logs')]}")
        check("8b retention: logs/project/ is pruned too",
              len(run_files(root / "logs" / "project")) == 2,
              f"{[p.name for p in run_files(root / 'logs' / 'project')]}")
        c1 = run_files(tier2_dir(root, "CHAPTER_0001"))
        c2 = run_files(tier2_dir(root, "CHAPTER_0002"))
        check("8c retention: each chapter keeps log_chapter_keep_runs",
              len(c1) == 1 and len(c2) == 1,
              f"c1={[p.name for p in c1]} c2={[p.name for p in c2]}")
        check("8d retention: chapter dirs are pruned independently",
              c1 != c2, f"c1={c1} c2={c2}")
        check("8e retention: index.jsonl survives four runs in one chapter dir",
              (tier2_dir(root, "CHAPTER_0001") / "index.jsonl").is_file(), "")


def case_9_two_projects() -> None:
    """Two project_dirs in one process: each appends into its OWN logs/ (no
    cross-project bleed), same-project events share one run file, the active
    run always lives in the logged project's logs/, and the reset contract
    (clear _run_path) self-heals into fresh runs for BOTH tiers."""
    with tempfile.TemporaryDirectory() as td:
        proj_a = Path(td) / "proj-a"
        proj_b = Path(td) / "proj-b"
        reset()
        logger.log_event(proj_a, {"event": "a1"})
        logger.log_event(proj_b, {"event": "b1"})
        logger.log_event(proj_a, {"event": "a2"})
        logger.log_event(proj_b, {"event": "b2"})
        logger.log_event(proj_a, {"event": "chunk", "chapter": "CHAPTER_0001"})
        logger.log_event(proj_b, {"event": "chunk", "chapter": "CHAPTER_0001"})

        a_events = events_in(proj_a / "logs")
        b_events = events_in(proj_b / "logs")
        check("9a projects: A's tier-1 logs hold exactly A's events, in order",
              [e["event"] for e in a_events] == ["a1", "a2"], f"{a_events}")
        check("9b projects: B's tier-1 logs hold exactly B's events, in order",
              [e["event"] for e in b_events] == ["b1", "b2"], f"{b_events}")
        check("9c projects: run filenames match run-stamp-tag-pid.jsonl",
              all(run_name_ok(p) for p in run_files(proj_a / "logs")
                  + run_files(proj_b / "logs")), "")
        check("9d projects: every event carries an ISO ts",
              all("ts" in e for e in a_events + b_events), "")
        check("9e projects: each project owns its own chapter bucket",
              [e["event"] for e in events_in(tier2_dir(proj_a, "CHAPTER_0001"))]
              == ["chunk"]
              and [e["event"] for e in events_in(tier2_dir(proj_b, "CHAPTER_0001"))]
              == ["chunk"], "")
        a_ids = {e["run_id"] for e in
                 a_events + events_in(tier2_dir(proj_a, "CHAPTER_0001"))}
        b_ids = {e["run_id"] for e in
                 b_events + events_in(tier2_dir(proj_b, "CHAPTER_0001"))}
        check("9f projects: one process is ONE run_id (both projects, both tiers)",
              a_ids == b_ids, f"{a_ids} {b_ids}")

        before = logger._run_path
        b_files = run_files(proj_b / "logs")
        logger.log_event(proj_b, {"event": "b3"})
        check("9g projects: same project appends to the SAME run file",
              logger._run_path == before
              and run_files(proj_b / "logs") == b_files
              and [e["event"] for e in events_in(proj_b / "logs")]
              == ["b1", "b2", "b3"],
              f"run={logger._run_path}")
        check("9h projects: _run_path is the active TIER-1 path",
              logger._run_path is not None
              and logger._run_path.parent == proj_b.resolve() / "logs",
              f"run={logger._run_path}")

        reset()
        logger.log_event(proj_b, {"event": "b4"})
        logger.log_event(proj_b, {"event": "chunk", "chapter": "CHAPTER_0009"})
        check("9i reset: clearing _run_path re-opens a run for the logged project",
              logger._run_path is not None
              and logger._run_path.parent == proj_b.resolve() / "logs"
              and [e["event"] for e in events_in(proj_b / "logs")]
              == ["b1", "b2", "b3", "b4"],
              f"run={logger._run_path}")
        check("9j reset: the reset also self-healed the tier-2 bucket",
              [e["event"] for e in events_in(tier2_dir(proj_b, "CHAPTER_0009"))]
              == ["chunk"], "")
        ids = {e["run_id"] for e in
               events_in(proj_b / "logs")
               + events_in(tier2_dir(proj_b, "CHAPTER_0009"))}
        check("9k reset: both tiers share one run_id after the reset",
              len(ids) == 1, f"{ids}")


def case_10_thread_safety() -> None:
    """The consensus fan-out logs from worker threads: concurrent calls
    produce well-formed lines with no interleaving."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()

        def worker(n: int) -> None:
            for i in range(2):
                logger.log_event(root, {"event": "chunk", "chapter": "CHAPTER_0001",
                                        "index": n * 2 + i})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = events_in(tier2_dir(root, "CHAPTER_0001"))
        indices = sorted(e["index"] for e in events)
        check("10a threads: 50 concurrent events all landed",
              len(events) == 50, f"{len(events)}")
        check("10b threads: every line parsed and no index was lost",
              indices == list(range(50)), f"{indices[:8]}...")


def case_11_never_raises() -> None:
    """Logging must never break the pipeline: an unserializable payload, and
    a logs/ path that is a regular file, both stay silent."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "bad-payload"
        reset()
        logger.log_event(root, {"event": "result", "chapter": "CHAPTER_0001",
                                "path": Path("C:/nope")})
        check("11a never-raise: an unserializable payload is swallowed",
              events_in(tier2_dir(root, "CHAPTER_0001")) == [], "")

        blocker = Path(td) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        reset()
        logger.log_event(blocker, {"event": "run_start"})
        logger.index_line(blocker, None, "open")
        logger.index_line(blocker, "CHAPTER_0001", "open")
        logger.log_event(blocker, {"event": "llm_request",
                                   "chapter": "CHAPTER_0001", "prompt": "p"})
        check("11b never-raise: logs/ being a regular file raises nothing",
              blocker.is_file(), "")


def case_12_gates() -> None:
    """The gates: log_orchestration:false silences tier 1 but not the
    indexes, and the llm_call counters keep accumulating anyway."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_orchestration=False)
        reset()
        logger.log_event(root, {"event": "run_start"})
        logger.log_event(root, {"event": "llm_call", "chapter": "CHAPTER_0001",
                                "job": "translator", "model": "m",
                                "usage": {"total_tokens": 100}})
        logger.log_event(root, {"event": "llm_call", "chapter": "CHAPTER_0001",
                                "job": "translator", "model": "m",
                                "usage": None})
        logger.log_event(root, {"event": "chunk", "chapter": "CHAPTER_0001"})
        logger.index_line(root, None, "open", command="translate")
        logger.index_line(root, "CHAPTER_0001", "open", file="CHAPTER_0001.md")
        check("12a gates: no tier-1 run file is created",
              run_files(root / "logs") == [],
              f"{[p.name for p in run_files(root / 'logs')]}")
        check("12b gates: the project index still receives its line",
              (root / "logs" / "project" / "index.jsonl").is_file(), "")
        check("12c gates: the chapter index still receives its line",
              (tier2_dir(root, "CHAPTER_0001") / "index.jsonl").is_file(), "")
        check("12d gates: tier-2 events are unaffected by log_orchestration",
              [e["event"] for e in events_in(tier2_dir(root, "CHAPTER_0001"))]
              == ["chunk"], "")
        stats = logger.take_chapter_stats(root, "CHAPTER_0001")
        check("12e gates: counters accumulate even with tier 1 off",
              stats["calls"] == 2 and stats["tokens"] == {"translator": 100},
              f"{stats}")
        check("12f gates: the counters are read-and-reset",
              logger.take_chapter_stats(root, "CHAPTER_0001")["calls"] == 0, "")


def case_13_log_llm_off() -> None:
    """log_llm:false removes the two model lines but NOT the tier-2 file:
    result/chunk/feedback are unconditional."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_llm=False)
        reset()
        logger.log_event(root, {"event": "llm_request", "chapter": "CHAPTER_0001",
                                "prompt": "p", "call_id": "a"})
        logger.log_event(root, {"event": "llm_response", "chapter": "CHAPTER_0001",
                                "response": "r", "call_id": "a"})
        logger.log_event(root, {"event": "result", "chapter": "CHAPTER_0001",
                                "call_id": "a", "kind": "verdict"})
        logger.log_event(root, {"event": "chunk", "chapter": "CHAPTER_0001"})
        logger.log_event(root, {"event": "feedback", "chapter": "CHAPTER_0001",
                                "reasons": ["x"]})
        names = [e["event"] for e in events_in(tier2_dir(root, "CHAPTER_0001"))]
        check("13a log_llm: no llm_request/llm_response lines",
              "llm_request" not in names and "llm_response" not in names,
              f"{names}")
        check("13b log_llm: the tier-2 file still exists with the rest",
              names == ["result", "chunk", "feedback"], f"{names}")


def case_14_index_line() -> None:
    """index.jsonl: append-only, both line shapes, never pruned, and written
    for chapter-less invocations too."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_chapter_keep_runs=1, log_llm_keep_runs=1)
        reset()
        logger.log_event(root, {"event": "run_start"})
        logger.index_line(root, None, "open", command="translate", pid=1)
        logger.index_line(root, "CHAPTER_0001", "open", command="translate",
                          pid=1, file="CHAPTER_0001.md")
        logger.index_line(root, "CHAPTER_0001", "close", outcome="translated",
                          attempts=1, calls=3, tokens={"translator": 40},
                          elapsed_s=12.5)
        logger.index_line(root, None, "close", outcome="ok")

        project_index = [json.loads(line) for line in
                         (root / "logs" / "project" / "index.jsonl")
                         .read_text(encoding="utf-8").splitlines() if line.strip()]
        chapter_index = [json.loads(line) for line in
                         (tier2_dir(root, "CHAPTER_0001") / "index.jsonl")
                         .read_text(encoding="utf-8").splitlines() if line.strip()]
        check("14a index: the project bucket gets an open/close pair",
              [e["phase"] for e in project_index] == ["open", "close"],
              f"{[e['phase'] for e in project_index]}")
        check("14b index: the project open line omits file",
              "file" not in project_index[0], f"{project_index[0]}")
        check("14c index: the chapter open line carries file",
              chapter_index[0].get("file") == "CHAPTER_0001.md",
              f"{chapter_index[0]}")
        check("14d index: the close line carries the chapter_end payload",
              chapter_index[1].get("outcome") == "translated"
              and chapter_index[1].get("tokens") == {"translator": 40},
              f"{chapter_index[1]}")
        check("14e index: every index line carries ts, run_id and phase",
              all({"ts", "run_id", "phase"} <= set(e)
                  for e in project_index + chapter_index), "")


def case_15_run_stats() -> None:
    """take_run_stats aggregates every chapter and the project bucket."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        for chapter in ("CHAPTER_0001", "CHAPTER_0002", None):
            logger.log_event(root, {"event": "llm_call", "chapter": chapter,
                                    "job": "translator",
                                    "usage": {"total_tokens": 10}})
            logger.log_event(root, {"event": "llm_call", "chapter": chapter,
                                    "job": "translator", "usage": None})
        stats = logger.take_run_stats(root)
        check("15a run stats: every llm_call counted, failed calls included",
              stats["calls"] == 6, f"{stats}")
        check("15b run stats: usage:null contributes zero tokens",
              stats["tokens"] == {"translator": 30}, f"{stats}")
        check("15c run stats: read-and-reset",
              logger.take_run_stats(root)["calls"] == 0, "")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_command_tag()
    case_2_keep_count()
    case_3_routing()
    case_4_stem_keying()
    case_5_prompt_bodies()
    case_6_flags_resolved_once()
    case_7_prune()
    case_8_retention()
    case_9_two_projects()
    case_10_thread_safety()
    case_11_never_raises()
    case_12_gates()
    case_13_log_llm_off()
    case_14_index_line()
    case_15_run_stats()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
