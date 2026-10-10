"""Tests for tier routing, gating and threading across the call sites.

Each event belongs to exactly ONE tier: model IO (llm_request /
llm_response / result / chunk / feedback) is chapter trace, everything else
-- including an unknown event name -- is orchestration. The two are gated by
different keys, and counters accumulate BEFORE gating so accounting survives
either switch being off.

The interesting cases here are the ones a routing table alone does not prove:

- a chapter-less call (profile, review) lands in logs/project/, and two
  chapters never share a bucket
- consensus's fan-out binds the chapter on the CALLING thread, so every
  worker's line is attributed to the right chapter even with two candidates
  in flight
- the recap backfill writes into the PREDECESSOR's bucket, not the chapter
  being translated -- the F3 misattribution, pinned
- cmd_tn binds a fresh chapter-bound callable per chapter, so N chapters
  produce N correctly-attributed exchanges
- log_llm:false drops the two model lines but leaves result/chunk/feedback
- log_prompt_bodies:false keeps the call accountable without the text

Hermetic sandboxes under tempfile.TemporaryDirectory(). Self-contained
PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_log_routing.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "requests>=2.31"]
# ///
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, config, consensus, logger, story, tn_recheck

PASSED = 0
FAILED: list[str] = []

TIER_2 = ("llm_request", "llm_response", "result", "chunk", "feedback")


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


def events(directory: Path) -> list[dict]:
    """One bucket's run-file lines. NOT recursive: the root bucket and a
    chapter bucket are separate, and rglob would merge the chapters tree
    into the root's results."""
    out: list[dict] = []
    for path in sorted(directory.glob("run-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def all_events(root: Path) -> list[dict]:
    """Every bucket under logs/, for the isolation checks."""
    out: list[dict] = []
    for path in sorted((root / "logs").rglob("run-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def chapter_dir(root: Path, stem: str) -> Path:
    return root / "logs" / "chapters" / stem


def reset() -> None:
    logger._run_path = None
    consensus._ANNOUNCED.clear()


def block(name: str = "m1") -> dict:
    return {"base_url": "http://x", "model": name, "api_key_env": "K",
            "timeout_s": 5, "max_retries": 1}


def stub_chat(bodies: dict | None = None):
    """Install a client.chat stand-in that fires the two meta_hook events
    with the real client's payload shapes.

    The default body carries a key for every template the pipeline drives,
    so a test can exercise attribution without each one pinning the JSON
    shape its own template expects."""
    def fake(provider_cfg, prompt, json_schema=None, max_tokens=None,
             meta_hook=None, **_):
        body = (bodies or {}).get(prompt) or json.dumps(
            {"recap": "r", "verdict": "SUCCESS", "reasons": [],
             "notes": [], "terms": []})
        if meta_hook:
            meta_hook({"event": "llm_request", "call_id": "c",
                       "url": "http://x", "model": provider_cfg.get("model"),
                       "params": {}, "guided_json": False, "prompt": prompt})
            meta_hook({"event": "llm_response", "call_id": "c",
                       "url": "http://x", "model": provider_cfg.get("model"),
                       "response": body, "finish_reason": "stop",
                       "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                 "total_tokens": 4},
                       "elapsed_s": 0.1, "error": None})
        return body
    return fake


def one_block_cfg() -> dict:
    """Every job the call sites under test name -- provider_list requires the
    key to exist, and these cases drive recap and annotator too."""
    return {"providers": {job: [block("m1")] for job in
                          ("translator", "reviewer", "glossary", "annotator",
                           "recap")}
            | {"consensus": [block("c1")]}}


def two_block_cfg() -> dict:
    return {"providers": {job: [block("m1"), block("m2")] for job in
                          ("translator", "reviewer", "glossary", "annotator",
                           "recap")}
            | {"consensus": [block("c1")]}}


def case_1_routing_table() -> None:
    """Each event name routes to exactly one tier, and an unknown name
    defaults to tier 1 rather than leaking model text into a chapter file."""
    check("1a routing: the tier-2 set is exactly the five model-IO names",
          set(consensus and logger.TIER_2_EVENTS) == set(TIER_2),
          f"{sorted(logger.TIER_2_EVENTS)}")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        for name in TIER_2:
            logger.log_event(root, {"event": name, "chapter": "CHAPTER_0001"})
        logger.log_event(root, {"event": "not_a_real_event",
                                "chapter": "CHAPTER_0001"})
        names = [e["event"] for e in events(chapter_dir(root, "CHAPTER_0001"))]
        root_names = [e["event"] for e in events(root / "logs")]
        check("1b routing: exactly the five tier-2 names reach the chapter",
              names == list(TIER_2), f"{names}")
        check("1c routing: an unknown name defaults to tier 1",
              root_names == ["not_a_real_event"], f"{root_names}")


def case_2_project_bucket() -> None:
    """A chapter-less call lands in logs/project/, and a chapter call never
    does."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        logger.log_event(root, {"event": "llm_request", "prompt": "p"})
        logger.log_event(root, {"event": "llm_request",
                                "chapter": "CHAPTER_0001.md", "prompt": "p"})
        project_events = events(root / "logs" / "project")
        check("2a bucket: the chapter-less call is in logs/project/",
              [e["event"] for e in project_events] == ["llm_request"],
              f"{project_events}")
        check("2b bucket: the chapter call is NOT in the project bucket",
              all(e.get("chapter") != "CHAPTER_0001.md"
                  for e in project_events), "")
        check("2c bucket: the chapter call is in the chapter bucket",
              [e["event"] for e in events(chapter_dir(root, "CHAPTER_0001"))]
              == ["llm_request"], "")
        check("2d bucket: the filename and the stem resolve to one directory",
              not (root / "logs" / "chapters" / "CHAPTER_0001.md").exists(), "")


def case_3_chapter_threading() -> None:
    """consensus.chat threads `chapter` into the trace hook, so the model
    exchange lands in that chapter's tier-2 bucket."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        client.chat = stub_chat()
        try:
            consensus.chat(root, one_block_cfg(), "translator", "P1",
                           chapter="CHAPTER_0007.md")
            consensus.chat(root, one_block_cfg(), "translator", "P2")
        finally:
            client.chat = _REAL_CHAT
        seven = events(chapter_dir(root, "CHAPTER_0007"))
        project_bucket = events(root / "logs" / "project")
        timeline = events(root / "logs")
        check("3a thread: the chapter-less call went to the project bucket",
              [e["event"] for e in project_bucket]
              == ["llm_request", "llm_response"], f"{project_bucket}")
        check("3b thread: the chapter's tier-2 file holds the model IO only",
              [e["event"] for e in seven] == ["llm_request", "llm_response"],
              f"{[e['event'] for e in seven]}")
        check("3c thread: llm_call is tier 1, so it lands in the ROOT timeline",
              [e["event"] for e in timeline] == ["llm_call", "llm_call"],
              f"{[e['event'] for e in timeline]}")
        check("3d thread: both buckets carry the same run_id",
              len({e["run_id"] for e in seven + project_bucket + timeline}) == 1,
              "")
        check("3e thread: the chapter name rides on the chapter's lines, and "
              "the chapter-less call carries none",
              all(e.get("chapter") == "CHAPTER_0007.md" for e in seven)
              and all(e.get("chapter") is None for e in project_bucket)
              and sorted(str(e.get("chapter")) for e in timeline)
              == ["CHAPTER_0007.md", "None"], "")


def case_4_fanout_binding() -> None:
    """The fan-out binds the chapter on the CALLING thread before any worker
    starts, so both candidates' lines are attributed to that chapter."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        client.chat = stub_chat()
        try:
            consensus.chat(root, two_block_cfg(), "translator", "P",
                           chapter="CHAPTER_0042.md")
        finally:
            client.chat = _REAL_CHAT
        calls = [e for e in events(root / "logs") if e["event"] == "llm_call"]
        candidates = sorted(e.get("candidate") for e in calls
                            if e.get("candidate") is not None)
        check("4a fanout: both candidates appear in the tier-1 timeline",
              candidates == [1, 2], f"{candidates}")
        check("4b fanout: the consensus call is traced too",
              any(e.get("job") == "consensus" for e in calls),
              f"{[e.get('job') for e in calls]}")
        check("4c fanout: every fan-out line carries the chapter",
              all(e.get("chapter") == "CHAPTER_0042.md"
                  for e in calls + events(chapter_dir(root, "CHAPTER_0042"))), "")
        names = [e["event"] for e in events(chapter_dir(root, "CHAPTER_0042"))]
        check("4d fanout: the chapter's tier-2 file holds 3 requests + 3 responses",
              sorted(names) == sorted(["llm_request", "llm_response"] * 3),
              f"{names}")
        check("4e fanout: nothing leaked into the project bucket",
              events(root / "logs" / "project") == [], "")


def case_5_recap_attribution() -> None:
    """The recap backfill's model call belongs to the PREDECESSOR whose text
    it summarized, not to the chapter being translated (the F3 defect)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        client.chat = stub_chat()
        try:
            story._default_chat(root, one_block_cfg(), "CHAPTER_0006.md")("R")
        finally:
            client.chat = _REAL_CHAT
        six = events(chapter_dir(root, "CHAPTER_0006"))
        check("5a recap: the predecessor's tier-2 file holds the backfill call",
              [e["event"] for e in six] == ["llm_request", "llm_response"],
              f"{[e['event'] for e in six]}")
        check("5b recap: the current chapter's bucket is untouched",
              events(chapter_dir(root, "CHAPTER_0007")) == [], "")
        check("5c recap: the line names the predecessor",
              all(e.get("chapter") == "CHAPTER_0006.md" for e in six), "")
        check("5d recap: nothing landed in the project bucket",
              events(root / "logs" / "project") == [], "")


def case_6_tn_recheck_binding() -> None:
    """cmd_tn never enters run_chapter, so the per-chapter callable factory is
    what attributes each annotator exchange to its own chapter."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reset()
        client.chat = stub_chat()
        try:
            for stem in ("CHAPTER_0001.md", "CHAPTER_0002.md"):
                tn_recheck.make_chat(root, one_block_cfg(), stem)("P")
        finally:
            client.chat = _REAL_CHAT
        one = events(chapter_dir(root, "CHAPTER_0001"))
        two = events(chapter_dir(root, "CHAPTER_0002"))
        check("6a tn: each chapter's exchange landed in its own bucket",
              [e["event"] for e in one] == ["llm_request", "llm_response"]
              and [e["event"] for e in two]
              == ["llm_request", "llm_response"],
              f"one={len(one)} two={len(two)}")
        check("6b tn: neither chapter's lines carry the other's name",
              all(e.get("chapter") == "CHAPTER_0001.md" for e in one)
              and all(e.get("chapter") == "CHAPTER_0002.md" for e in two), "")
        check("6c tn: the project bucket stayed empty",
              events(root / "logs" / "project") == [], "")


def case_7_log_llm_gate() -> None:
    """log_llm:false removes the two model lines but leaves the pipeline's
    own per-chunk record in place."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_llm=False)
        reset()
        client.chat = stub_chat()
        try:
            consensus.chat(root, one_block_cfg(), "translator", "P",
                           chapter="CHAPTER_0001.md")
        finally:
            client.chat = _REAL_CHAT
        names = [e["event"] for e in events(chapter_dir(root, "CHAPTER_0001"))]
        timeline = [e["event"] for e in events(root / "logs")]
        check("7a log_llm: no llm_request / llm_response",
              "llm_request" not in names and "llm_response" not in names,
              f"{names}")
        check("7b log_llm: llm_call survives in tier 1 (not log_llm's concern)",
              timeline == ["llm_call"], f"{timeline}")
        reset()
        write_cfg(root, log_llm=False)
        logger.log_event(root, {"event": "chunk", "chapter": "CHAPTER_0001"})
        logger.log_event(root, {"event": "result", "chapter": "CHAPTER_0001",
                                "kind": "faithfulness"})
        names = [e["event"] for e in events(chapter_dir(root, "CHAPTER_0001"))]
        check("7c log_llm: result/chunk are unconditional",
              names == ["chunk", "result"], f"{names}")


def case_8_prompt_bodies_gate() -> None:
    """log_prompt_bodies:false drops the text from the tier-2 lines while
    keeping the metadata that makes the call accountable."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_prompt_bodies=False)
        reset()
        client.chat = stub_chat({"P": "0123456789"})
        try:
            consensus.chat(root, one_block_cfg(), "translator", "P",
                           chapter="CHAPTER_0001.md")
        finally:
            client.chat = _REAL_CHAT
        lines = events(chapter_dir(root, "CHAPTER_0001"))
        req = next(e for e in lines if e["event"] == "llm_request")
        resp = next(e for e in lines if e["event"] == "llm_response")
        check("8a bodies: prompt replaced by its length",
              req.get("prompt_chars") == 1 and "prompt" not in req, f"{req}")
        check("8b bodies: response replaced by its length",
              resp.get("response_chars") == 10 and "response" not in resp,
              f"{resp}")
        check("8c bodies: usage and finish_reason survive",
              resp.get("usage") == {"prompt_tokens": 3, "completion_tokens": 1,
                                    "total_tokens": 4}
              and resp.get("finish_reason") == "stop", f"{resp}")
        calls = [e for e in events(root / "logs") if e["event"] == "llm_call"]
        check("8d bodies: the tier-1 llm_call keeps its usage and is never "
              "body-stripped",
              calls and calls[0].get("usage", {}).get("total_tokens") == 4,
              f"{calls}")


def case_9_orchestration_gate() -> None:
    """log_orchestration:false silences tier 1, and the counters still
    accumulate because they are incremented before the gate."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_cfg(root, log_orchestration=False)
        reset()
        client.chat = stub_chat()
        try:
            consensus.chat(root, one_block_cfg(), "translator", "P",
                           chapter="CHAPTER_0001.md")
        finally:
            client.chat = _REAL_CHAT
        check("9a orchestration: no tier-1 run file exists",
              not list((root / "logs").glob("run-*.jsonl")),
              f"{[p.name for p in (root / 'logs').glob('run-*.jsonl')]}")
        check("9b orchestration: the chapter tier-2 file still has the model IO",
              [e["event"] for e in events(chapter_dir(root, "CHAPTER_0001"))]
              == ["llm_request", "llm_response"], "")
        stats = logger.take_chapter_stats(root, "CHAPTER_0001.md")
        check("9c orchestration: counters survived the gate",
              stats["calls"] == 1 and stats["tokens"] == {"translator": 4},
              f"{stats}")


def case_10_isolation() -> None:
    """Two chapters never share a bucket, and two projects never bleed."""
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a"
        b = Path(td) / "b"
        reset()
        logger.log_event(a, {"event": "chunk", "chapter": "CHAPTER_0001.md"})
        logger.log_event(a, {"event": "chunk", "chapter": "CHAPTER_0002.md"})
        logger.log_event(b, {"event": "chunk", "chapter": "CHAPTER_0001.md"})
        check("10a isolation: A's two chapters are separate buckets",
              len([e for e in events(chapter_dir(a, "CHAPTER_0001"))]) == 1
              and len([e for e in events(chapter_dir(a, "CHAPTER_0002"))]) == 1,
              "")
        check("10b isolation: B's CHAPTER_0001 is B's, not A's",
              len([e for e in events(chapter_dir(b, "CHAPTER_0001"))]) == 1
              and not (b / "logs" / "chapters" / "CHAPTER_0002").exists(), "")


_REAL_CHAT = client.chat


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_routing_table()
    case_2_project_bucket()
    case_3_chapter_threading()
    case_4_fanout_binding()
    case_5_recap_attribution()
    case_6_tn_recheck_binding()
    case_7_log_llm_gate()
    case_8_prompt_bodies_gate()
    case_9_orchestration_gate()
    case_10_isolation()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
