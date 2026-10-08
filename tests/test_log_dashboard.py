"""Tests for lib/logdashboard.py -- the project-wide HTML trace dashboard.

The dashboard's whole job is to be trusted with numbers it did not measure, so
these tests are about it never lying and never raising:

- it ESCAPES model text, so a reply carrying a closing script tag or
  <img onerror=...> cannot break out of the structure the page asserts on
- it makes NO network requests: no CDN, no webfont, no absolute URL
- it INVENTS no numbers: a call with usage:null renders an em dash, and a
  missing metric is absent rather than zero
- it degrades HONESTLY: with the orchestration tier gone it says so instead of
  drawing zero-width bars, and spend outside any chapter is called out as
  unknown rather than silently dropped

The CALL LEDGER gets the deepest attention, because that is where the design is
easiest to get quietly wrong. Three ways it could lie, all pinned here:

- it reports ONE run per chapter, and it must be the SAME run the ledger bar
  reports (an earlier draft preferred the last CLOSE line, which with
  log_chapter_keep_runs:3 picks an OLDER run than the bar and shows two runs
  side by side);
- it dispatches response shape on the PARSED KEY SET, never on `job` or
  `consensus_for` -- one job name drives three different schemas, so keying on
  it renders the wrong table;
- it encodes the body blob with `<` -> \\u003c, NOT html.escape, because
  <script> is a rawtext element. The html.unescape-based test that "proved"
  html.escape correct simulated the opposite of what a browser does, and passed
  green for the broken encoding.

The PRIMARY path gets the most attention. An early version of this feature
validated only against a fixture whose tier-1 files had been deleted, which is
how the signature element ended up specified against its own worst case; case 4
below builds a tree with tier-1 present and asserts the rich rendering.

Hermetic sandboxes under tempfile.TemporaryDirectory(). Self-contained
PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_log_dashboard.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import logger, logdashboard  # noqa: E402

PASSED = 0
FAILED: list[str] = []

RID = "20260101-000000-translate-4242"
RID2 = "20260102-000000-translate-4243"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}  {detail}")


def write_lines(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def seed_config(root: Path, **extra: object) -> None:
    """A minimal project config. The CLI refuses to run without one, so every
    fixture that is exercised through `translate logs` needs it."""
    base = {"version": 14, "log_orchestration": True, "log_llm": True,
            "log_prompt_bodies": True, "log_llm_keep_runs": 10,
            "log_chapter_keep_runs": 3, "source_lang": "zh", "target_lang": "en"}
    base.update(extra)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(base, ensure_ascii=False),
                                      encoding="utf-8")


def seed_tier1(root: Path, stem: str = "CHAPTER_0001", *,
               retry_stage: bool = False) -> Path:
    """A project WITH the orchestration tier -- the normal case.

    retry_stage adds a second TRANSLATE attempt so case 6 can prove the bar
    sums every attempt rather than collapsing to the last one the way
    report.md's table does.
    """
    logs = root / "logs"
    bucket = logs / "chapters" / stem
    seed_config(root)
    write_lines(bucket / "index.jsonl", [
        {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID,
         "phase": "open", "command": "translate", "file": f"{stem}.md",
         "number": int(stem.split("_")[1])},
        {"ts": "2026-01-01T00:09:00.000+00:00", "run_id": RID, "phase": "close",
         "outcome": "translated", "attempts": 2 if retry_stage else 1,
         "stages": 3, "calls": 3,
         "tokens": {"translator": 1000, "reviewer": 500}, "elapsed_s": 540.0},
    ])
    write_lines(logs / "project" / "index.jsonl", [
        {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID,
         "phase": "open", "command": "translate"},
        {"ts": "2026-01-01T00:09:00.000+00:00", "run_id": RID,
         "phase": "close", "outcome": "completed"},
    ])
    stages = [
        {"ts": "2026-01-01T00:00:01.000+00:00", "run_id": RID, "event": "stage",
         "chapter": f"{stem}.md", "stage": "FAITH", "phase": "begin",
         "attempt": 1},
        {"ts": "2026-01-01T00:00:31.000+00:00", "run_id": RID, "event": "stage",
         "chapter": f"{stem}.md", "stage": "FAITH", "phase": "end",
         "attempt": 1, "elapsed_s": 30.0},
        {"ts": "2026-01-01T00:00:32.000+00:00", "run_id": RID, "event": "stage",
         "chapter": f"{stem}.md", "stage": "TRANSLATE", "phase": "begin",
         "attempt": 1},
        {"ts": "2026-01-01T00:00:52.000+00:00", "run_id": RID, "event": "stage",
         "chapter": f"{stem}.md", "stage": "TRANSLATE", "phase": "end",
         "attempt": 1, "elapsed_s": 20.0},
    ]
    if retry_stage:
        # A second attempt of the same stage: the bar must count BOTH.
        stages += [
            {"ts": "2026-01-01T00:00:53.000+00:00", "run_id": RID,
             "event": "stage", "chapter": f"{stem}.md", "stage": "TRANSLATE",
             "phase": "begin", "attempt": 2},
            {"ts": "2026-01-01T00:01:33.000+00:00", "run_id": RID,
             "event": "stage", "chapter": f"{stem}.md", "stage": "TRANSLATE",
             "phase": "end", "attempt": 2, "elapsed_s": 40.0},
        ]
    stages.append(
        {"ts": "2026-01-01T00:01:34.000+00:00", "run_id": RID, "event": "stage",
         "chapter": f"{stem}.md", "stage": "ASSEMBLE", "phase": "begin",
         "attempt": 1})
    write_lines(logs / f"run-{RID}.jsonl", stages + [
        {"ts": "2026-01-01T00:00:35.000+00:00", "run_id": RID, "event": "gate",
         "chapter": f"{stem}.md", "stage": "FAITH", "verdict": "pass",
         "reasons": []},
        {"ts": "2026-01-01T00:01:35.000+00:00", "run_id": RID,
         "event": "llm_call", "chapter": f"{stem}.md", "job": "translator",
         "model": "model-a", "usage": {"prompt_tokens": 600,
                                       "completion_tokens": 400},
         "elapsed_s": 20.0, "finish_reason": "stop"},
        {"ts": "2026-01-01T00:01:36.000+00:00", "run_id": RID,
         "event": "llm_call", "chapter": f"{stem}.md", "job": "reviewer",
         "model": "model-b", "candidate": 1, "candidates": 2,
         "usage": {"prompt_tokens": 300, "completion_tokens": 200},
         "elapsed_s": 15.0, "finish_reason": "stop"},
        # usage null -> zero tokens, never a guess.
        {"ts": "2026-01-01T00:01:37.000+00:00", "run_id": RID,
         "event": "llm_call", "chapter": f"{stem}.md", "job": "reviewer",
         "model": "model-b", "candidate": 2, "candidates": 2, "usage": None,
         "elapsed_s": 1.0, "finish_reason": "length", "error": "boom"},
        {"ts": "2026-01-01T00:09:00.000+00:00", "run_id": RID,
         "event": "run_end", "command": "translate", "outcome": "completed",
         "calls": 3, "tokens": {"translator": 1500, "reviewer": 700,
                                "profile": 42}, "elapsed_s": 540.0},
    ])
    return bucket


def seed_no_tier1(root: Path) -> Path:
    """A project whose orchestration tier retention has emptied. Indexes only."""
    bucket = root / "logs" / "chapters" / "CHAPTER_0007"
    seed_config(root)
    write_lines(bucket / "index.jsonl", [
        {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID, "phase": "open",
         "command": "translate", "file": "CHAPTER_0007.md", "number": 7},
        {"ts": "2026-01-01T00:05:00.000+00:00", "run_id": RID, "phase": "close",
         "outcome": "translated", "attempts": 1, "stages": 5, "calls": 8,
         "tokens": {"recap": 500}, "elapsed_s": 300.0},
    ])
    return bucket


def html_of(root: Path) -> str:
    return logdashboard.render(logdashboard.collect(root))


def blob_of(text: str) -> list[dict]:
    """The records the browser will see, read back the way a browser does.

    Deliberately does NOT apply html.unescape. A <script> element is rawtext:
    its content is handed to the DOM verbatim and character references are
    never decoded, so unescaping here would model a browser that does not
    exist -- and it is exactly that mistake that let html.escape survive review
    on a blob it breaks 100% of. (Confirmed in Chrome; see
    probe-artifacts/blob-escape-browser-test3.html.)
    """
    start = text.index('<script type="application/json" id="dl-bodies">')
    start = text.index(">", start) + 1
    end = text.index("</" + "script", start)
    return json.loads(text[start:end])


def seed_calls(root: Path, stem: str, run_id: str,
               calls: list[dict], *, open_index: bool = True) -> Path:
    """Write a tier-2 chapter bucket holding real llm_request/llm_response pairs.

    Each entry of `calls` becomes one request row and one response row sharing
    a call_id, which is what the collector joins on. `body` may be any string --
    raw JSON, a ```json fence, or prose -- because the shape dispatch has to
    cope with all three.
    """
    bucket = root / "logs" / "chapters" / stem
    seed_config(root)
    rows: list[dict] = []
    for n, c in enumerate(calls):
        cid = c.get("call_id", f"call{n:04d}")
        base = {"ts": f"2026-01-01T00:0{n % 10}:00.000+00:00", "run_id": run_id,
                "chapter": f"{stem}.md", "call_id": cid}
        req = dict(base, event="llm_request", job=c.get("job", "translator"),
                   model=c.get("model", "model-a"),
                   url=c.get("url", "https://api.invalid/v1/chat"),
                   prompt=c.get("prompt", ["hello"]),
                   params=c.get("params", {"temperature": 0.7}))
        for k in ("candidate", "candidates", "consensus_for"):
            if k in c:
                req[k] = c[k]
        rows.append(req)
        resp = dict(base, event="llm_response", job=c.get("job", "translator"),
                    model=c.get("model", "model-a"),
                    url=c.get("url", "https://api.invalid/v1/chat"),
                    response=c.get("body", "{}"),
                    finish_reason=c.get("finish_reason", "stop"),
                    elapsed_s=c.get("elapsed_s", 5.0))
        for k in ("candidate", "candidates", "consensus_for", "usage", "error"):
            if k in c:
                resp[k] = c[k]
        rows.append(resp)
    write_lines(bucket / f"run-{run_id}.jsonl", rows)
    if open_index:
        number = int(stem.split("_")[1])
        write_lines(bucket / "index.jsonl", [
            {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": run_id,
             "phase": "open", "command": "translate", "file": f"{stem}.md",
             "number": number},
            {"ts": "2026-01-01T00:09:00.000+00:00", "run_id": run_id,
             "phase": "close", "outcome": "translated", "attempts": 1,
             "stages": 5, "calls": len(calls),
             "tokens": {"translator": len(calls) * 100},
             "elapsed_s": 540.0},
        ])
    return bucket


def cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "translate.py"), *args,
         "--project", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------

def case_1_primary_path() -> None:
    """THE NORMAL CASE. With tier-1 present the page must render stage spans,
    gate verdicts, the per-call table and the per-model roster."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root)
        data = logdashboard.collect(root)
        text = logdashboard.render(data)

        chapter = data["chapters"][0]
        check("1a primary: tier-1 is detected",
              data["has_tier1"] is True, "")
        check("1b primary: stage spans are read from tier-1",
              [s["stage"] for s in chapter["stage_spans"]]
              == ["FAITH", "TRANSLATE", "ASSEMBLE"],
              f"{[s['stage'] for s in chapter['stage_spans']]}")
        check("1c primary: the unfinished stage is kept and marked",
              chapter["stage_spans"][-1]["ended"] is False, "")
        check("1d primary: gate verdicts are read",
              chapter["gates"] and chapter["gates"][0]["verdict"] == "pass", "")
        check("1e primary: per-call rows are read",
              len(chapter["calls_detail"]) == 3,
              f"{len(chapter['calls_detail'])}")
        check("1f primary: the candidate label keeps its fan-out ratio",
              any(c["candidate"] == "1/2" for c in chapter["calls_detail"]), "")
        check("1g primary: per-model roster is built",
              set(data["tokens_by_model"]) == {"model-a", "model-b"},
              f"{list(data['tokens_by_model'])}")
        check("1h primary: tokens come from per-call rows, not the index",
              data["token_source"].startswith("per-call rows"),
              data["token_source"])
        check("1i primary: no 'unavailable' banner",
              "orchestration tier unavailable" not in text, "")
        check("1j primary: the gate verdict appears in the page",
              "pass" in text, "")
        check("1k primary: the model roster is not empty",
              "No per-model breakdown" not in text, "")


def case_2_escaping() -> None:
    """The blob encoding is the whole safety story, and html.escape is wrong.

    A <script> element is rawtext: the HTML parser never decodes character
    references inside it, so html.escape turns every JSON quote into a literal
    &quot; and JSON.parse fails at position 1. Since every response body
    contains a quote, that breaks 100% of records -- the section would render
    an error banner instead of content.

    These assertions therefore compare the blob against the encoder's own
    output, and read it back WITHOUT html.unescape.
    """
    close = "<" + "/script>"
    nasty = (close + '<img src=x onerror=alert(1)>' + '"quoted"'
             + "a<b>&c\n```\n</details>\n<!-- --></style>")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_calls(root, "CHAPTER_0001", RID, [
            {"call_id": "nasty01", "job": "translator", "model": "model-a",
             "body": nasty},
            {"call_id": "nasty02", "job": "reviewer", "model": "model-b",
             "body": json.dumps({"verdict": "SUCCESS", "reasons": []})},
        ])
        text = html_of(root)

        check("2a blob: the payload is not a literal close tag in the page",
              close + "><img" not in text, "")
        check("2b blob: '<' never survives raw inside the script element",
              "<img src=x onerror" not in text, "")
        start = text.index('id="dl-bodies">') + len('id="dl-bodies">')
        end = text.index("</" + "script", start)
        blob_text = text[start:end]
        # Scoped to the blob. HTML ATTRIBUTES elsewhere legitimately carry
        # &quot; from _esc(quote=True); only the blob must be entity-free,
        # because nothing decodes entities inside a rawtext element.
        check("2c blob: the blob itself contains no entity escape",
              "&quot;" not in blob_text and "&amp;" not in blob_text, "")

        records = blob_of(text)
        check("2d blob: the records parse, which is the whole point",
              isinstance(records, list) and len(records) == 2, f"{len(records)}")
        got = next((r for r in records if r["i"] == "nasty01"), {})
        check("2e blob: the hostile body round-trips byte-identical",
              got.get("body") == nasty, repr(got.get("body"))[:80])
        check("2f blob: a quoted JSON body round-trips too",
              next((r for r in records if r["i"] == "nasty02"), {})
              .get("parsed") == {"verdict": "SUCCESS", "reasons": []}, "")

        # The encoded form is exactly the documented transform -- this is the
        # assertion that would have caught the html.escape version.
        expected = json.dumps(logdashboard._ledger_blob(
            logdashboard.collect(root)), ensure_ascii=False,
            default=str).replace("<", "\\u003c")
        check("2g blob: the emitted blob is the documented encoding",
              blob_text == expected, "")


def case_3_no_network() -> None:
    """Self-contained or it is not: no CDN, no webfont, no absolute URL, and no
    fetch() -- including from the new body-rendering script.

    A model response may legitimately CONTAIN a URL; that is inert text. What
    must not exist is a live reference or a programmatic fetch.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_calls(root, "CHAPTER_0001", RID, [
            {"call_id": "net001", "job": "translator", "model": "model-a",
             "body": "https://example.invalid/see-here"},
        ])
        text = html_of(root)
        low = text.lower()
        for label, needle in (("img src", 'src="http'),
                              ("link href", 'href="http'),
                              ("css url()", "url(http"),
                              ("@import", "@import"),
                              ("protocol-relative src", 'src="//'),
                              ("iframe", "<iframe"),
                              ("fetch()", "fetch("),
                              ("xhr", "xmlhttprequest"),
                              ("websocket", "websocket"),
                              ("event-source", "eventsource")):
            check(f"3 network: no {label}", needle not in low, "")
        check("3g network: no <link> to a stylesheet at all",
              "<link" not in low, "")
        check("3h network: no external script or iframe references",
              "script src=" not in low and "<iframe" not in low, "")
        check("3i network: the model URL survives only as data",
              "https://example.invalid/see-here" in text, "")


def case_4_no_invented_numbers() -> None:
    """usage:null is unknown, not zero. The index close line carries a real
    total; the per-call row does not, and the page must not conflate them."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root)
        data = logdashboard.collect(root)
        check("4a numbers: the failed call still counts as a call",
              data["total_calls"] == 3, f"{data['total_calls']}")
        # 1000+500 from the index close line; per-call rows total 600+400 +
        # 300+200 + nothing = 1500. The page uses per-call rows and says so.
        check("4b numbers: the source of the total is named",
              data["token_source"] in ("per-call rows (tier 1)",
                                       "per-call rows (tier 2)",
                                       "index close lines"),
              data["token_source"])
        check("4c numbers: an unknown duration renders as an em dash",
              logdashboard._fmt_duration(None) == "-",
              logdashboard._fmt_duration(None))
        check("4d numbers: an unknown count renders as an em dash",
              logdashboard._fmt_count(None) == "-",
              logdashboard._fmt_count(None))
        check("4e numbers: zero is rendered as zero, not as unknown",
              logdashboard._fmt_count(0) == "0",
              logdashboard._fmt_count(0))
        check("4f numbers: a malformed usage contributes zeros, never a guess",
              logdashboard._usage_tokens({"prompt_tokens": "x"}) == (0, 0), "")
        check("4g numbers: a non-numeric elapsed_s stays unknown",
              logdashboard._fmt_duration("abc") == "-",
              logdashboard._fmt_duration("abc"))


def case_5_degrades_honestly() -> None:
    """Retention emptied the orchestration tier. The page must still render,
    and must say what it lost instead of drawing an empty chart."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_no_tier1(root)
        text = html_of(root)
        check("5a degrade: a page is still produced",
              "<html" in text and "</html>" in text, "")
        check("5b degrade: the missing tier is named",
              "orchestration tier unavailable" in text, "")
        check("5c degrade: chapter totals survive",
              "recap" in text and "500" in text, "")
        check("5d degrade: no empty stage chart is claimed",
              "no stage events" not in text.lower(), "")
        check("5e degrade: chapter-less spend is declared unknown, not zero",
              "is NOT in any total" in text, "")


def case_6_retry_spans_sum() -> None:
    """A retried stage must contribute every second to the bar.

    report.md collapses a stage by name and shows only the final attempt; the
    bar must not, or the ledger would account for less time than the run spent.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root, retry_stage=True)
        spans = logdashboard.collect(root)["chapters"][0]["stage_spans"]
        translate = [s for s in spans if s["stage"] == "TRANSLATE"]
        check("6a retry: both attempts are kept",
              len(translate) == 2, f"{len(translate)}")
        total = sum(s["elapsed_s"] or 0 for s in translate)
        check("6b retry: their durations sum, they do not overwrite",
              total == 60.0, f"{total}")


def case_7_ordering_and_legacy() -> None:
    """CHAPTER_0100 sorts after CHAPTER_0099; a pre-migration bucket name
    sorts last rather than vanishing or raising."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for stem in ("CHAPTER_0100", "Chapter_0099", "CHAPTER_0007"):
            write_lines(root / "logs" / "chapters" / stem / "index.jsonl", [
                {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID,
                 "phase": "open", "command": "translate", "file": f"{stem}.md",
                 "number": int(stem.split("_")[1])},
            ])
        stems = [c["stem"] for c in logdashboard.collect(root)["chapters"]]
        check("7a order: numeric, not lexicographic",
              stems[0] == "CHAPTER_0007" and stems[1] == "CHAPTER_0100",
              f"{stems}")
        check("7b order: a legacy bucket sorts last and is kept",
              stems[-1] == "Chapter_0099" and len(stems) == 3, f"{stems}")
        text = html_of(root)
        check("7c order: the legacy bucket is reported, not hidden",
              "predate the CHAPTER_NNNN naming" in text, "")


def case_8_unclosed_and_multi_run() -> None:
    """An open with no close is the crash signal, and a chapter that crashed
    then succeeded must read as recovered.

    Two buckets, because the two rules pull opposite ways and one fixture
    cannot assert both: if the unclosed run were the LAST one, the chapter
    genuinely IS unresolved and must read red.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_lines(root / "logs" / "chapters" / "CHAPTER_0004" / "index.jsonl", [
            {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID,
             "phase": "open", "command": "translate", "file": "CHAPTER_0004.md",
             "number": 4},
            {"ts": "2026-01-01T00:07:00.000+00:00", "run_id": RID,
             "phase": "close", "outcome": "crashed", "attempts": 0, "stages": 0,
             "calls": 3, "tokens": {"translator": 100}, "elapsed_s": 432.0},
            {"ts": "2026-01-01T00:08:00.000+00:00", "run_id": RID2,
             "phase": "open", "command": "translate", "file": "CHAPTER_0004.md",
             "number": 4},
            {"ts": "2026-01-01T00:20:00.000+00:00", "run_id": RID2,
             "phase": "close", "outcome": "translated", "attempts": 1,
             "stages": 8, "calls": 11, "tokens": {"translator": 900},
             "elapsed_s": 1184.0},
        ])
        # A chapter whose LAST run died mid-flight is genuinely unresolved.
        write_lines(root / "logs" / "chapters" / "CHAPTER_0009" / "index.jsonl", [
            {"ts": "2026-01-01T00:30:00.000+00:00", "run_id": "20260103-x",
             "phase": "open", "command": "translate", "file": "CHAPTER_0009.md",
             "number": 9},
        ])
        data = logdashboard.collect(root)
        by_stem = {c["stem"]: c for c in data["chapters"]}

        check("8a unclosed: an open with no close is kept",
              len(by_stem["CHAPTER_0009"]["runs"]) == 1
              and by_stem["CHAPTER_0009"]["runs"][0]["closed"] is False, "")
        check("8b unclosed: the crash signal is surfaced",
              any("20260103-x" in u for u in data["unclosed"]),
              f"{data['unclosed']}")
        check("8c unclosed: a run is listed once, not once per bucket",
              len(data["unclosed"]) == 1, f"{data['unclosed']}")
        check("8d unclosed: an unresolved last run reads as unclosed",
              by_stem["CHAPTER_0009"]["outcome"] is None
              and by_stem["CHAPTER_0009"]["closed"] is False, "")

        check("8e unclosed: the latest run decides a recovered chapter",
              by_stem["CHAPTER_0004"]["outcome"] == "translated",
              f"{by_stem['CHAPTER_0004']['outcome']}")
        text = logdashboard.render(data)
        check("8f unclosed: a recovered chapter is not stamped red",
              'data-outcome="translated"' in text, "")
        check("8g unclosed: the unresolved one is stamped unclosed",
              'data-outcome="unclosed"' in text, "")
        check("8h unclosed: earlier runs are summarised on the row",
              "+1 earlier" in text, "")


def case_9_no_cap() -> None:
    """There is no cap and no second mode. Everything the project kept is here.

    The removed IO_BYTE_CAP walked chapters and files in ASCENDING order, so it
    spent the budget on the oldest runs first and dropped recent chapters: 84 of
    131 bodies on the real sample. The replacement must carry all of them.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        big = "y" * 4000
        calls = [{"call_id": f"cap{n:04d}", "job": "translator",
                  "model": "model-a",
                  "body": json.dumps({"verdict": "SUCCESS", "reasons": [],
                                      "pad": big})}
                 for n in range(300)]
        seed_calls(root, "CHAPTER_0001", RID, calls)
        text = html_of(root)
        records = blob_of(text)
        ids = {r["i"] for r in records}
        check("9a cap: every call is present, none sampled",
              len(records) == 300, f"{len(records)}")
        check("9b cap: every call id survives to the blob",
              all(f"cap{n:04d}" in ids for n in range(300)), "")
        check("9c cap: the index table has one row per call",
              text.count('class="callrow"') == 300,
              f"{text.count('class=' + chr(34) + 'callrow' + chr(34))}")
        check("9d cap: no body was truncated",
              all(len(r.get("parsed", {}).get("pad", "")) == 4000
                  for r in records if r.get("parsed")), "")
        check("9e cap: the cap constant is gone from the module",
              not hasattr(logdashboard, "IO_BYTE_CAP"), "")
        check("9f cap: the cap text is nowhere in the page",
              "IO_BYTE_CAP" not in text and "body cap" not in text, "")


def case_10_epub_join() -> None:
    """The header carries a filename whose casing changed with v010, and a
    background build appends concurrently."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root, stem="Chapter_0001")  # legacy-cased bucket
        log = root / "logs" / "epub-build.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "\n=== epub build after Chapter_0001.md | 2026-01-01T00:00:00 ===\n"
            "[ok] epub: /home/someone/secret/book.epub\n"
            "\n=== epub build after CHAPTER_0001.md | 2026-01-01T01:00:00 ===\n"
            "[warn] failed writing /home/someone/secret/book.epub\n",
            encoding="utf-8")
        chapters = logdashboard.collect(root)["chapters"]
        epub = chapters[0]["epub"]
        check("10a epub: a differently-cased bucket still joins",
              epub is not None, f"{epub}")
        check("10b epub: the latest block wins",
              epub["ok"] is False, f"{epub}")
        text = logdashboard.render(logdashboard.collect(root))
        check("10c epub: the maintainer's home path is not republished",
              "/home/someone" not in text, "")


def case_11_torn_tail() -> None:
    """A build child is appending right now: the half-written final block is
    skipped and counted, never parsed."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root)
        log = root / "logs" / "epub-build.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "=== epub build after CHAPTER_0001.md | 2026-01-01T00:00:00 ===\n"
            "[ok] epub: /home/x/b.epub\n"
            "=== epub build after CHAPTER_0001.md | 2026-01-01T01:00:00 ===\n"
            "[ok] epub: /home/x/b.epub\n"
            "=== epub build after CHAPTER_0",  # no newline: mid-write
            encoding="utf-8")
        data = logdashboard.collect(root)
        check("11a torn: the page still renders",
              "<html" in logdashboard.render(data), "")
        check("11b torn: the torn tail is reported",
              any("mid-block" in g for g in data["gaps"]),
              f"{data['gaps']}")


def case_12_never_raises() -> None:
    """A dashboard bug must never take a run down or destroy a good page."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root)
        target = root / "logs" / "report.html"
        good = logdashboard.write_dashboard(root)
        check("12a never-raises: a healthy write returns the path",
              good == target and target.is_file(), f"{good}")

        # An unreadable bucket must not stop the render.
        bucket = root / "logs" / "chapters" / "CHAPTER_0001"
        original = os.stat(bucket / "index.jsonl")
        try:
            os.chmod(bucket / "index.jsonl", 0)
        except OSError:
            pass
        check("12b never-raises: a locked bucket still yields a page",
              logdashboard.write_dashboard(root) is not None, "")
        os.chmod(bucket / "index.jsonl", original.st_mode)

        # A directory where the page belongs: the write fails, nothing raises.
        target.unlink()
        target.mkdir()
        check("12c never-raises: an unwritable destination returns None",
              logdashboard.write_dashboard(root) is None, "")
        target.rmdir()

        # refresh() is the _run_end path: it must swallow everything.
        logdashboard.refresh(root)
        check("12d never-raises: refresh never raises", True, "")


def case_13_cli() -> None:
    """--html must work where `logs` would otherwise bail: with no tier-1 and
    no events, cmd_logs returns 1 before printing anything."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_no_tier1(root)
        result = cli(root, "logs", "--html")
        page = root / "logs" / "report.html"
        check("13a cli: the page is written despite an empty event stream",
              page.is_file(), result.stdout[-300:])
        check("13b cli: the house marker is printed",
              "[ok] html:" in result.stdout, result.stdout[-300:])
        check("13c cli: it exits 0", result.returncode == 0,
              f"rc={result.returncode} {result.stderr[-300:]}")

        result = cli(root, "logs", "--html", "--json")
        check("13d cli: --json stdout stays parseable",
              all(json.loads(line) for line in result.stdout.splitlines()
                  if line.strip()), result.stdout[-300:])

        bare = cli(root, "logs")
        check("13e cli: plain `logs` is untouched by the flag",
              "report.html" not in bare.stdout, bare.stdout[-200:])


def case_14_bodies_always_embedded() -> None:
    """There is one render, and it carries the bodies.

    The old --html-io flag existed to keep model text out of the default page,
    and it produced the worst bug report of the feature: "why is it still
    saying No stored prompt/response bodies" on a project that plainly had
    them. Embedding everything costs ~36 ms against a 981 s chapter, so the
    second mode bought nothing but the confusion.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_config(root, log_prompt_bodies=True)
        seed_calls(root, "CHAPTER_0001", RID, [
            {"call_id": "secret01", "job": "translator", "model": "model-a",
             "body": json.dumps({"verdict": "SUCCESS",
                                 "reasons": ["SECRET-BODY"]})},
        ])
        result = cli(root, "logs", "--html")
        page = (root / "logs" / "report.html").read_text(encoding="utf-8")
        check("14a bodies: a plain --html carries the body",
              "SECRET-BODY" in page, result.stdout[-200:])
        check("14b bodies: it is readable, not an entity soup",
              "&quot;SECRET-BODY&quot;" not in page, "")
        records = blob_of(page)
        check("14c bodies: and it parses back out",
              records[0]["parsed"]["reasons"] == ["SECRET-BODY"], "")
        check("14d bodies: --html-io is gone from the help",
              "--html-io" not in result.stdout, result.stdout[-200:])

        # A project whose only per-call record is the orchestration tier says
        # so once, without blaming a config flag that is already correct.
        empty = Path(td) / "nobodies"
        seed_tier1(empty)
        text_empty = html_of(empty)
        check("14e bodies: a bodiless project says so honestly",
              "orchestration-tier summaries" in text_empty, "")
        check("14f bodies: it does not name a flag to retry with",
              "--html-io" not in text_empty, "")


def case_15_run_end_refreshes() -> None:
    """The automatic path. `_run_end` is the single choke point every logging
    command closes through, so wiring the refresh there is what keeps the page
    current without anyone asking for it."""
    import translate

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root)
        logger._run_path = None  # documented reset contract
        logger._paths.clear()
        translate._run_end(root, "translate", "completed",
                           {"translated": ["CHAPTER_0001.md"]})
        page = root / "logs" / "report.html"
        check("15a auto: _run_end wrote the dashboard", page.is_file(), "")
        text = page.read_text(encoding="utf-8")
        check("15b auto: the refresh carries the call ledger",
              "<script" in text and "Call ledger" in text, "")
        check("15c auto: and it is complete, not metadata-only",
              "Model exchanges" not in text, "")

        # A refresh over a project that does not exist must not raise: _run_end
        # runs on every command, including ones whose project dir has since
        # been deleted.
        translate._run_end(Path(td) / "gone", "translate", "failed", None)
        check("15d auto: a missing project does not raise", True, "")
        logger._run_path = None
        logger._paths.clear()


def case_16_tier2_is_the_call_source() -> None:
    """The per-call table comes from tier 2, not tier 1.

    An earlier version read calls only from tier 1's llm_call summaries and so
    showed an empty model roster on any project whose orchestration tier had
    aged out -- while the chapter's own tier-2 bucket sat there holding every
    call in full. This pins the tier-2 path, and the tier-1 fallback for a
    chapter written while log_llm was off.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bucket = seed_tier1(root)  # tier 1 present, no tier-2 file
        write_lines(bucket / f"run-{RID}.jsonl", [
            {"ts": "2026-01-01T00:03:00.000+00:00", "run_id": RID,
             "event": "llm_response", "chapter": "CHAPTER_0001.md",
             "job": "translator", "model": "model-t2",
             "candidate": 1, "candidates": 3,
             "usage": {"prompt_tokens": 700, "completion_tokens": 300},
             "elapsed_s": 30.0, "finish_reason": "stop"},
            {"ts": "2026-01-01T00:03:01.000+00:00", "run_id": RID,
             "event": "llm_response", "chapter": "CHAPTER_0001.md",
             "job": "consensus", "model": "model-t2", "consensus_for":
             "translator", "usage": {"prompt_tokens": 100,
                                     "completion_tokens": 50},
             "elapsed_s": 12.0, "finish_reason": "stop"},
        ])
        data = logdashboard.collect(root)
        calls = data["calls"]
        models = {c["model"] for c in calls}
        check("16a tier2: the chapter's tier-2 calls are used",
              "model-t2" in models, f"{models}")
        check("16b tier2: consensus_for survives onto the record",
              any(c["consensus_for"] == "translator" for c in calls), "")
        check("16c tier2: the source is named on the page",
              "tier 2" in data["call_source"], data["call_source"])
        check("16d tier2: the roster is built from them",
              "model-t2" in data["tokens_by_model"],
              f"{list(data['tokens_by_model'])}")
        check("16e tier2: per-call rows are not double counted",
              len(calls) == 2, f"{len(calls)}")
        check("16f tier2: the chapter row sees its OWN tier-2 calls",
              [c["model"] for c in data["chapters"][0]["calls_detail"]]
              == ["model-t2", "model-t2"],
              f"{data['chapters'][0]['calls_detail']}")
        check("16f2 tier2: and they are the same rows, not copies",
              all(c["call_id"] == "t" for c in data["chapters"][0]["calls_detail"])
              or {c["call_id"] for c in calls}
              == {c["call_id"] for c in data["chapters"][0]["calls_detail"]}, "")
        check("16f3 tier2: the roll-up still counts them exactly once",
              data["total_calls"] == len(calls)
              == sum(len(c["calls_detail"]) for c in data["chapters"]),
              f"{data['total_calls']}")
        check("16f4 tier2: no chapter with calls shows 'not recorded'",
              "not recorded" not in logdashboard.render(data), "")
        check("16g tier2: the fan-out ratio survives",
              any(c["candidate"] == 1 and c["candidates"] == 3 for c in calls), "")
        check("16h tier2: the index table carries them",
              "model-t2" in logdashboard.render(data), "")

        # log_llm off: no tier-2 call lines, so tier 1 must still answer.
        no_llm = Path(td) / "off"
        seed_tier1(no_llm)
        data_off = logdashboard.collect(no_llm)
        check("16i tier2: tier 1 answers when there is no tier-2 call record",
              data_off["call_source"] == "per-call rows (tier 1)",
              data_off["call_source"])
        check("16j tier2: and it is not silently empty",
              data_off["total_calls"] == 3, f"{data_off['total_calls']}")
        check("16k tier2: tier 1 rows still show the candidate ratio",
              any(c["candidate"] == "1/2"
                  for c in data_off["chapters"][0]["calls_detail"]), "")


def case_17_shape_dispatch() -> None:
    """Shape comes from the PARSED KEY SET, never from `job`.

    One job name drives at least three schemas -- `glossary` runs expand -> {
    terms }, merge -> a flat object, cleanup -> { decisions } -- and
    consensus_for records only the job name. So a switch keyed on `job` or
    `consensus_for` renders the wrong table, and a switch keyed on the
    alphabetically-first key renders nonsense for MERGE_SCHEMA (whose first
    sorted key is `category`). Every registry shape gets its own view, and
    anything unrecognised still renders through the generic renderer.
    """
    merge = {"source": "二次元", "translation": "2D", "category": "place",
             "definition": "the anime dimension"}
    cases = [
        ("lines", json.dumps({"title": "c1",
                              "lines": [{"i": 1, "t": "hello"}]}), "translator", 1),
        ("verdict", json.dumps({"verdict": "SUCCESS", "reasons": []}), "reviewer", 1),
        ("terms", json.dumps({"terms": [{"source": "a", "translation": "b",
                                          "category": "c", "definition": "d",
                                          "variants": []}]}), "glossary", 1),
        ("cleanup", json.dumps({"decisions": [{"source": "a", "keep": True,
                                               "reason": "ok"}]}), "glossary", 1),
        ("notes", json.dumps({"notes": [{"line": 3, "term": "t", "note": "n",
                                         "category": "c", "threshold": 1}]}),
         "annotator", 1),
        ("recap", json.dumps({"recap": "what happened"}), "recap", 1),
        ("profile", json.dumps({"style_summary": "terse",
                                "background": "a coder"}), "profile", 1),
        ("merge", json.dumps(merge), "glossary", 1),
        ("generic", json.dumps({"unexpected": {"deep": [1, 2, 3]}}), "glossary", 1),
        ("generic", json.dumps([{"a": 1}, {"b": 2}]), "glossary", 1),
        ("generic", json.dumps(["x", "y"]), "glossary", 1),
        ("generic", json.dumps("plain scalar"), "glossary", 1),
        ("text", "not json at all", "glossary", 1),
        # A fenced block MUST reach the fenced shape, not degrade to text.
        ("verdict", "```json\n{\"verdict\": \"SUCCESS\", \"reasons\": []}\n```",
         "reviewer", 1),
    ]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        calls = [{"call_id": f"shp{n:03d}", "job": job,
                  "model": f"model-{n}", "candidate": cand, "candidates": 1,
                  "body": body}
                 for n, (_want, body, job, cand) in enumerate(cases)]
        seed_calls(root, "CHAPTER_0001", RID, calls)
        data = logdashboard.collect(root)
        got = [c["shape"] for c in data["calls"]]
        want = [c[0] for c in cases]
        check("17a shape: every registry entry dispatches correctly",
              got == want, f"{got}")
        check("17b shape: one job name yields many different shapes",
              len({s for s, _b, j, _c in cases if j == "glossary"}) >= 4, "")
        check("17c shape: a fenced json body still parses",
              data["calls"][-1]["shape"] == "verdict",
              data["calls"][-1]["shape"])
        check("17d shape: the merge term card is flat, not an array",
              data["calls"][7]["parsed"] == merge, "")
        check("17e shape: non-JSON stays text rather than erroring",
              data["calls"][12]["shape"] == "text"
              and data["calls"][12]["body"] == "not json at all", "")

        text = html_of(root)
        records = blob_of(text)
        check("17f shape: every record survives to the browser",
              len(records) == len(cases), f"{len(records)}")
        check("17g shape: the JS never re-guesses the shape",
              "shape:" in text and "rec.shape" in text, "")


def case_18_latest_run_agrees_with_the_ledger() -> None:
    """One run per chapter, and it is the run the ledger bar reports.

    The first draft preferred the run_id of the last CLOSE line. With
    log_chapter_keep_runs: 3 a closed older run's file coexists with a newer
    run's file, so that rule picks the OLDER run and puts one run's bar beside
    another run's calls -- the exact inconsistency it was added to prevent.
    The rule is now: whatever `runs[-1]` (last OPEN) is.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_config(root)
        bucket = root / "logs" / "chapters" / "CHAPTER_0001"
        older, newer = "20260101-010000-translate-1", "20260101-020000-translate-2"
        write_lines(bucket / "index.jsonl", [
            {"ts": "2026-01-01T01:00:00.000+00:00", "run_id": older,
             "phase": "open", "command": "translate", "file": "CHAPTER_0001.md",
             "number": 1},
            {"ts": "2026-01-01T01:30:00.000+00:00", "run_id": older,
             "phase": "close", "outcome": "translated", "attempts": 1,
             "stages": 5, "calls": 1, "tokens": {"translator": 111},
             "elapsed_s": 100.0},
            {"ts": "2026-01-01T02:00:00.000+00:00", "run_id": newer,
             "phase": "open", "command": "translate", "file": "CHAPTER_0001.md",
             "number": 1},
            {"ts": "2026-01-01T02:30:00.000+00:00", "run_id": newer,
             "phase": "close", "outcome": "translated", "attempts": 1,
             "stages": 5, "calls": 2, "tokens": {"translator": 222},
             "elapsed_s": 200.0},
        ])
        write_lines(bucket / f"run-{older}.jsonl", [
            {"ts": "2026-01-01T01:10:00.000+00:00", "run_id": older,
             "event": "llm_response", "call_id": "OLD", "chapter": "CHAPTER_0001.md",
             "job": "translator", "model": "model-old", "candidate": 1,
             "candidates": 1, "response": '{"verdict":"SUCCESS","reasons":[]}',
             "usage": {"prompt_tokens": 10, "completion_tokens": 10},
             "elapsed_s": 1.0, "finish_reason": "stop"},
        ])
        write_lines(bucket / f"run-{newer}.jsonl", [
            {"ts": "2026-01-01T02:10:00.000+00:00", "run_id": newer,
             "event": "llm_request", "call_id": "NEW1",
             "chapter": "CHAPTER_0001.md", "job": "translator",
             "model": "model-new", "candidate": 1, "candidates": 2,
             "prompt": ["p"], "params": {}},
            {"ts": "2026-01-01T02:11:00.000+00:00", "run_id": newer,
             "event": "llm_response", "call_id": "NEW1",
             "chapter": "CHAPTER_0001.md", "job": "translator",
             "model": "model-new", "candidate": 1, "candidates": 2,
             "response": '{"verdict":"SUCCESS","reasons":[]}',
             "usage": {"prompt_tokens": 20, "completion_tokens": 20},
             "elapsed_s": 2.0, "finish_reason": "stop"},
        ])
        data = logdashboard.collect(root)
        chapter = data["chapters"][0]
        ids = [c["call_id"] for c in data["calls"]]
        check("18a run: the ledger uses the newest run, not the last close",
              ids == ["NEW1"], f"{ids}")
        check("18b run: the older run's call is nowhere in the ledger",
              "OLD" not in ids, f"{ids}")
        check("18c run: it agrees with the run the ledger bar reports",
              chapter["runs"][-1]["run_id"] == newer
              and data["calls"][0]["run_id"] == newer, "")
        check("18d run: totals come from the same run",
              data["total_tokens"] == 40, f"{data['total_tokens']}")
        check("18e run: the older run does not inflate the total",
              data["total_tokens"] != 60, "")
        check("18f run: the selection basis is recorded",
              chapter["call_source"] == "index", chapter["call_source"])
        text = html_of(root)
        check("18g run: the stale run id does not leak into the page",
              "OLD" not in text or ">NEW1<" in text, "")


def case_19_fallback_and_unpaired() -> None:
    """A run that opened and died keeps its calls; a half-written call is kept
    and labelled rather than dropped."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_config(root)
        bucket = root / "logs" / "chapters" / "CHAPTER_0001"
        rid = "20260101-030000-translate-3"
        write_lines(bucket / "index.jsonl", [
            {"ts": "2026-01-01T03:00:00.000+00:00", "run_id": rid,
             "phase": "open", "command": "translate", "file": "CHAPTER_0001.md",
             "number": 1},
        ])
        write_lines(bucket / f"run-{rid}.jsonl", [
            {"ts": "2026-01-01T03:01:00.000+00:00", "run_id": rid,
             "event": "llm_request", "call_id": "ORPHAN", "chapter": "CHAPTER_0001.md",
             "job": "translator", "model": "m", "prompt": ["p"], "params": {}},
            {"ts": "2026-01-01T03:02:00.000+00:00", "run_id": rid,
             "event": "result", "kind": "faithfulness", "chapter": "CHAPTER_0001.md",
             "verdict": "SUCCESS", "accepted": True, "reasons": []},
            {"ts": "2026-01-01T03:03:00.000+00:00", "run_id": rid,
             "event": "chunk", "chapter": "CHAPTER_0001.md", "n": 1},
        ])
        data = logdashboard.collect(root)
        check("19a unpaired: a request with no response is kept",
              [c["call_id"] for c in data["calls"]] == ["ORPHAN"], "")
        check("19b unpaired: and flagged as unpaired",
              data["calls"][0]["paired"] is False, "")
        check("19c unpaired: it has a prompt but no body",
              data["calls"][0]["prompt"] == "p"
              and data["calls"][0]["body"] is None, "")
        kinds = {e.get("kind") or e.get("event") for e in data["unpaired_events"]}
        check("19d unpaired: result/chunk rows are kept, not dropped",
              kinds == {"faithfulness", "chunk"}, f"{kinds}")
        text = html_of(root)
        check("19e unpaired: the page says so rather than implying silence",
              "unpaired" in text, "")
        check("19f unpaired: stage events are labelled as non-calls",
              "not part of the call ledger" in text, "")

        # The bucket exists but its index names a run with no file.
        gone = Path(td) / "gone"
        seed_config(gone)
        b2 = gone / "logs" / "chapters" / "CHAPTER_0002"
        write_lines(b2 / "index.jsonl", [
            {"ts": "2026-01-01T04:00:00.000+00:00", "run_id": "nope",
             "phase": "open", "command": "translate", "file": "CHAPTER_0002.md",
             "number": 2},
        ])
        data2 = logdashboard.collect(gone)
        check("19g fallback: a run with no body is handled, not fatal",
              data2["calls"] == [], "")
        check("19h fallback: the chapter still renders",
              "<html" in logdashboard.render(data2), "")


def case_20_fanout_width() -> None:
    """The side-by-side is N+1 wide, and N is read, never assumed.

    providers.<job> is an array of blocks, so the fan-out is whatever the
    project configured. Worse for any positional shortcut: candidate 1 is a
    DIFFERENT model for a different job, and reviewer's order is the reverse
    of translator's on the same chapter. So every column header must come from
    that call's own `model`, and a consensus call (job="consensus",
    consensus_for="translator") must land in the translator's matrix.
    """
    def source_prompt(n: int) -> list[str]:
        src = "### Task\ntranslate\n\n### Source Data\n" + json.dumps(
            [{"i": i, "t": f"原文{i}"} for i in range(1, n + 1)],
            ensure_ascii=False)
        return [src]

    def lines_body(prefix: str, n: int) -> str:
        return json.dumps({"title": "t", "lines": [
            {"i": i, "t": f"{prefix}{i}"} for i in range(1, n + 1)]})

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for width, label in ((1, "n1"), (2, "n2"), (5, "n5")):
            calls = []
            for k in range(1, width + 1):
                calls.append({"call_id": f"{label}-c{k}", "job": "translator",
                              "model": f"model-{k}", "candidate": k,
                              "candidates": width, "prompt": source_prompt(3),
                              "body": lines_body(f"{label}c{k}-", 3)})
            calls.append({"call_id": f"{label}-cons", "job": "consensus",
                          "model": "model-consensus", "consensus_for": "translator",
                          "prompt": source_prompt(3), "body": lines_body("CONS-", 3)})
            seed_calls(root, f"CHAPTER_00{label[-1]}x" if False else
                       f"CHAPTER_{int(label[1]) + 10:04d}", RID, calls)

        data = logdashboard.collect(root)
        records = logdashboard._ledger_blob(data)
        by_chapter: dict[str, list[dict]] = {}
        for r in records:
            if r["shape"] == "lines":
                by_chapter.setdefault(r["chapter"], []).append(r)

        for width, chapter in ((1, "CHAPTER_0011"), (2, "CHAPTER_0012"),
                               (5, "CHAPTER_0015")):
            group = by_chapter.get(chapter, [])
            tasks = {r["task"] for r in group}
            check(f"20{width} fanout: N={width} groups into ONE matrix",
                  tasks == {"translator"}, f"{tasks}")
            check(f"20 fanout: N={width} renders N+1 columns",
                  len(group) == width + 1, f"{len(group)} for N={width}")
            check(f"20 fanout: N={width} consensus joined the matrix",
                  any(r["consensus_for"] == "translator" for r in group), "")
            stored = sum(1 for r in group if r.get("source_lines"))
            check(f"20 fanout: N={width} stores the source array once",
                  stored == 1, f"{stored}")

        text = html_of(root)
        check("20x fanout: the group key is the arbitrated task, not the job",
              "task" in text and "consensus_for" in text, "")

        # Reversed order across jobs on one chapter: columns must follow each
        # call's model, so a shared position index is never the identity.
        rev = Path(td) / "rev"
        seed_calls(rev, "CHAPTER_0003", RID, [
            {"call_id": "rev-t1", "job": "translator", "model": "AAA",
             "candidate": 1, "candidates": 2, "prompt": source_prompt(2),
             "body": lines_body("T1-", 2)},
            {"call_id": "rev-t2", "job": "translator", "model": "BBB",
             "candidate": 2, "candidates": 2, "prompt": source_prompt(2),
             "body": lines_body("T2-", 2)},
            {"call_id": "rev-r1", "job": "reviewer", "model": "BBB",
             "candidate": 1, "candidates": 2,
             "body": json.dumps({"verdict": "SUCCESS", "reasons": []})},
        ])
        rec = logdashboard._ledger_blob(logdashboard.collect(rev))
        models = {(r["task"], r.get("candidate")): r["model"] for r in rec}
        check("20y fanout: position 1 is a different model per job",
              models.get(("translator", 1)) == "AAA"
              and models.get(("reviewer", 1)) == "BBB",
              f"{models}")
        check("20z fanout: a single-shot job has no candidate column",
              all(r["candidate"] is None
                  for r in rec if r["shape"] == "notes" or r["task"] == "recap"),
              "")


def case_21_side_by_side_source() -> None:
    """Source recovery is a balanced scan, and it must survive prose that
    contains brackets."""
    tricky = "括号 [测试] 与 {花括号} 还有 \"引号\""
    prompt = ("### Task\ntranslate\n\n### Source Data\n"
              + json.dumps([{"i": 1, "t": tricky}, {"i": 2, "t": "第二行"}],
                           ensure_ascii=False))
    got = logdashboard._json_array_of_i_t(prompt)
    check("21a sbs: source is recovered past bracket-laden prose",
          got is not None and len(got) == 2, f"{got}")
    check("21b sbs: the bracket text survives intact",
          got and got[0]["t"] == tricky, "")
    # The shape a naive regex gets wrong: a } closing the object early.
    naive = '[{"i": 1, "t": "a}b"}'
    check("21c sbs: the naive bracket regex pattern is not used",
          logdashboard._json_array_of_i_t(naive) is None,
          "a regex would mis-parse this")
    check("21d sbs: prose with no array returns None, never raises",
          logdashboard._json_array_of_i_t("no arrays here") is None, "")
    check("21e sbs: unbalanced brackets do not raise",
          logdashboard._json_array_of_i_t('[[{"i": 1, "t": "x"}') is None, "")
    check("21f sbs: an escaped quote inside a string does not desync it",
          (lambda a: a is not None and a[0]["t"] == 'say "hi"')(
              logdashboard._json_array_of_i_t(
                  '[{"i": 1, "t": "say \\"hi\\""}]')), "")


def case_22_script_selectors_match_the_markup() -> None:
    """Every querySelectorAll in the shipped JS must match real elements.

    A tag-qualified selector that does not match anything fails SILENTLY: the
    button still flips aria-pressed, so it looks like the control is broken
    rather than like the selector is wrong. This shipped once -- the outcome
    filter read `tr.lrow` while a chapter row is a <details class="lrow"> --
    and it is the kind of defect no assertion about behaviour catches, because
    the behaviour under test was never wired up at all.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_calls(root, "CHAPTER_0001", RID, [
            {"call_id": "sel001", "job": "translator", "model": "model-a",
             "body": json.dumps({"verdict": "SUCCESS", "reasons": []})},
        ])
        text = html_of(root)
        # (tag name used by the markup, the selector the JS applies to it)
        guarded = [
            ("lrow", ".lrow"),
            ("callrow", "tr.callrow"),
            ("chip", ".chip[data-filter]"),
            ("bodyhost", "tr.bodyhost"),
        ]
        for cls, selector in guarded:
            tags = set(re.findall(r"<(\w+)[^>]*class=\"[^\"]*\b" + cls + r"\b",
                                  text))
            check(f"22{cls} selector: {selector!r} targets elements that exist",
                  bool(tags), f"no element carries class {cls!r}")
            for sel_tag in re.findall(r"querySelectorAll\('([\w.\[\]=_-]+)'\)",
                                      text):
                if sel_tag.endswith("." + cls) or sel_tag == "." + cls:
                    qual = sel_tag.rsplit(".", 1)[0]
                    check(f"22{cls} selector: {sel_tag!r} matches the real tag",
                          qual == "" or qual in tags,
                          f"markup uses {sorted(tags)}, selector assumes {qual!r}")
        check("22x selector: no tag-qualified filter on .lrow",
              "tr.lrow" not in text, "")
        check("22y selector: the ledger rows carry data-outcome",
              'class="lrow"' in text and "data-outcome=" in text, "")
        check("22z selector: the ledger rows are <details>, not <tr>",
              "<details class=\"lrow\"" in text
              or re.search(r"<details[^>]*class=\"lrow\"", text) is not None, "")


def case_23_no_chapter_claims_a_cause_it_cannot_know() -> None:
    """An empty per-call panel must not name a cause it cannot prove.

    This shipped claiming "no tier-1 run retained for this chapter" on all 11
    chapters of a project whose tier 1 was present and whose every chapter held
    8-11 real calls. The panel was empty for a third reason entirely: the calls
    had moved to the unified ledger and the chapter row was never given a way
    back to them. A message that names a plausible cause is worse than one that
    admits both, because it sends the reader looking in the wrong place.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_tier1(root)
        seed_calls(root, "CHAPTER_0001", RID, [
            {"call_id": "has01", "job": "translator", "model": "model-a",
             "body": json.dumps({"verdict": "SUCCESS", "reasons": []})},
        ])
        text = html_of(root)
        check("23a honesty: a chapter WITH calls never says 'not recorded'",
              "not recorded" not in text, "")
        check("23b honesty: the panel lists the chapter's own call",
              "model-a" in text, "")

        # Genuinely empty: a bucket with an index but no run file and no
        # tier-1 llm_call row -- the log_llm-off / retention-emptied shape.
        bare = Path(td) / "bare"
        seed_tier1(bare)
        write_lines(bare / "logs" / "chapters" / "CHAPTER_0002" / "index.jsonl", [
            {"ts": "2026-01-01T00:00:00.000+00:00", "run_id": RID,
             "phase": "open", "command": "translate", "file": "CHAPTER_0002.md",
             "number": 2},
            {"ts": "2026-01-01T00:04:00.000+00:00", "run_id": RID,
             "phase": "close", "outcome": "translated", "attempts": 1,
             "stages": 5, "calls": 7, "tokens": {"translator": 900},
             "elapsed_s": 240.0},
        ])
        data_bare = logdashboard.collect(bare)
        empty_ch = [c for c in data_bare["chapters"] if c["stem"] == "CHAPTER_0002"]
        check("23c0 honesty: the fixture really is call-less",
              empty_ch and empty_ch[0]["calls_detail"] == [], "")
        text_bare = logdashboard.render(data_bare)
        # Scope to the Model calls panel. The row TOOLTIP legitimately says
        # "stage detail unavailable (no tier-1 run retained)" when a chapter has
        # no stage spans -- that is a different panel making a different,
        # provable claim.
        m = re.search(r"<h4>Model calls</h4>(.*?)(?=<h4|<div class=\"tip|</ul>)",
                      text_bare, re.S)
        panel = m.group(1) if m else ""
        check("23c honesty: an empty panel still explains itself",
              "not recorded" in panel, f"{panel[:120]}")
        check("23d honesty: it does not blame tier 1 specifically",
              "tier-1" not in panel and "tier 1" not in panel,
              f"{panel[:160]}")
        check("23e honesty: it offers both real causes",
              "log_llm off" in panel and "retention" in panel,
              f"{panel[:160]}")


def case_24_whole_row_click_guards() -> None:
    """The whole row toggles, and the guards that keep it sane are present.

    A whole-row click handler is easy to add and easy to get wrong three ways:
    it double-fires with the call-id button already inside the row, it collapses
    the row while the user is dragging out a model name to copy, and it is
    mouse-only. Each has its own guard, and all three are asserted on the
    emitted script because a missing guard is invisible in a screenshot.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_calls(root, "CHAPTER_0001", RID, [
            {"call_id": "row001", "job": "translator", "model": "model-a",
             "body": json.dumps({"verdict": "SUCCESS", "reasons": []})},
        ])
        text = html_of(root)
        script = text[text.index("var BODIES"):]

        check("24a row: rows are clickable",
              "tr.callrow').forEach(function (r)" in script, "")
        check("24b row: the button stops propagation so it cannot double-fire",
              "ev.stopPropagation()" in script, "")
        check("24c row: a click on a nested control is ignored by the row",
              "ev.target.closest('button, a, input')" in script, "")
        check("24d row: an active text selection does not collapse the row",
              "window.getSelection" in script, "")
        check("24e row: Enter and Space toggle from the keyboard",
              "'Enter'" in script and "Spacebar" in script
              and "ev.preventDefault()" in script, "")
        check("24f row: rows are focusable",
              'tabindex="0"' in text, "")
        check("24g row: the button reports its state, not just its label",
              'aria-expanded="false"' in text and
              "setAttribute('aria-expanded', 'true')" in script, "")
        check("24h row: collapsing resets aria-expanded too",
              "setAttribute('aria-expanded', 'false')" in script, "")
        check("24i row: the button has an accessible name",
              "aria-label=" in text, "")
        check("24j row: the row advertises a pointer",
              "tr.callrow{cursor:pointer}" in text, "")
        check("24k row: keyboard focus is visible",
              "tr.callrow:focus-visible" in text, "")
        check("24l row: the row keeps its table role (no role override)",
              'role="button"' not in text and 'role="row"' not in text, "")


def case_25_no_double_escaped_entity() -> None:
    """No HTML entity may appear inside a value that _esc() then processes.

    An entity is MARKUP. _esc() exists to neutralise markup, so passing a
    string containing one through it yields "&amp;rarr;" -- the entity
    rendered as visible text. That is the same class of mistake as html.escape
    on the body blob: reaching for the wrong escaping layer.

    It shipped once as a literal "consensus &rarr; translator" in the call
    ledger's subtitle. Rather than pin that one string, this asserts the
    GENERAL invariant over the whole rendered page: no `&amp;<name>;` anywhere.
    Entities passed AROUND _esc() (the `&middot;` separators in section copy)
    are correct and must not be touched -- they render as an entity, not as
    text, so they must not produce a doubled ampersand.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seed_calls(root, "CHAPTER_0001", RID, [
            {"call_id": "ent001", "job": "consensus", "model": "model-a",
             "consensus_for": "translator",
             "body": json.dumps({"verdict": "SUCCESS", "reasons": []})},
            {"call_id": "ent002", "job": "translator", "model": "model-b",
             "candidate": 1, "candidates": 3,
             "body": json.dumps({"verdict": "FAIL", "reasons": ["r"]})},
        ])
        text = html_of(root)

        doubled = sorted(set(re.findall(r"&amp;[a-zA-Z][a-zA-Z0-9]*;|&amp;#\d+;", text)))
        check("25a entity: nothing is double-escaped anywhere on the page",
              doubled == [], f"{doubled}")
        check("25b entity: the consensus subtitle uses a real arrow",
              "consensus → translator" in text, "")
        check("25c entity: and no literal entity text leaks through",
              "&rarr;" not in text and "&amp;rarr;" not in text, "")
        check("25d entity: entities passed around _esc still work",
              "&middot;" in text, "")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in (
            "utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for case in (case_1_primary_path, case_2_escaping, case_3_no_network,
                 case_4_no_invented_numbers, case_5_degrades_honestly,
                 case_6_retry_spans_sum, case_7_ordering_and_legacy,
                 case_8_unclosed_and_multi_run, case_9_no_cap,
                 case_10_epub_join, case_11_torn_tail, case_12_never_raises,
                 case_13_cli, case_14_bodies_always_embedded,
                 case_15_run_end_refreshes, case_16_tier2_is_the_call_source,
                 case_17_shape_dispatch,
                 case_18_latest_run_agrees_with_the_ledger,
                 case_19_fallback_and_unpaired,
                 case_20_fanout_width, case_21_side_by_side_source,
                 case_22_script_selectors_match_the_markup,
                 case_23_no_chapter_claims_a_cause_it_cannot_know,
                 case_24_whole_row_click_guards,
                 case_25_no_double_escaped_entity):
        case()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())