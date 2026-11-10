# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.60", "anthropic>=1.0", "httpx2>=2.0"]
# ///
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai

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


def status_error(status: int, text: str, body: object | None = None) -> openai.APIStatusError:
    """A real openai.APIStatusError carrying the scripted status and text --
    the exception instances the SDK raises, not a stubbed shape."""
    request = httpx2.Request("POST", "http://llm.local:8888/v1/chat/completions")
    response = httpx2.Response(status, request=request, text=text)
    return openai.APIStatusError(f"Error {status}", response=response, body=body)


def connection_error() -> openai.APIConnectionError:
    request = httpx2.Request("POST", "http://llm.local:8888/v1/chat/completions")
    return openai.APIConnectionError(request=request)


def completion(content: str | None = "  hi  ", finish_reason: str = "stop",
               usage: object = None, reasoning_content: str | None = None) -> SimpleNamespace:
    """An SDK-shaped 200 completion (the fields chat() reads)."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, reasoning_content=reasoning_content),
            finish_reason=finish_reason)],
        usage=usage,
    )


def _wire_body(kwargs: dict) -> dict:
    """The effective JSON body the SDK serializes: declared params merged
    under extra_body (extra_body values win -- verified SDK merge order)."""
    body = {k: v for k, v in kwargs.items()
            if k in ("model", "messages", "temperature", "max_tokens", "top_p")}
    body.update(kwargs.get("extra_body") or {})
    return body


class StubOpenAI:
    """client._openai_client stand-in: chat.completions.create(**kwargs)
    records the effective wire body and answers from a scripted queue -- a
    SimpleNamespace/dict payload returns as the 200 completion, a (status,
    text) tuple raises a real APIStatusError, an Exception instance is
    raised verbatim. models.list records (timeout, headers) and pops page
    payloads the same way (a page is {"data": [...]}, items pass through
    verbatim)."""

    def __init__(self) -> None:
        self.script: list = []
        self.bodies: list[dict] = []
        self.listings: list[dict] = []
        self.cfgs: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    def record_cfg(self, cfg: dict) -> "StubOpenAI":
        self.cfgs.append(dict(cfg))
        return self

    def _create(self, **kwargs):
        self.bodies.append(_wire_body(kwargs))
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
        return len(self.bodies)


def install(script) -> tuple[StubOpenAI, object]:
    """Swap client._openai_client for a StubOpenAI with the scripted queue;
    returns (stub, orig) -- the caller restores orig in finally. The SAME
    stub answers every factory call, so chat() and resolve_model() share
    one log."""
    log = StubOpenAI()
    log.script = list(script)
    orig = client._openai_client
    client._openai_client = lambda cfg: log.record_cfg(cfg)
    return log, orig


class SleepLog:
    """client.time stand-in: real monotonic, sleep() calls recorded (and
    skipped) so backoff paths stay instant and observable."""

    def __init__(self):
        self.calls: list[float] = []

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


def install_sleep() -> tuple[SleepLog, object]:
    """Swap client.time for a SleepLog; returns (log, orig)."""
    log = SleepLog()
    orig = client.time
    client.time = log
    return log, orig


BASE = "http://llm.local:8888"
KEY1 = {"Authorization": "Bearer key-one"}
KEY2 = {"Authorization": "Bearer key-two"}


def models(n: int) -> dict:
    return {"data": [{"id": f"model-{n}"}]}


PROVIDER = {"base_url": BASE, "model": "model-1"}
SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}
V1 = "http://llm.local:8888/v1"


def case_1_same_auth_one_fetch() -> None:
    """Same base + same auth: two resolves share one fetch; the cache entry
    is keyed on the /v1-normalized URL, the exact Authorization value and
    the protocol."""
    client._MODEL_CACHE.clear()
    log, orig = install([models(1)])
    try:
        first = client.resolve_model(BASE, headers=KEY1)
        second = client.resolve_model(BASE, headers=dict(KEY1))
        check("1a same auth: both resolves return the fetched id",
              first == "model-1" and second == "model-1",
              f"first={first!r} second={second!r}")
        check("1b same auth: exactly one models.list for two resolves",
              len(log.listings) == 1, f"listings={log.listings}")
        check("1c same auth: the client was built on the /v1-normalized base",
              log.cfgs[0]["base_url"] == V1,
              f"cfgs={log.cfgs}")
        check("1d same auth: cache keyed on (normalized base, auth value, protocol)",
              client._MODEL_CACHE.get(
                  (V1, "Bearer key-one", "openai")) == "model-1",
              f"cache={dict(client._MODEL_CACHE)!r}")
        again = client.resolve_model(BASE + "/", headers=KEY1)
        check("1e same auth: trailing-slash base shares the cache entry",
              again == "model-1" and len(log.listings) == 1, f"n={len(log.listings)}")
    finally:
        client._openai_client = orig


def case_2_auth_and_base_dimensions() -> None:
    """Different auth on the same base refetches (each key keeps its own
    model), the anonymous no-header resolution caches separately from both,
    and a different base with the same auth is its own entry too."""
    client._MODEL_CACHE.clear()
    log, orig = install([models(1), models(2), models(3), models(4)])
    try:
        client.resolve_model(BASE, headers=KEY1)
        m2 = client.resolve_model(BASE, headers=KEY2)
        check("2a different auth: refetches and returns its own model",
              m2 == "model-2" and len(log.listings) == 2,
              f"m2={m2!r} listings={len(log.listings)}")
        check("2b different auth: two cache entries for the same base",
              client._MODEL_CACHE.get((V1, "Bearer key-one", "openai")) == "model-1"
              and client._MODEL_CACHE.get((V1, "Bearer key-two", "openai")) == "model-2",
              f"cache={dict(client._MODEL_CACHE)!r}")
        back = client.resolve_model(BASE, headers=KEY1)
        check("2c different auth: the first key still resolves from cache",
              back == "model-1" and len(log.listings) == 2, f"back={back!r} n={len(log.listings)}")

        anon = client.resolve_model(BASE)
        check("2d anonymous: no-header resolution fetches separately",
              anon == "model-3" and len(log.listings) == 3, f"anon={anon!r} n={len(log.listings)}")
        check("2e anonymous: cached under a None auth key",
              client._MODEL_CACHE.get((V1, None, "openai"))
              == "model-3", f"cache={dict(client._MODEL_CACHE)!r}")
        anon2 = client.resolve_model(BASE, headers={})
        check("2f anonymous: an empty headers dict shares the None entry",
              anon2 == "model-3" and len(log.listings) == 3, f"n={len(log.listings)}")

        other = client.resolve_model("http://other.local:9999/v1", headers=KEY1)
        check("2g different base: same auth is its own cache entry",
              other == "model-4" and len(log.listings) == 4
              and client._MODEL_CACHE.get(("http://other.local:9999/v1",
                                           "Bearer key-one", "openai")) == "model-4",
              f"other={other!r} n={len(log.listings)}")
    finally:
        client._openai_client = orig


def case_3_failures_not_cached() -> None:
    """A connection error or a bad payload raises LLMError and leaves the
    key uncached, so the next resolve refetches and can succeed."""
    client._MODEL_CACHE.clear()
    log, orig = install([
        connection_error(),
        {"data": "not-a-list"},
        models(9),
    ])
    try:
        exc1 = exc2 = None
        try:
            client.resolve_model(BASE, headers=KEY1)
        except client.LLMError as caught:
            exc1 = caught
        try:
            client.resolve_model(BASE, headers=KEY1)
        except client.LLMError as caught:
            exc2 = caught
        check("3a failures: connection error -> LLMError naming the endpoint",
              exc1 is not None and "failed to list models"
              in str(exc1), f"exc={exc1!r}")
        check("3b failures: unexpected payload -> LLMError, no fetch pin",
              exc2 is not None and "unexpected payload" in str(exc2),
              f"exc={exc2!r}")
        check("3c failures: neither failure landed in the cache",
              len(client._MODEL_CACHE) == 0, f"cache={dict(client._MODEL_CACHE)!r}")
        ok = client.resolve_model(BASE, headers=KEY1)
        check("3d failures: the next resolve refetches and succeeds",
              ok == "model-9" and len(log.listings) == 3 and len(client._MODEL_CACHE) == 1,
              f"ok={ok!r} n={len(log.listings)}")
    finally:
        client._openai_client = orig


def case_4_guided_400_fallback() -> None:
    """A 400 whose error text names the guided-JSON mechanism (any of the
    six tokens, case-insensitively) disarms response_format and retries
    once immediately: no backoff, one llm_retry meta + one extra
    llm_request meta line."""
    metas: list[dict] = []
    sleep_log, orig_time = install_sleep()
    log, orig = install([
        (400, "Invalid request: response_format is not supported"),
        completion(),
    ])
    try:
        out = client.chat(PROVIDER, "hello", json_schema=SCHEMA,
                          meta_hook=metas.append)
        check("4a guided-400: fallback retry succeeds",
              out == "hi" and log.n == 2, f"out={out!r} n={log.n}")
        check("4b guided-400: first request armed, retried request disarmed",
              "response_format" in log.bodies[0]
              and "response_format" not in log.bodies[1],
              f"bodies={[sorted(b) for b in log.bodies]}")
        check("4c guided-400: no backoff sleep on the immediate fallback",
              sleep_log.calls == [], f"sleeps={sleep_log.calls}")
        check("4d guided-400: the retry is announced between the two requests",
              [m["event"] for m in metas]
              == ["llm_request", "llm_retry", "llm_request", "llm_response"]
              and metas[0]["guided_json"] is True
              and metas[2]["guided_json"] is False
              and metas[1]["kind"] == "guided_fallback"
              and metas[1]["attempt"] == 1
              and metas[1]["backoff_s"] is None
              and "response_format" in metas[1]["error"],
              f"metas={[(m['event'], m.get('guided_json')) for m in metas]}")
    finally:
        client._openai_client = orig
        client.time = orig_time

    for i, text in enumerate([
        "response_format rejected",
        "json_schema validation failed",
        "Guided JSON not supported here",
        "x-guided decoding error",
        "output_config is not supported here",
    ]):
        log, orig = install([(400, text), completion()])
        try:
            client.chat(PROVIDER, "hello", json_schema=SCHEMA)
            check(f"4{chr(101 + i)} guided-400 token: fallback fires for {text!r}",
                  log.n == 2 and "response_format" not in log.bodies[1],
                  f"n={log.n}")
        finally:
            client._openai_client = orig


def case_5_unrelated_400_retries_then_raises() -> None:
    """An unrelated 400 (no mechanism mention) is a real failure: it keeps
    response_format (no silent resend), consumes the retry budget with
    backoff like 429/5xx, and raises LLMError when the budget is gone."""
    metas: list[dict] = []
    sleep_log, orig_time = install_sleep()
    log, orig = install([(400, "model 'nope' not found")] * 4)
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello", json_schema=SCHEMA,
                        meta_hook=metas.append)
        except client.LLMError as caught:
            exc = caught
        check("5a unrelated-400: LLMError once the budget is exhausted",
              exc is not None and "HTTP 400" in str(exc)
              and "after 4 attempts" in str(exc)
              and "model 'nope' not found" in str(exc), f"exc={exc!r}")
        check("5b unrelated-400: four attempts, every body keeps response_format",
              log.n == 4 and all("response_format" in b for b in log.bodies),
              f"n={log.n} armed={[('response_format' in b) for b in log.bodies]}")
        check("5c unrelated-400: backoff 2/4/8 consumed",
              sleep_log.calls == [2, 4, 8], f"sleeps={sleep_log.calls}")
        check("5d unrelated-400: the response meta carries the error",
              [m["event"] for m in metas]
              == ["llm_request", "llm_retry", "llm_retry", "llm_retry",
                  "llm_response"]
              and metas[-1].get("error") == str(exc)
              and all(m["kind"] == "status" for m in metas[1:4]),
              f"metas={[(m['event'], m.get('error')) for m in metas]}")
    finally:
        client._openai_client = orig
        client.time = orig_time


def case_6_guided_400_no_loop() -> None:
    """Two consecutive guided-400s: the pop disarms the fallback, so the
    second takes the generic >= 400 raise -- no third request, no sleep. A
    plain 400 with response_format never armed raises on the first response."""
    sleep_log, orig_time = install_sleep()
    log, orig = install([
        (400, "response_format unsupported"),
        (400, "response_format unsupported again"),
    ])
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello", json_schema=SCHEMA)
        except client.LLMError as caught:
            exc = caught
        check("6a guided-400 loop: raises on the second guided-400 "
              "(generic message, no attempts suffix)",
              exc is not None and str(exc).startswith("HTTP 400 from")
              and "after" not in str(exc), f"exc={exc!r}")
        check("6b guided-400 loop: exactly two requests, second disarmed",
              log.n == 2 and "response_format" in log.bodies[0]
              and "response_format" not in log.bodies[1],
              f"n={log.n}")
        check("6c guided-400 loop: no backoff sleeps",
              sleep_log.calls == [], f"sleeps={sleep_log.calls}")
    finally:
        client._openai_client = orig
        client.time = orig_time

    log, orig = install([(400, "malformed request")])
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello")
        except client.LLMError as caught:
            exc = caught
        check("6d plain-400: generic LLMError on the first response",
              exc is not None and log.n == 1
              and str(exc).startswith("HTTP 400 from"), f"exc={exc!r} n={log.n}")
    finally:
        client._openai_client = orig


def case_7_absent_content_vs_malformed() -> None:
    """A reasoning model that exhausts its budget returns finish_reason
    "length" with `content` ABSENT (not empty) and reasoning_content as the
    only populated field -- the shape MiniMax-M3.1-Flash-Preview sends. That
    must reach the empty-content diagnostic naming the budget knob, NOT the
    malformed-payload message. A payload with no choices at all is still
    malformed and still says so."""
    absent = completion(content=None, finish_reason="length",
                        usage={"completion_tokens": 65536,
                               "completion_tokens_details": {"reasoning_tokens": 65536}},
                        reasoning_content="still planning...")
    log, orig = install([absent])
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello")
        except client.LLMError as caught:
            exc = caught
        check("7a absent content: reports empty completion, not malformed payload",
              exc is not None and str(exc).startswith("empty completion content from"),
              f"exc={exc!r}")
        check("7b absent content: hint names the reasoning budget knob",
              exc is not None and "reasoning_content present" in str(exc)
              and "reasoning_effort" in str(exc), f"exc={exc!r}")
        check("7c absent content: exactly one request, no retry storm",
              log.n == 1, f"n={log.n}")
    finally:
        client._openai_client = orig

    log, orig = install([SimpleNamespace(id="x", object="chat.completion")])
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello")
        except client.LLMError as caught:
            exc = caught
        check("7d no choices: still reported as an unexpected payload",
              exc is not None and str(exc).startswith("unexpected response payload from"),
              f"exc={exc!r}")
    finally:
        client._openai_client = orig

    log, orig = install([completion(content="", reasoning_content="thinking")])
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello")
        except client.LLMError as caught:
            exc = caught
        check("7e empty-string content: same budget diagnostic",
              exc is not None and str(exc).startswith("empty completion content from")
              and "reasoning_effort" in str(exc), f"exc={exc!r}")
    finally:
        client._openai_client = orig


def case_8_connection_ladder() -> None:
    """A chat()-path connection error (not just resolve_model's) takes the
    full ladder: four attempts, backoff 2/4/8, LLMError at the end."""
    metas: list[dict] = []
    sleep_log, orig_time = install_sleep()
    log, orig = install([connection_error()] * 4)
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello", meta_hook=metas.append)
        except client.LLMError as caught:
            exc = caught
        check("8a connection ladder: LLMError after four attempts",
              exc is not None and "failed after 4 attempts" in str(exc)
              and log.n == 4, f"exc={exc!r} n={log.n}")
        check("8b connection ladder: backoff 2/4/8 consumed",
              sleep_log.calls == [2, 4, 8], f"sleeps={sleep_log.calls}")
        retries = [m for m in metas if m["event"] == "llm_retry"]
        check("8c connection ladder: every retry announces itself before the wait",
              len({m["call_id"] for m in metas}) == 1
              and [m["event"] for m in metas]
              == ["llm_request", "llm_retry", "llm_retry", "llm_retry",
                  "llm_response"]
              and [m["attempt"] for m in retries] == [1, 2, 3]
              and [m["backoff_s"] for m in retries] == [2, 4, 8]
              and all(m["kind"] == "connection" for m in retries),
              f"metas={[(m['event'], m.get('call_id')) for m in metas]}")
    finally:
        client._openai_client = orig
        client.time = orig_time


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_same_auth_one_fetch()
    case_2_auth_and_base_dimensions()
    case_3_failures_not_cached()
    case_4_guided_400_fallback()
    case_5_unrelated_400_retries_then_raises()
    case_6_guided_400_no_loop()
    case_7_absent_content_vs_malformed()
    case_8_connection_ladder()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
