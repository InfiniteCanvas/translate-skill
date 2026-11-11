# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "openai>=1.60", "anthropic>=1.0", "httpx2>=2.0"]
# ///
"""Tests for the trace-log meta contract the dashboards consume.

What is pinned here (the clauses the P1 static lenses found unasserted):

  * call_id pairing: on EVERY terminal path, the metas a chat emits carry
    ONE call_id from the first line to the last -- llm_request/llm_response
    pair up as two JSONL lines per call;
  * exactly one llm_response per terminal path: plain success, retry
    success, guided fallback, fatal code, exhausted ladder, malformed
    payload -- no path emits two, none emits zero;
  * every meta JSON-serializes (logger._append runs json.dumps; a
    non-serializable field is swallowed silently and the whole line is
    lost -- that is the defect class the pydantic usage object would have
    reintroduced);
  * the usage dict carries exactly the OpenAI-shaped keys logger._count
    reads, and _count actually counts them, for BOTH protocols.

Hermetic: client._openai_client / client._anthropic_client are swapped for
stubs answering from scripted queues of real SDK-shaped results and real
SDK exception instances. No network, restore in finally.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_meta_contract.py
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai
import anthropic

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, logger

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


def openai_status(status: int, text: str, body: object | None = None) -> openai.APIStatusError:
    request = httpx2.Request("POST", "http://meta.test/v1/chat/completions")
    return openai.APIStatusError(f"Error {status}",
                                 response=httpx2.Response(status, request=request, text=text),
                                 body=body)


def anthropic_status(status: int, text: str) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "http://meta.test/v1/messages")
    return anthropic.APIStatusError(f"Error {status}",
                                    response=httpx2.Response(status, request=request, text=text),
                                    body=None)


OPENAI_BLOCK = {"base_url": "http://meta.test", "model": "glm-5.3", "thinking": False}
SCHEMA = {"type": "object"}


def _wire_body(kwargs: dict) -> dict:
    body = {k: v for k, v in kwargs.items()
            if k in ("model", "messages", "temperature", "max_tokens", "top_p")}
    body.update(kwargs.get("extra_body") or {})
    return body


class StubOpenAI:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    def _create(self, **kwargs):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            raise openai_status(step[0], step[1])
        return step

    def _list(self, timeout=None, extra_headers=None):
        raise NotImplementedError


class StubAnthropic:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.messages = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(list=self._list)

    def _create(self, **kwargs):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            raise anthropic_status(step[0], step[1])
        return step

    def _list(self, timeout=None, extra_headers=None):
        raise NotImplementedError


@contextmanager
def scripted_openai(script: list):
    log = StubOpenAI(script)
    orig = client._openai_client
    client._openai_client = lambda cfg: log
    try:
        yield
    finally:
        client._openai_client = orig


@contextmanager
def scripted_anthropic(script: list):
    log = StubAnthropic(script)
    orig = client._anthropic_client
    client._anthropic_client = lambda cfg: log
    try:
        yield
    finally:
        client._anthropic_client = orig


@contextmanager
def no_backoff():
    orig = client._DEFAULT_RETRY
    client._DEFAULT_RETRY = {**orig, "backoff_seconds": [0, 0, 0], "jitter": 0}
    try:
        yield
    finally:
        client._DEFAULT_RETRY = orig


def openai_ok(content="ok", usage: dict | None = None):
    """usage models the SDK's pydantic object: it answers model_dump() (the
    flattening _usage_dict performs), not a plain dict."""
    real_usage = usage if usage is not None else \
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, reasoning_content=None),
            finish_reason="stop")],
        usage=SimpleNamespace(model_dump=lambda **_: dict(real_usage)),
    )


def anthropic_ok(text="ok", usage: dict | None = None, blocks: list | None = None):
    real_usage = usage if usage is not None else \
        {"input_tokens": 10, "output_tokens": 5}
    return SimpleNamespace(
        content=blocks if blocks is not None
        else [SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(model_dump=lambda **_: dict(real_usage)),
    )


ANTHROPIC_BLOCK = {"base_url": "http://meta.test", "model": "claude-sonnet-5-5",
                   "thinking": False, "protocol": "anthropic"}


def _paths():
    return [
        ("plain success", OPENAI_BLOCK, [openai_ok()], None),
        ("retry success", OPENAI_BLOCK,
         [(429, "slow down"), openai_ok()], None),
        ("guided fallback", OPENAI_BLOCK,
         [(400, "response_format is not supported"), openai_ok()], None),
        ("fatal code", OPENAI_BLOCK,
         [openai_status(400, '{"error": {"code": "1214"}}',
                        body={"code": "1214"})],
         client.LLMFatal),
        ("exhausted ladder", OPENAI_BLOCK,
         [(503, "down")] * 4, client.LLMExhausted),
        ("malformed payload", OPENAI_BLOCK,
         [SimpleNamespace(id="x")], client.LLMError),
        ("anthropic success", ANTHROPIC_BLOCK, [anthropic_ok()], None),
        ("anthropic retry success", ANTHROPIC_BLOCK,
         [(429, "slow down"), anthropic_ok()], None),
        ("anthropic guided fallback", ANTHROPIC_BLOCK,
         [(400, "output_config is not supported here"), anthropic_ok()], None),
        ("anthropic other-4xx", ANTHROPIC_BLOCK,
         [(403, "forbidden")], client.LLMError),
        ("anthropic exhausted ladder", ANTHROPIC_BLOCK,
         [(503, "down")] * 4, client.LLMExhausted),
        ("anthropic empty content", ANTHROPIC_BLOCK,
         [anthropic_ok(text="", blocks=[SimpleNamespace(type="thinking"),
                                        SimpleNamespace(type="text", text="")])],
         client.LLMError),
        ("anthropic connection ladder", ANTHROPIC_BLOCK,
         [anthropic.APIConnectionError(request=httpx2.Request(
             "POST", "http://meta.test/v1/messages"))] * 4, client.LLMExhausted),
    ]


def case_pairing_and_one_response_per_path() -> None:
    """Every terminal path pairs its metas under ONE call_id and emits
    exactly one llm_response."""
    for label, block, script, expected in _paths():
        metas: list[dict] = []
        exc = None
        seam = scripted_anthropic if block.get("protocol") == "anthropic" else scripted_openai
        with seam(script):
            with no_backoff():
                try:
                    client.chat(block, "hi", json_schema=SCHEMA, meta_hook=metas.append)
                except Exception as caught:
                    exc = caught
        events = [m["event"] for m in metas]
        responses = [m for m in metas if m["event"] == "llm_response"]
        ids = {m.get("call_id") for m in metas}
        check(f"{label}: the metas pair under ONE call_id",
              len(ids) == 1 and None not in ids, f"ids={ids}")
        check(f"{label}: exactly one llm_response on the terminal path",
              len(responses) == 1, f"events={events}")
        check(f"{label}: the terminal outcome is the expected type",
              (type(exc) is expected) if expected
              else (exc is None and events[-1] == "llm_response"
                    and responses[0].get("error") is None),
              f"exc={exc!r} events={events}")
        check(f"{label}: the metas JSON-serialize",
              all(isinstance(json.dumps(m), str) for m in metas),
              f"events={events}")


def case_retry_metas_announce_the_ladder() -> None:
    """Every in-call retry announces itself through meta_hook: one
    llm_retry per retry, BEFORE the retry goes out, carrying the failed
    wire attempt's ordinal (the guided fallback's extra send counts), a
    kind, a one-line error, and the wait -- absent only for the immediate
    guided fallback. (call_id pairing across these metas is already pinned
    by case_pairing_and_one_response_per_path.)"""
    retry_paths = [
        ("retry success", OPENAI_BLOCK, [(429, "slow down"), openai_ok()],
         scripted_openai, [("status", 1)], False),
        ("guided fallback", OPENAI_BLOCK,
         [(400, "response_format is not supported"), openai_ok()],
         scripted_openai, [("guided_fallback", 1)], True),
        ("exhausted ladder", OPENAI_BLOCK, [(503, "down")] * 4,
         scripted_openai, [("status", 1), ("status", 2), ("status", 3)], False),
        ("anthropic retry success", ANTHROPIC_BLOCK,
         [(429, "slow down"), anthropic_ok()], scripted_anthropic,
         [("status", 1)], False),
        ("anthropic guided fallback", ANTHROPIC_BLOCK,
         [(400, "output_config is not supported here"), anthropic_ok()],
         scripted_anthropic, [("guided_fallback", 1)], True),
        ("anthropic connection ladder", ANTHROPIC_BLOCK,
         [anthropic.APIConnectionError(request=httpx2.Request(
             "POST", "http://meta.test/v1/messages"))] * 4, scripted_anthropic,
         [("connection", 1), ("connection", 2), ("connection", 3)], False),
    ]
    for label, block, script, seam, expected, immediate in retry_paths:
        metas: list[dict] = []
        with seam(script):
            with no_backoff():
                try:
                    client.chat(block, "hi", json_schema=SCHEMA,
                                meta_hook=metas.append)
                except Exception:
                    pass
        retries = [m for m in metas if m["event"] == "llm_retry"]
        check(f"{label}: one llm_retry per retry with kind and failed attempt",
              [(m.get("kind"), m.get("attempt")) for m in retries] == expected,
              f"retries={[(m.get('kind'), m.get('attempt')) for m in retries]}")
        check(f"{label}: the wait is {'absent' if immediate else 'present'} "
              "on every retry meta",
              all((m.get("backoff_s") is None) == immediate for m in retries),
              f"backoffs={[m.get('backoff_s') for m in retries]}")
        check(f"{label}: retry errors are one line",
              all(isinstance(m.get("error"), str)
                  and "\n" not in m["error"] for m in retries),
              f"errors={[m.get('error') for m in retries]}")


def case_metas_serialize_and_usage_counts() -> None:
    """Every meta JSON-serializes and the usage dict carries exactly the keys
    logger._count reads -- for BOTH protocols. logger._append runs
    json.dumps inside a swallow-all; a non-serializable field (the raw
    pydantic usage object) would silently drop the whole line."""
    for label, block, script, context in [
        ("openai", OPENAI_BLOCK, [openai_ok()], scripted_openai),
        ("anthropic", ANTHROPIC_BLOCK, [anthropic_ok()], scripted_anthropic),
    ]:
        metas: list[dict] = []
        with context(script):
            client.chat(block, "hi", meta_hook=metas.append)
        response_meta = metas[-1]
        serializable = None
        try:
            serializable = json.dumps(response_meta)
        except TypeError as exc:
            serializable = f"TypeError: {exc}"
        check(f"{label}: the response meta JSON-serializes (the trace line survives)",
              isinstance(serializable, str) and "usage" in serializable,
              f"serializable={serializable!r}")
        usage = response_meta.get("usage")
        check(f"{label}: usage is a dict with the OpenAI-shaped keys _count reads",
              isinstance(usage, dict)
              and isinstance(usage.get("prompt_tokens"), int)
              and isinstance(usage.get("completion_tokens"), int)
              and isinstance(usage.get("total_tokens"), int),
              f"usage={usage!r}")

        run = SimpleNamespace(counters={})
        logger._count(run, "chapter", {"job": "translator", "usage": usage})
        slot = run.counters.get("chapter", {})
        check(f"{label}: logger._count counts the meta's usage",
              slot.get("calls") == 1
              and slot.get("tokens", {}).get("translator") == usage["total_tokens"],
              f"slot={slot!r}")

    metas: list[dict] = []
    with scripted_anthropic([anthropic_ok()]):
        client.chat(ANTHROPIC_BLOCK, "hi", meta_hook=metas.append)
    usage = metas[-1]["usage"]
    check("anthropic: the meta's usage is normalized (no input_tokens key left)",
          "input_tokens" not in usage and "output_tokens" not in usage
          and usage.get("prompt_tokens") == 10 and usage.get("completion_tokens") == 5
          and usage.get("total_tokens") == 15,
          f"usage={usage!r}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_pairing_and_one_response_per_path()
    case_retry_metas_announce_the_ladder()
    case_metas_serialize_and_usage_counts()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
