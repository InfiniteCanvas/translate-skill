# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "openai>=1.60", "anthropic>=1.0", "httpx2>=2.0"]
# ///
"""Tests for the anthropic-protocol backend: the native Messages API wire
shape, the guided-JSON fallback on output_config, the error ladder, the
per-protocol dispatch and the model-cache keying.

The dispatch contract the load-time validation backs up: a provider block's
`protocol` field selects the SDK; absent means "openai"; anything else (a
bogus string, null, a number) is refused with an LLMError naming the block --
the backstop for hand-built cfgs that never pass load_config's normalizer.

What is pinned here:

  * the anthropic wire body: model/messages/max_tokens (the API REQUIRES
    max_tokens -- the cap always resolves), knobs via extra_body
    (temperature/top_p/top_k), NULL-valued knobs omitted, and the
    openai-protocol knobs (thinking -> chat_template_kwargs,
    repetition_penalty) absent;
  * native structured outputs ride output_config.format
    {"type": "json_schema", "schema": ...} (the anthropic SDK's declared
    shape; the raw API's older output_format name is covered by the same
    fallback regex);
  * a 400 whose text names the mechanism disarms output_config and retries
    immediately; an unrelated 400 consumes the ladder; other 4xx raise
    immediately; there is NO numeric fatal table on this protocol;
  * usage normalization (input_tokens/output_tokens -> the OpenAI-shaped
    keys the trace counters read, anthropic extras passed through),
    stop_reason -> finish_reason passthrough, the <think> strip, and the
    anthropic empty-content diagnostic;
  * _MODEL_CACHE keys carry the protocol dimension, and the anthropic client
    is built on the bare-origin base_url verbatim (_v1_url NOT applied);
  * no-auth: an empty key suppresses the Authorization header via the Omit
    sentinel (anthropic 2.x would otherwise send an EMPTY "Authorization:
    Bearer" header).

Hermetic: client._anthropic_client is swapped for a stub whose create()
records the call kwargs (they ARE the wire body here -- the anthropic path
has no declared/extra merge) and answers from a scripted queue. No network,
restore in finally.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_anthropic_backend.py
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx2
import anthropic

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client

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


def status_error(status: int, text: str, body: object | None = None) -> anthropic.APIStatusError:
    """A real anthropic.APIStatusError carrying the scripted status and text."""
    request = httpx2.Request("POST", "http://x.test/v1/messages")
    response = httpx2.Response(status, request=request, text=text)
    return anthropic.APIStatusError(f"Error {status}", response=response, body=body)


def message(text: str = "  hi  ", stop_reason: str = "end_turn",
            usage: object = None, blocks: list | None = None) -> SimpleNamespace:
    """An SDK-shaped 200 Messages response (the fields chat() reads)."""
    if blocks is None:
        blocks = [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=usage if usage is not None else
                           SimpleNamespace(input_tokens=1, output_tokens=2))


class StubAnthropic:
    """client._anthropic_client stand-in: messages.create(**kwargs) records
    the call kwargs and answers from a scripted queue -- a SimpleNamespace
    payload returns as the 200 response, a (status, text) tuple raises a real
    APIStatusError, an Exception instance is raised verbatim. models.list
    records (timeout, headers) and pops {"data": [...]} pages verbatim."""

    def __init__(self) -> None:
        self.script: list = []
        self.calls: list[dict] = []
        self.listings: list[dict] = []
        self.cfgs: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(list=self._list)

    def record_cfg(self, cfg: dict) -> "StubAnthropic":
        self.cfgs.append(dict(cfg))
        return self

    def _create(self, **kwargs):
        self.calls.append(dict(kwargs))
        step = self.script.pop(0)
        if isinstance(step, tuple):
            raise status_error(step[0], step[1])
        if isinstance(step, Exception):
            raise step
        return step

    def _list(self, timeout=None, extra_headers=None):
        self.listings.append({"timeout": timeout, "extra_headers": extra_headers})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(data=step["data"])

    @property
    def n(self) -> int:
        return len(self.calls)


@contextmanager
def install_anthropic(script: list | None = None):
    """Swap client._anthropic_client for a StubAnthropic; yields (stub, orig)."""
    log = StubAnthropic()
    log.script = list(script or [])
    orig = client._anthropic_client
    client._anthropic_client = lambda cfg: log.record_cfg(cfg)
    try:
        yield log, orig
    finally:
        client._anthropic_client = orig


class StubOpenAICapture:
    """Canned-ok openai stub recording effective bodies (dispatch cases)."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    def _create(self, **kwargs):
        body = {k: v for k, v in kwargs.items()
                if k in ("model", "messages", "temperature", "max_tokens", "top_p")}
        body.update(kwargs.get("extra_body") or {})
        self.bodies.append(body)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hi", reasoning_content=None),
                finish_reason="stop")],
            usage=None,
        )

    def _list(self, timeout=None, extra_headers=None):
        return SimpleNamespace(data=[])


@contextmanager
def install_openai_capture():
    """Swap client._openai_client for a canned-ok stub (dispatch cases)."""
    log = StubOpenAICapture()
    orig = client._openai_client
    client._openai_client = lambda cfg: log
    try:
        yield log, orig
    finally:
        client._openai_client = orig


class _SleepLog:
    """client.time stand-in: real monotonic, recorded skipped sleeps."""

    def __init__(self):
        self.calls: list[float] = []

    def monotonic(self) -> float:
        import time
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


@contextmanager
def no_backoff():
    orig = client._BACKOFF
    client._BACKOFF = (0, 0, 0)
    try:
        yield
    finally:
        client._BACKOFF = orig


BASE = "http://x.test"
SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


def block(**kw) -> dict:
    b = {"base_url": BASE, "model": "claude-sonnet-5-5", "thinking": False,
         "protocol": "anthropic"}
    b.update(kw)
    return b


def case_a_dispatch() -> None:
    """The protocol field dispatches; anything but openai/anthropic is
    refused naming the block, and absence means the openai backend."""
    with install_anthropic() as (log, orig_a):
        with install_openai_capture() as (log_o, orig_o):
            exc = None
            try:
                client.chat({**block(), "protocol": "grpc"}, "hi")
            except client.LLMError as caught:
                exc = caught
            check("a1 unknown protocol: LLMError naming the block and the value",
                  exc is not None and "grpc" in str(exc) and BASE in str(exc)
                  and '"openai" or "anthropic"' in str(exc), f"exc={exc!r}")
            check("a2 unknown protocol: neither backend was called",
                  log.n == 0 and log_o.bodies == [], f"n={log.n}")
            exc = None
            try:
                client.chat({**block(), "protocol": None}, "hi")
            except client.LLMError as caught:
                exc = caught
            check("a3 null protocol: refused too (present means valid)",
                  exc is not None and '"openai" or "anthropic"' in str(exc),
                  f"exc={exc!r}")
            openai_block = block()
            del openai_block["protocol"]
            out = client.chat(openai_block, "hi")
            check("a4 absent protocol: the openai backend served the call",
                  out == "hi" and log.n == 0 and len(log_o.bodies) == 1,
                  f"out={out!r} openai_bodies={len(log_o.bodies)}")
            exc = None
            try:
                client.probe({**block(), "protocol": 3})
            except client.LLMError as caught:
                exc = caught
            check("a5 probe dispatches too: non-string protocol refused",
                  exc is not None and '"openai" or "anthropic"' in str(exc),
                  f"exc={exc!r}")


def case_b_wire_shape() -> None:
    """The native Messages body: knobs via extra_body, null knobs omitted,
    openai-protocol knobs absent, base_url verbatim."""
    with install_anthropic([message()]) as (log, orig):
        cfg = block(temperature=0.7, top_p=0.9, top_k=40, thinking=True,
                    repetition_penalty=1.1, extra_body={"bogus": 1})
        out = client.chat(cfg, "hello")
        check("b1 anthropic: returns the joined text content",
              out == "hi", f"out={out!r}")
        call = log.calls[0]
        check("b2 anthropic: model + user message + resolved max_tokens",
              call["model"] == "claude-sonnet-5-5"
              and call["messages"] == [{"role": "user", "content": "hello"}]
              and call["max_tokens"] == client.config.DEFAULT_MAX_TOKENS,
              f"call={ {k: call[k] for k in ('model', 'messages', 'max_tokens')} }")
        check("b3 anthropic: temperature/top_p/top_k ride extra_body",
              call["extra_body"].get("temperature") == 0.7
              and call["extra_body"].get("top_p") == 0.9
              and call["extra_body"].get("top_k") == 40
              and call["extra_body"].get("bogus") == 1,
              f"extra_body={call.get('extra_body')!r}")
        check("b4 anthropic: thinking/repetition_penalty never reach the wire",
              "chat_template_kwargs" not in call["extra_body"]
              and "repetition_penalty" not in call["extra_body"],
              f"extra_body={call.get('extra_body')!r}")
        check("b5 anthropic: no response_format key (that is the openai field)",
              "response_format" not in call["extra_body"]
              and "output_config" not in call,
              f"call keys={sorted(call)}")
        check("b6 anthropic: the client is built on the bare origin (no /v1)",
              log.cfgs[0]["base_url"] == BASE, f"cfgs={log.cfgs}")

    with install_anthropic([message()]) as (log, orig):
        client.chat(block(temperature=None, top_p=None, top_k=None), "hi")
        call = log.calls[0]
        check("b7 anthropic: NULL-valued knobs are omitted, not sent as null",
              "temperature" not in call.get("extra_body", {})
              and "top_p" not in call.get("extra_body", {})
              and "top_k" not in call.get("extra_body", {}),
              f"extra_body={call.get('extra_body')!r}")
        check("b8 anthropic: no knobs at all -> no extra_body kwarg",
              "extra_body" not in call, f"call keys={sorted(call)}")


def case_c_structured_outputs_and_fallback() -> None:
    """Native guided JSON rides output_config.format; a 400 naming the
    mechanism disarms it and retries immediately."""
    metas: list[dict] = []
    with install_anthropic([
        (400, "output_config is not supported by this endpoint"),
        message("ok"),
    ]) as (log, orig):
        out = client.chat(block(), "hello", json_schema=SCHEMA,
                          meta_hook=metas.append)
        check("c1 guided-400: the fallback retry succeeds",
              out == "ok" and log.n == 2, f"out={out!r} n={log.n}")
        check("c2 guided-400: output_config present then absent",
              "output_config" in log.calls[0]
              and log.calls[0]["output_config"]
              == {"format": {"type": "json_schema", "schema": SCHEMA}}
              and "output_config" not in log.calls[1],
              f"armed={('output_config' in log.calls[0])} "
              f"rearmed={('output_config' in log.calls[1])}")
        check("c3 guided-400: metas announce the retry between the two requests",
              [m["event"] for m in metas]
              == ["llm_request", "llm_retry", "llm_request", "llm_response"]
              and metas[0]["guided_json"] is True
              and metas[2]["guided_json"] is False
              and metas[1]["kind"] == "guided_fallback"
              and metas[1]["attempt"] == 1
              and metas[1]["backoff_s"] is None
              and "output_config" in metas[1]["error"],
              f"metas={[(m['event'], m.get('guided_json')) for m in metas]}")

    with install_anthropic([(400, "output_format rejected here"), message("ok")]) as (log, orig):
        client.chat(block(), "hello", json_schema=SCHEMA)
        check("c4 the legacy output_format name also triggers the fallback",
              log.n == 2 and "output_config" not in log.calls[1], f"n={log.n}")


def case_d_error_ladder() -> None:
    """429/5xx retry to success; an unrelated armed 400 consumes the ladder;
    other 4xx raise immediately; a coded error body is NOT fatal here."""
    with install_anthropic([(429, "slow down"), message("ok")]) as (log, orig):
        with no_backoff():
            out = client.chat(block(), "hi")
        check("d1 429: retried, then succeeded",
              out == "ok" and log.n == 2, f"out={out!r} n={log.n}")

    with install_anthropic([(400, "model 'nope' not found")] * 4) as (log, orig):
        exc = None
        with no_backoff():
            try:
                client.chat(block(), "hi", json_schema=SCHEMA)
            except client.LLMError as caught:
                exc = caught
        check("d2 unrelated armed 400: consumes the ladder then LLMError",
              exc is not None and "after 4 attempts" in str(exc) and log.n == 4,
              f"exc={exc!r} n={log.n}")
        check("d3 unrelated armed 400: output_config never silently dropped",
              all("output_config" in c for c in log.calls), f"n={log.n}")

    with install_anthropic([(403, "forbidden")]) as (log, orig):
        exc = None
        try:
            client.chat(block(), "hi")
        except client.LLMError as caught:
            exc = caught
        check("d4 other 4xx: immediate LLMError, single request",
              exc is not None and str(exc).startswith("HTTP 403 from") and log.n == 1,
              f"exc={exc!r} n={log.n}")

    with install_anthropic([(400, '{"error": {"code": "1214"}}')]) as (log, orig):
        exc = None
        try:
            client.chat(block(), "hi")
        except client.LLMError as caught:
            exc = caught
        check("d5 a Z.AI-shaped coded body is NOT fatal on this protocol",
              isinstance(exc, client.LLMError)
              and not isinstance(exc, client.LLMFatal) and log.n == 1,
              f"exc={exc!r}")


def case_e_response_shape_and_usage() -> None:
    """Usage normalization, stop_reason passthrough, <think> strip, the
    anthropic empty-content diagnostic, and the meta url."""
    metas: list[dict] = []
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 3}
    with install_anthropic([message("<think>hmm</think>\nanswer",
                                    stop_reason="end_turn", usage=usage)]) as (log, orig):
        out = client.chat(block(), "hi", meta_hook=metas.append)
        check("e1 <think> block stripped, content trimmed",
              out == "answer", f"out={out!r}")
        check("e2 stop_reason passes through as finish_reason",
              metas[-1]["finish_reason"] == "end_turn", f"meta={metas[-1]!r}")
        check("e3 usage normalized to the OpenAI shape, anthropic extras kept",
              metas[-1]["usage"] == {"cache_creation_input_tokens": 3,
                                     "prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 15},
              f"usage={metas[-1]['usage']!r}")
        check("e4 the request meta names the native endpoint",
              metas[0]["url"] == "http://x.test/v1/messages"
              and metas[-1]["url"] == "http://x.test/v1/messages",
              f"url={metas[0]['url']!r}")
        check("e5 the anthropic params carry max_tokens",
              metas[0]["params"].get("max_tokens") == client.config.DEFAULT_MAX_TOKENS,
              f"params={metas[0]['params']!r}")

    with install_anthropic([message("", stop_reason="max_tokens", usage={
            "input_tokens": 1234, "output_tokens": 5678}, blocks=[
            SimpleNamespace(type="thinking"),
            SimpleNamespace(type="text", text="")])]) as (log, orig):
        metas: list[dict] = []
        exc = None
        try:
            client.chat(block(), "hi", meta_hook=metas.append)
        except client.LLMError as caught:
            exc = caught
        check("e6 empty text blocks: the thinking-budget diagnostic",
              exc is not None
              and str(exc).startswith("empty completion content from")
              and "budget went to thinking" in str(exc)
              and "budget_tokens" in str(exc), f"exc={exc!r}")
        check("e7 empty path: the terminal meta's usage is normalized too",
              len(metas) == 2
              and metas[-1]["event"] == "llm_response"
              and metas[-1]["usage"] == {"prompt_tokens": 1234,
                                         "completion_tokens": 5678,
                                         "total_tokens": 6912}
              and metas[-1]["finish_reason"] == "max_tokens",
              f"metas={[(m['event'], m.get('usage')) for m in metas]}")

    conn = anthropic.APIConnectionError(request=httpx2.Request(
        "POST", "http://x.test/v1/messages"))
    with install_anthropic([conn] * 4) as (log, orig):
        sleep_log = _SleepLog()
        orig_time = client.time
        client.time = sleep_log
        try:
            exc = None
            try:
                client.chat(block(), "hi")
            except client.LLMError as caught:
                exc = caught
        finally:
            client.time = orig_time
        check("e8 connection ladder: 4 attempts with the 2/4/8 backoff",
              exc is not None and "failed after 4 attempts" in str(exc)
              and log.n == 4 and sleep_log.calls == [2, 4, 8],
              f"exc={exc!r} n={log.n} sleeps={sleep_log.calls}")


def case_f_cache_and_no_auth() -> None:
    """The cache is keyed per protocol; an anthropic resolve builds its
    client on the bare origin; an empty key suppresses the Authorization
    header via the Omit sentinel."""
    client._MODEL_CACHE.clear()
    with install_anthropic([{"data": [{"id": "claude-1"}]}]) as (log_a, orig_a):
        with install_openai_capture() as (log_o, orig_o):
            got = client.resolve_model(BASE, protocol="anthropic")
            check("f1 anthropic resolve: returns the listed id",
                  got == "claude-1" and len(log_a.cfgs) == 1, f"got={got!r}")
            check("f2 anthropic resolve: cache key carries the protocol",
                  client._MODEL_CACHE.get((BASE, None, "anthropic")) == "claude-1"
                  and client._MODEL_CACHE.get((BASE, None, "openai")) is None,
                  f"cache={dict(client._MODEL_CACHE)!r}")
            check("f3 anthropic resolve: the client was built on the bare origin",
                  log_a.cfgs[0]["base_url"] == BASE, f"cfgs={log_a.cfgs}")
            check("f4 anthropic resolve: no-auth listing suppresses the header",
                  log_a.listings[-1]["extra_headers"] is not None
                  and "Authorization" in log_a.listings[-1]["extra_headers"],
                  f"listings={log_a.listings}")
            got2 = client.resolve_model(BASE, protocol="anthropic")
            check("f5 the cached anthropic entry resolves without a refetch",
                  got2 == "claude-1" and len(log_a.cfgs) == 1, f"got2={got2!r}")
    client._MODEL_CACHE.clear()

    with install_anthropic([message()]) as (log, orig):
        client.chat(block(), "hi")
        call = log.calls[0]
        check("f6 no-auth chat: the Authorization header is omitted, not empty",
              call.get("extra_headers") is not None
              and "Authorization" in call["extra_headers"],
              f"extra_headers={call.get('extra_headers')!r}")
    client._MODEL_CACHE.clear()


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_a_dispatch()
    case_b_wire_shape()
    case_c_structured_outputs_and_fallback()
    case_d_error_ladder()
    case_e_response_shape_and_usage()
    case_f_cache_and_no_auth()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
