"""Tests for lib/consensus.py: the multi-model fan-out and consensus merge
sitting between the pipeline and client.chat.

Covered, per the failure policy in the module docstring: (a) a
single-block job makes exactly ONE client.chat call with that block,
returns its text, prints no [consensus] line, and its trace hook writes
a JSONL line tagged {"job": ...} under <project>/logs/; (b) a two-block
job fans the TASK prompt out to both blocks (task json_schema and
max_tokens passed through verbatim) and makes one consensus call over
the shipped consensus.md template -- prompt carries "consensus
arbitrator", "### Candidate N (model: ...)" sections, and the task
prompt verbatim; json_schema passes through and max_tokens is
max(task, consensus block); the "[consensus] ... N model(s)" console
line prints exactly ONCE per process even across repeated chat()
invocations (the _ANNOUNCED dedup), with no [warn]; (b2) the consensus
max_tokens floor: a consensus block BELOW the task cap never lowers it;
(c) meta keys: invoking every captured meta_hook writes fan-out lines
tagged "candidate" (1-based) / "candidates" and a consensus line tagged
"job": "consensus" + "consensus_for"; (d) a failed candidate 2 degrades
to the single survivor with the exact [warn] lines and NO consensus
call; (e) the mirror case (candidate 1 fails, survivor 2 wins); (f) all
candidates failed re-raises client.LLMError; (g) a failed consensus
call returns the first survivor verbatim with the exact degrade [warn];
(h) an explicitly authored 2-block consensus array fails load_config
with the exact contractual ValueError; (i) a block whose model is
unresolvable labels its candidate "(unresolved model)"; (local) a
project-local templates/consensus.md wins over the skill-assets copy,
which the remaining cases exercise as the fallback.

Mechanics: client.chat is faked by ATTRIBUTE SWAP on the lib.client
module singleton (orig/restore in try/finally -- no unittest.mock);
consensus.py resolves client.chat at call time and its worker threads
submit the same module object's attribute, so the fake intercepts fan-
out calls too. The fake records every call as a dict appended to a
plain list (GIL-atomic) and calls are CLASSIFIED BY PROMPT CONTENT,
never by call order (fan-out carries the task prompt VERBATIM; anything
else is the consensus call, whose prompt wraps the task -- so a
project-local consensus.md template classifies the same way). Failures
are scripted per provider-block model as raised client.LLMError. Between cases the
module globals reset: consensus._ANNOUNCED.clear() (announce dedup) and
logger._run_path = None (each sandbox's logs/ owns its run's trace --
the test_cleanup_flow._TOKEN_CAP_WARNED precedent). cfg dicts are hand-
built (never load_config'd) with array-shaped providers. The consensus
fan-out's Ctrl-C safety is NOT covered: intercepting a ThreadPoolExecutor
shutdown needs thread-timing games no deterministic fake can play.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_consensus.py
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

# scripts/ (and therefore lib/) lives at novel-translator/scripts
# relative to this file (CWD-independent).
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, config, consensus, logger  # noqa: E402

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


ANNOUNCE = "[consensus] translator: 2 model(s) - merging results via the consensus provider"
TASK_PROMPT = (
    "Translate the following chapter into English.\n\n"
    "### Source Data\n\n第一行正文。\n第二行正文。\n"
)


def block(model: str | None, base_url: str = "http://fake:1/v1",
          temperature: float = 0.7, max_tokens: int = 4096) -> dict:
    """A hand-built provider block shaped like a normalized one."""
    return {"base_url": base_url, "model": model,
            "temperature": temperature, "max_tokens": max_tokens,
            "thinking": False}


def make_project(root: Path, name: str,
                 local_consensus: str | None = None) -> Path:
    """A sandbox project with a templates/ dir; consensus.md is left OUT so
    consensus.chat exercises the skill-assets fallback, unless
    local_consensus text is given (then it becomes the project's copy)."""
    proj = root / name
    (proj / "templates").mkdir(parents=True)
    if local_consensus is not None:
        (proj / "templates" / "consensus.md").write_text(
            local_consensus, encoding="utf-8", newline="\n")
    return proj


def two_block_cfg(cblock: dict, b1: dict | None = None,
                  b2: dict | None = None) -> dict:
    return {"providers": {"translator": [b1 or block("m1"), b2 or block("m2")],
                          "consensus": [cblock]},
            "log_llm": True}


def read_log_events(proj: Path) -> list[dict]:
    """Every parsed JSONL run line under <proj>/logs/, at any depth.

    These cases make chapter-less calls, so their model IO lands in the
    project bucket (logs/project/) rather than the tier-1 root -- a
    chapter-carrying case would land under logs/chapters/<stem>/. Recursing
    finds all three without the helper needing to know which."""
    events: list[dict] = []
    for path in sorted((proj / "logs").rglob("run-*.jsonl")):
        events.extend(json.loads(line)
                      for line in path.read_text(encoding="utf-8").splitlines()
                      if line.strip())
    return events


def _events_named(proj: Path, name: str) -> list[str]:
    """Reasons of every logged event with this name, for a negative assertion
    ("this event must no longer be emitted here")."""
    return [str(e.get("reason", "")) for e in read_log_events(proj)
            if e.get("event") == name]


def reset_globals() -> None:
    """Per-case module resets: the announce dedup and the logger's run
    pointer (so each sandbox's logs/ owns its run's trace)."""
    consensus._ANNOUNCED.clear()
    logger._run_path = None


class FakeChat:
    """Stand-in for client.chat, installed by attribute swap on the
    lib.client module. Records every call as a dict (classified later BY
    PROMPT CONTENT, never order) and never invokes meta_hook itself --
    the tests invoke captured hooks explicitly for trace-log checks.
    Failures are scripted per provider-block model (fan-out candidates)
    or for the consensus call as a whole."""

    def __init__(self, fail_models: tuple[str, ...] = (),
                 fail_consensus: bool = False):
        self.calls: list[dict] = []
        self.fail_models = set(fail_models)
        self.fail_consensus = fail_consensus

    def __call__(self, provider_cfg: dict, prompt: str,
                 json_schema: dict | None = None,
                 max_tokens: int | None = None,
                 meta_hook=None,
                 enforce_ceiling: bool = True) -> str:
        self.calls.append({"block": provider_cfg, "prompt": prompt,
                           "json_schema": json_schema,
                           "max_tokens": max_tokens,
                           "enforce_ceiling": enforce_ceiling,
                           "meta_hook": meta_hook})
        if prompt != TASK_PROMPT:
            # The consensus arbitration prompt (whatever template built it
            # -- shipped or project-local, it always embeds the task).
            if self.fail_consensus:
                raise client.LLMError("consensus endpoint down")
            return "merged-final"
        model = provider_cfg.get("model")
        if model in self.fail_models:
            raise client.LLMError(f"boom {model}")
        return f"reply-{model}"

    def fanout(self) -> list[dict]:
        """Candidate calls: the task prompt verbatim."""
        return [c for c in self.calls if c["prompt"] == TASK_PROMPT]

    def consensus(self) -> list[dict]:
        """Consensus calls: any prompt OTHER than the task prompt verbatim
        (the arbitration prompt wraps the task, whichever template built
        it -- the shipped copy or a project-local override)."""
        return [c for c in self.calls if c["prompt"] != TASK_PROMPT]


@contextlib.contextmanager
def patched_chat(fake: FakeChat):
    """Swap client.chat on the module singleton (worker threads resolve the
    same module object, so pool.submit captures the fake too)."""
    orig = client.chat
    client.chat = fake
    try:
        yield
    finally:
        client.chat = orig


@contextlib.contextmanager
def patched_resolve_model(raisr):
    """Swap client.resolve_model (label resolution only; avoids any network)."""
    orig = client.resolve_model
    client.resolve_model = raisr
    try:
        yield
    finally:
        client.resolve_model = orig


def case_a_single_block() -> None:
    """One block: one call, its text back, silent console, trace line
    tagged with the job name."""
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        b1 = block("m1")
        cfg = {"providers": {"translator": [b1],
                             "consensus": [block("c-merge", temperature=0.2)]},
               "log_llm": True}
        schema = {"type": "object"}
        fake = FakeChat()
        buf = io.StringIO()
        with patched_chat(fake), contextlib.redirect_stdout(buf):
            text = consensus.chat(proj, cfg, "translator", TASK_PROMPT,
                                  json_schema=schema, max_tokens=1000)
        out = buf.getvalue()
        check("a1 single block: exactly one chat call with that block and the task args",
              len(fake.calls) == 1 and fake.calls[0]["block"] is b1
              and fake.calls[0]["prompt"] == TASK_PROMPT
              and fake.calls[0]["json_schema"] is schema
              and fake.calls[0]["max_tokens"] == 1000,
              f"calls={len(fake.calls)}")
        check("a2 single block: returns its text, no consensus/console output",
              text == "reply-m1" and out == "",
              f"text={text!r} out={out!r}")
        hook = fake.calls[0]["meta_hook"]
        check("a3 single block: meta_hook captured for the call",
              callable(hook))
        hook({"event": "llm_request", "model": "m1", "call_id": "case-a"})
        events = read_log_events(proj)
        hit = [e for e in events if e.get("call_id") == "case-a"]
        check("a4 single block: hook writes a JSONL line tagged job=translator",
              len(hit) == 1 and hit[0]["job"] == "translator"
              and hit[0]["event"] == "llm_request"
              and hit[0]["model"] == "m1",
              f"hit={hit!r}")


def case_b_two_blocks() -> None:
    """Two blocks: fan-out + one consensus call over the shipped template
    (skill-assets fallback: the sandbox templates/ has no consensus.md),
    exact announce line once across TWO chat() invocations (dedup)."""
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cblock = block("c-merge", temperature=0.2, max_tokens=2048)
        cfg = two_block_cfg(cblock)
        schema = {"type": "object", "properties": {"lines": {"type": "array"}}}
        fake = FakeChat()
        buf = io.StringIO()
        with patched_chat(fake), contextlib.redirect_stdout(buf):
            r1 = consensus.chat(proj, cfg, "translator", TASK_PROMPT,
                                json_schema=schema, max_tokens=1000)
            r2 = consensus.chat(proj, cfg, "translator", TASK_PROMPT,
                                json_schema=schema, max_tokens=1000)
        out = buf.getvalue()
        fan, con = fake.fanout(), fake.consensus()
        check("b1 two blocks: two fan-out calls per invocation, task args verbatim",
              len(fan) == 4
              and all(c["json_schema"] is schema and c["max_tokens"] == 1000
                      for c in fan)
              and {c["block"]["model"] for c in fan} == {"m1", "m2"},
              f"fan={len(fan)}")
        check("b2 two blocks: one consensus call per invocation with the cblock",
              len(con) == 2
              and all(c["block"] is cblock for c in con),
              f"con={len(con)}")
        c = con[0]
        check("b3 two blocks: consensus prompt renders the template with both candidates",
              "You are the consensus arbitrator." in c["prompt"]
              and "### Candidate 1 (model: m1)\n\nreply-m1" in c["prompt"]
              and "### Candidate 2 (model: m2)\n\nreply-m2" in c["prompt"]
              and TASK_PROMPT in c["prompt"],
              f"prompt={c['prompt'][:300]!r}")
        check("b4 two blocks: consensus call keeps the schema, caps at max(task, cblock)",
              c["json_schema"] is schema and c["max_tokens"] == 2048,
              f"schema={c['json_schema']!r} max_tokens={c['max_tokens']!r}")
        check("b5 two blocks: the consensus response is the return value",
              r1 == r2 == "merged-final", f"r1={r1!r} r2={r2!r}")
        check("b6 two blocks: announce line exactly once across both invocations, no warns",
              out.count(ANNOUNCE) == 1 and "[warn] consensus:" not in out,
              f"count={out.count(ANNOUNCE)} out={out!r}")

    # The cap floor: a consensus block BELOW the task's explicit
    # max_tokens must never lower it (the synthesis must fit what the
    # task's own contract allows).
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = two_block_cfg(block("c-merge", temperature=0.2, max_tokens=512))
        fake = FakeChat()
        with patched_chat(fake):
            consensus.chat(proj, cfg, "translator", TASK_PROMPT,
                           json_schema=None, max_tokens=4096)
        con = fake.consensus()
        check("b7 two blocks: consensus max_tokens never drops below the task cap",
              len(con) == 1 and con[0]["max_tokens"] == 4096,
              f"max_tokens={con[0]['max_tokens'] if con else None!r}")
        # The synthesis is the ONE call site allowed to exceed its block's own
        # max_tokens. Without the opt-out, client.chat's ceiling clamp would
        # cap this merge at 512 -- a third of what the candidates need -- and
        # the fake here could not catch it, because it replaces client.chat
        # outright. Assert the flag, not just the arithmetic.
        check("b8 two blocks: synthesis opts out of the client ceiling clamp",
              len(con) == 1 and con[0]["enforce_ceiling"] is False,
              f"enforce_ceiling={con[0]['enforce_ceiling'] if con else None!r}")
        fan = [c for c in fake.calls if c["prompt"] == TASK_PROMPT]
        check("b9 two blocks: fan-out candidates stay clamped",
              len(fan) == 2 and all(c["enforce_ceiling"] is True for c in fan),
              f"n={len(fan)} "
              f"flags={[c['enforce_ceiling'] for c in fan]!r}")


def case_c_meta_keys() -> None:
    """Trace tags: fan-out lines carry candidate/candidates; the consensus
    line carries job=consensus + consensus_for."""
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = two_block_cfg(block("c-merge", temperature=0.2))
        fake = FakeChat()
        with patched_chat(fake):
            consensus.chat(proj, cfg, "translator", TASK_PROMPT,
                           json_schema=None, max_tokens=1000)
        # Invoke every captured hook with a sample meta -- the real
        # client.chat would do this before/after each request.
        for i, call in enumerate(fake.calls):
            call["meta_hook"]({"event": "llm_request",
                               "model": call["block"].get("model"),
                               "call_id": f"case-c-{i}"})
        events = read_log_events(proj)
        fan_lines = [e for e in events if e.get("job") == "translator"]
        con_lines = [e for e in events if e.get("job") == "consensus"]
        check("c1 meta: fan-out lines carry 1-based candidate and candidates counts",
              len(fan_lines) == 2
              and sorted(e["candidate"] for e in fan_lines) == [1, 2]
              and all(e["candidates"] == 2 for e in fan_lines),
              f"fan={fan_lines!r}")
        check("c2 meta: consensus line tagged job=consensus with consensus_for",
              len(con_lines) == 1
              and con_lines[0]["consensus_for"] == "translator"
              and con_lines[0]["model"] == "c-merge",
              f"con={con_lines!r}")


def case_d_second_candidate_fails() -> None:
    """Candidate 2 fails after its retries: the run STOPS.

    This case used to pin the opposite -- survivor 1's text returned verbatim
    with a `[warn] ... continuing with the remaining candidates`. That fallback
    is gone. A chapter assembled from whichever model happened to answer is not
    the chapter the project configured, and the multi-model merge the run was
    set up for silently did not happen.
    """
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = two_block_cfg(block("c-merge", temperature=0.2))
        fake = FakeChat(fail_models=("m2",))
        buf = io.StringIO()
        exc: Exception | None = None
        with patched_chat(fake), contextlib.redirect_stdout(buf):
            try:
                consensus.chat(proj, cfg, "translator", TASK_PROMPT)
            except Exception as caught:  # noqa: BLE001 - the caller asserts on it
                exc = caught
        out = buf.getvalue()
        check("d1 candidate 2 failed: the call raises instead of returning",
              exc is not None and isinstance(exc, client.LLMError),
              f"exc={exc!r}")
        check("d2 candidate 2 failed: no consensus call was made",
              len(fake.consensus()) == 0, f"con={len(fake.consensus())}")
        check("d3 candidate 2 failed: one [FAIL] naming the model, no fallback",
              "[FAIL] consensus: translator candidate 2/2 (m2) failed: "
              "LLMError: boom m2" in out
              and "continuing with the remaining candidates" not in out,
              f"out={out!r}")


def case_e_first_candidate_fails() -> None:
    """The mirror: candidate 1 fails, and that stops the run too."""
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = two_block_cfg(block("c-merge", temperature=0.2))
        fake = FakeChat(fail_models=("m1",))
        buf = io.StringIO()
        exc: Exception | None = None
        with patched_chat(fake), contextlib.redirect_stdout(buf):
            try:
                consensus.chat(proj, cfg, "translator", TASK_PROMPT)
            except Exception as caught:  # noqa: BLE001 - the caller asserts on it
                exc = caught
        check("e1 candidate 1 failed: raises, no survivor is substituted",
              exc is not None and isinstance(exc, client.LLMError)
              and len(fake.consensus()) == 0
              and "candidate 1/2 (m1) failed: LLMError: boom m1"
              in buf.getvalue(), f"exc={exc!r} out={buf.getvalue()!r}")


def case_f_all_candidates_fail() -> None:
    """Every candidate failed: raises, and NAMES every one of them.

    Naming all failures before raising is the point -- an operator fixing a
    broken fan-out needs to know which models failed, not only the first.
    """
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = two_block_cfg(block("c-merge", temperature=0.2))
        fake = FakeChat(fail_models=("m1", "m2"))
        exc: Exception | None = None
        buf = io.StringIO()
        with patched_chat(fake), contextlib.redirect_stdout(buf):
            try:
                consensus.chat(proj, cfg, "translator", TASK_PROMPT)
            except Exception as caught:  # noqa: BLE001 - the caller asserts on it
                exc = caught
        check("f1 all failed: raises client.LLMError",
              isinstance(exc, client.LLMError), f"exc={exc!r}")
        check("f2 all failed: BOTH candidates are named before the raise",
              "[FAIL] consensus: translator candidate 1/2 (m1) failed: "
              "LLMError: boom m1" in buf.getvalue()
              and "[FAIL] consensus: translator candidate 2/2 (m2) failed: "
              "LLMError: boom m2" in buf.getvalue(),
              f"out={buf.getvalue()!r}")


def case_g_consensus_call_fails() -> None:
    """The merge itself fails: fatal, NOT a degrade.

    This was the worst failure in the pipeline. The chapter came out looking
    translated, `chapters.json` said `translated`, and the multi-model merge
    everyone configured had simply never run -- behind one `[warn]` line and a
    tier-1 `degraded` event nobody reads during a batch.
    """
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = two_block_cfg(block("c-merge", temperature=0.2))
        fake = FakeChat(fail_consensus=True)
        buf = io.StringIO()
        exc: Exception | None = None
        with patched_chat(fake), contextlib.redirect_stdout(buf):
            try:
                consensus.chat(proj, cfg, "translator", TASK_PROMPT)
            except Exception as caught:  # noqa: BLE001 - the caller asserts on it
                exc = caught
        check("g1 consensus call failed: raises instead of degrading",
              exc is not None and isinstance(exc, client.LLMError),
              f"exc={exc!r}")
        check("g2 consensus call failed: no 'without merging' fallback remains",
              "without merging" not in buf.getvalue()
              and len(fake.consensus()) == 1,
              f"out={buf.getvalue()!r}")
        check("g3 consensus call failed: no `degraded` event is emitted",
              not _events_named(proj, "degraded"),
              f"events={_events_named(proj, 'degraded')}")


def case_h_explicit_consensus_array() -> None:
    """A load_config'd project with an explicitly authored 2-block
    consensus array: the exact contractual ValueError at load time."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "config.json").write_text(json.dumps({
            "providers": {
                "translator": {"base_url": "http://x/v1", "model": "m1"},
                "consensus": [{"model": "c1"}, {"model": "c2"}],
            },
        }), encoding="utf-8")
        exc: Exception | None = None
        try:
            config.load_config(proj)
        except Exception as caught:  # noqa: BLE001 - the caller asserts on it
            exc = caught
        check("h1 explicit 2-block consensus: load_config raises the exact ValueError",
              isinstance(exc, ValueError)
              and str(exc) == "providers.consensus must list exactly one model (got 2)",
              f"exc={exc!r}")


def case_i_unresolved_model_label() -> None:
    """A block with model None and an unresolvable endpoint labels its
    candidate "(unresolved model)" instead of failing the task. The label
    path is isolated by swapping client.resolve_model with a raiser (no
    network, no /models dependency)."""
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), "proj")
        cfg = two_block_cfg(block("c-merge", temperature=0.2),
                            b1=block(None, base_url="http://127.0.0.1:9/v1"))

        def boom_resolve(base_url, headers=None):
            raise RuntimeError("no models here")

        fake = FakeChat()
        with patched_resolve_model(boom_resolve), patched_chat(fake):
            text = consensus.chat(proj, cfg, "translator", TASK_PROMPT)
        con = fake.consensus()
        check("i1 unresolved model: candidate labeled (unresolved model), task still merges",
              len(con) == 1
              and "### Candidate 1 (model: (unresolved model))" in con[0]["prompt"]
              and "### Candidate 2 (model: m2)" in con[0]["prompt"]
              and text == "merged-final",
              f"prompt={con[0]['prompt'][:300] if con else None!r}")


def case_local_template_wins() -> None:
    """A project-local templates/consensus.md beats the skill-assets copy:
    the consensus prompt is filled from the project's template."""
    reset_globals()
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(
            Path(td), "proj",
            local_consensus="LOCAL arbitrator.\n{{task_prompt}}\n***\n"
                            "{{candidates_section}}\n")
        cfg = two_block_cfg(block("c-merge", temperature=0.2))
        fake = FakeChat()
        with patched_chat(fake):
            consensus.chat(proj, cfg, "translator", TASK_PROMPT)
        con = fake.consensus()
        check("local template: project's consensus.md wins over the skill assets",
              len(con) == 1 and con[0]["prompt"].startswith("LOCAL arbitrator.")
              and "You are the consensus arbitrator." not in con[0]["prompt"]
              and TASK_PROMPT in con[0]["prompt"]
              and "### Candidate 1 (model: m1)" in con[0]["prompt"],
              f"prompt={con[0]['prompt'][:200] if con else None!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_a_single_block()
    case_b_two_blocks()
    case_c_meta_keys()
    case_d_second_candidate_fails()
    case_e_first_candidate_fails()
    case_f_all_candidates_fail()
    case_g_consensus_call_fails()
    case_h_explicit_consensus_array()
    case_i_unresolved_model_label()
    case_local_template_wins()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
