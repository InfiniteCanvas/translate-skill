"""Tests for client.resolve_model's auth-keyed model cache and client.chat's
400 handling.

resolve_model() GETs {base}/v1/models and caches the first data[].id in
client._MODEL_CACHE keyed on (normalized base URL, resolved Authorization
header value): two jobs may share a base URL with different API keys and
see different model lists, so one job's resolution must not pin the
other's -- and anonymous (no auth) resolutions cache separately again.
Failures are never cached: a fetch error or a bad payload raises LLMError
and leaves the key absent, so a later successful resolve refetches.

chat()'s 400 branch is narrow: only an error text blaming the guided-JSON
mechanism (response_format / json_schema / "guided json" / x-guided,
case-insensitively) disarms response_format and retries once immediately;
an unrelated 400 keeps response_format (no silent resend), consumes the
retry budget with backoff like 429/5xx, and raises LLMError when the
budget is gone. Two consecutive guided-400s raise on the second -- the pop
disarms the fallback, so there is no loop.

Hermetic: client.requests is swapped for a fake module object whose get()/
post() count calls and answer from a scripted queue, and client.time for a
stand-in whose sleep() is recorded instead of waited (the file's
attribute-swap convention, restore in finally). Real requests is imported
only for its RequestException type.

Self-contained PASS/FAIL script (no pytest). Run from anywhere (needs
requests, declared inline below for standalone uv runs):

    uv run tests/test_client_cache.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31"]
# ///
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import requests

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client  # noqa: E402

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


class FakeResponse:
    def __init__(self, payload, status_code=200, text="fake"):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FetchLog:
    """client.requests stand-in: get() records (url, auth) and answers from
    a scripted queue of payloads (an entry that is an Exception type or
    instance is raised instead)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, str | None]] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers.get("Authorization") if headers else None))
        step = self.script.pop(0)
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("scripted failure")
        if isinstance(step, Exception):
            raise step
        return FakeResponse(step)

    @property
    def n(self) -> int:
        return len(self.calls)


def install(script) -> tuple[FetchLog, object]:
    """Swap client.requests for a FetchLog; returns (log, orig) -- the
    caller restores orig in finally."""
    log = FetchLog(script)
    orig = client.requests
    client.requests = SimpleNamespace(get=log.get, RequestException=requests.RequestException)
    return log, orig


class PostLog:
    """client.requests stand-in for chat(): post() records each JSON body
    and answers from a scripted queue -- a dict is a 200 payload, a
    (status, text) tuple is an error response (chat only reads .text on
    those, never .json())."""

    def __init__(self, script):
        self.script = list(script)
        self.bodies: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        # Snapshot copy: chat() pops response_format from the body dict in
        # place on the guided-JSON fallback, so a reference would rewrite
        # every earlier call's recorded body.
        self.bodies.append(dict(json))
        step = self.script.pop(0)
        if isinstance(step, tuple):
            return FakeResponse(None, status_code=step[0], text=step[1])
        return FakeResponse(step)

    @property
    def n(self) -> int:
        return len(self.bodies)


def install_post(script) -> tuple[PostLog, object]:
    """Swap client.requests for a PostLog; returns (log, orig) -- the caller
    restores orig in finally. chat() with an explicit model never calls
    get(), so the namespace needs only post + RequestException."""
    log = PostLog(script)
    orig = client.requests
    client.requests = SimpleNamespace(post=log.post, RequestException=requests.RequestException)
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


# chat() fixtures: an explicit model skips resolve_model entirely, and a
# json_schema arms response_format in the request body.
PROVIDER = {"base_url": BASE, "model": "model-1"}
SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}
OK_PAYLOAD = {"choices": [{"message": {"content": "  hi  "},
                           "finish_reason": "stop"}],
              "usage": {"total_tokens": 3}}


# ---------------------------------------------------------------------- cases


def case_1_same_auth_one_fetch() -> None:
    """Same base + same auth: two resolves share one fetch; the cache entry
    is keyed on the /v1-normalized URL and the exact Authorization value."""
    client._MODEL_CACHE.clear()
    log, orig = install([models(1)])
    try:
        first = client.resolve_model(BASE, headers=KEY1)
        second = client.resolve_model(BASE, headers=dict(KEY1))
        check("1a same auth: both resolves return the fetched id",
              first == "model-1" and second == "model-1",
              f"first={first!r} second={second!r}")
        check("1b same auth: exactly one HTTP fetch for two resolves",
              log.n == 1, f"calls={log.calls}")
        check("1c same auth: fetch hit the /v1-normalized models URL",
              log.calls == [("http://llm.local:8888/v1/models", "Bearer key-one")],
              f"calls={log.calls}")
        check("1d same auth: cache keyed on (normalized base, auth value)",
              client._MODEL_CACHE.get(
                  ("http://llm.local:8888/v1", "Bearer key-one")) == "model-1",
              f"cache={dict(client._MODEL_CACHE)!r}")
        # A trailing slash on the base normalizes onto the same entry
        again = client.resolve_model(BASE + "/", headers=KEY1)
        check("1e same auth: trailing-slash base shares the cache entry",
              again == "model-1" and log.n == 1, f"n={log.n}")
    finally:
        client.requests = orig


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
              m2 == "model-2" and log.n == 2,
              f"m2={m2!r} calls={log.calls}")
        check("2b different auth: two cache entries for the same base",
              client._MODEL_CACHE.get(("http://llm.local:8888/v1",
                                       "Bearer key-one")) == "model-1"
              and client._MODEL_CACHE.get(("http://llm.local:8888/v1",
                                           "Bearer key-two")) == "model-2",
              f"cache={dict(client._MODEL_CACHE)!r}")
        back = client.resolve_model(BASE, headers=KEY1)
        check("2c different auth: the first key still resolves from cache",
              back == "model-1" and log.n == 2, f"back={back!r} n={log.n}")

        anon = client.resolve_model(BASE)
        check("2d anonymous: no-header resolution fetches separately",
              anon == "model-3" and log.n == 3, f"anon={anon!r} n={log.n}")
        check("2e anonymous: cached under a None auth key",
              client._MODEL_CACHE.get(("http://llm.local:8888/v1", None))
              == "model-3", f"cache={dict(client._MODEL_CACHE)!r}")
        anon2 = client.resolve_model(BASE, headers={})
        check("2f anonymous: an empty headers dict shares the None entry",
              anon2 == "model-3" and log.n == 3, f"n={log.n}")

        other = client.resolve_model("http://other.local:9999/v1", headers=KEY1)
        check("2g different base: same auth is its own cache entry",
              other == "model-4" and log.n == 4
              and client._MODEL_CACHE.get(("http://other.local:9999/v1",
                                           "Bearer key-one")) == "model-4",
              f"other={other!r} n={log.n}")
    finally:
        client.requests = orig


def case_3_failures_not_cached() -> None:
    """A connection error or a bad payload raises LLMError and leaves the
    key uncached, so the next resolve refetches and can succeed."""
    client._MODEL_CACHE.clear()
    log, orig = install([
        requests.RequestException,   # 1st resolve: connection failure
        {"data": "not-a-list"},      # 2nd resolve: unexpected payload
        models(9),                   # 3rd resolve: success
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
              ok == "model-9" and log.n == 3 and len(client._MODEL_CACHE) == 1,
              f"ok={ok!r} n={log.n}")
    finally:
        client.requests = orig


def case_4_guided_400_fallback() -> None:
    """A 400 whose error text names the guided-JSON mechanism (any of the
    four tokens, case-insensitively) disarms response_format and retries
    once immediately: no backoff, one extra llm_request meta line."""
    metas: list[dict] = []
    sleep_log, orig_time = install_sleep()
    log, orig = install_post([
        (400, "Invalid request: response_format is not supported"),
        OK_PAYLOAD,
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
        check("4d guided-400: metas are request, request, response with "
              "guided_json True then False",
              [m["event"] for m in metas]
              == ["llm_request", "llm_request", "llm_response"]
              and metas[0]["guided_json"] is True
              and metas[1]["guided_json"] is False,
              f"metas={[(m['event'], m.get('guided_json')) for m in metas]}")
    finally:
        client.requests = orig
        client.time = orig_time

    # Every mechanism token triggers the fallback, case-insensitively.
    for i, text in enumerate([
        "response_format rejected",
        "json_schema validation failed",
        "Guided JSON not supported here",
        "x-guided decoding error",
    ]):
        log, orig = install_post([(400, text), OK_PAYLOAD])
        try:
            client.chat(PROVIDER, "hello", json_schema=SCHEMA)
            check(f"4{chr(101 + i)} guided-400 token: fallback fires for {text!r}",
                  log.n == 2 and "response_format" not in log.bodies[1],
                  f"n={log.n}")
        finally:
            client.requests = orig


def case_5_unrelated_400_retries_then_raises() -> None:
    """An unrelated 400 (no mechanism mention) is a real failure: it keeps
    response_format (no silent resend), consumes the retry budget with
    backoff like 429/5xx, and raises LLMError when the budget is gone."""
    metas: list[dict] = []
    sleep_log, orig_time = install_sleep()
    log, orig = install_post([(400, "model 'nope' not found")] * 4)
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
              [m["event"] for m in metas] == ["llm_request", "llm_response"]
              and metas[1].get("error") == str(exc),
              f"metas={[(m['event'], m.get('error')) for m in metas]}")
    finally:
        client.requests = orig
        client.time = orig_time


def case_6_guided_400_no_loop() -> None:
    """Two consecutive guided-400s: the pop disarms the fallback, so the
    second takes the generic >= 400 raise -- no third request, no sleep. A
    plain 400 with response_format never armed raises on the first response."""
    sleep_log, orig_time = install_sleep()
    log, orig = install_post([
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
        client.requests = orig
        client.time = orig_time

    log, orig = install_post([(400, "malformed request")])
    try:
        exc = None
        try:
            client.chat(PROVIDER, "hello")  # no json_schema: never armed
        except client.LLMError as caught:
            exc = caught
        check("6d plain-400: generic LLMError on the first response",
              exc is not None and log.n == 1
              and str(exc).startswith("HTTP 400 from"), f"exc={exc!r} n={log.n}")
    finally:
        client.requests = orig


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_same_auth_one_fetch()
    case_2_auth_and_base_dimensions()
    case_3_failures_not_cached()
    case_4_guided_400_fallback()
    case_5_unrelated_400_retries_then_raises()
    case_6_guided_400_no_loop()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
