# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.60", "anthropic>=1.0", "httpx2>=2.0"]
# ///
"""Tests for the rate-limit-aware retry policy in lib/client.py.

Pinned behaviour:

  * `Retry-After` (seconds or HTTP-date) and `X-RateLimit-Reset` (epoch
    milliseconds, epoch seconds, ISO 8601) are parsed into a wait hint;
  * a retry waits at least the provider's hint, and the llm_retry meta +
    backoff_s carry the actual wait, so the ladder is visible;
  * a hint longer than `max_wait_seconds` aborts IMMEDIATELY with
    LLMExhausted (carrying retry_after_s) instead of sleeping -- the
    orchestrator can resume at the reset time;
  * exhausted 429/5xx/connection ladders raise LLMExhausted (resumable,
    exit 4), while a content-shaped retryable 400 still ends as plain
    LLMError (exit 3 if it escapes);
  * config.retry_policy() applies layer-0 defaults, merges partial overrides
    key-wise, and rejects malformed values at load time.

Self-contained PASS/FAIL script (no pytest). Run via uv:

    uv run tests/test_retry_policy.py
"""

from __future__ import annotations

import sys
import time as real_time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, config

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


PROVIDER = {"base_url": "http://llm.test", "model": "m", "protocol": "openai"}
SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}
POLICY = {
    "max_attempts": 3,
    "backoff_seconds": [1],
    "max_wait_seconds": 60,
    "jitter": 0,
    "respect_retry_after": True,
}


def status_error(status: int, text: str,
                 headers: dict | None = None) -> openai.APIStatusError:
    request = httpx2.Request("POST", "http://llm.test/v1/chat/completions")
    response = httpx2.Response(status, request=request, text=text,
                               headers=headers or {})
    return openai.APIStatusError(f"Error {status}", response=response, body=None)


def completion(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, reasoning_content=None),
            finish_reason="stop")],
        usage=None,
    )


class StubOpenAI:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.bodies: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.bodies.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    @property
    def n(self) -> int:
        return len(self.bodies)


class SleepLog:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def monotonic(self) -> float:
        return real_time.monotonic()

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


def run(script: list, policy: dict, *, json_schema: dict | None = None,
        meta_hook=None) -> tuple[str | None, Exception | None,
                                StubOpenAI, SleepLog]:
    stub = StubOpenAI(script)
    log = SleepLog()
    orig_client, orig_time = client._openai_client, client.time
    client._openai_client = lambda cfg: stub
    client.time = log
    out: str | None = None
    exc: Exception | None = None
    try:
        try:
            out = client.chat(PROVIDER, "hi", json_schema=json_schema,
                              meta_hook=meta_hook, retry=policy)
        except Exception as caught:  # noqa: BLE001 - the class is the subject
            exc = caught
    finally:
        client._openai_client = orig_client
        client.time = orig_time
    return out, exc, stub, log


def case_1_hint_parsing() -> None:
    """Every documented hint shape becomes seconds-from-now."""
    now = datetime.now(timezone.utc)
    http_date = format_datetime(now + timedelta(seconds=30), usegmt=True)
    iso = (now + timedelta(seconds=25)).isoformat().replace("+00:00", "Z")
    cases = [
        ("retry-after seconds", {"retry-after": "17"}, 17),
        ("retry-after HTTP-date", {"retry-after": http_date}, 30),
        ("x-ratelimit-reset epoch ms",
         {"x-ratelimit-reset": str(int((now.timestamp() + 20) * 1000))}, 20),
        ("x-ratelimit-reset epoch s",
         {"x-ratelimit-reset": str(int(now.timestamp() + 15))}, 15),
        ("x-ratelimit-reset ISO", {"x-ratelimit-reset": iso}, 25),
    ]
    for name, headers, expected in cases:
        seconds, reset_at = client._retry_after_seconds(
            status_error(429, "slow", headers))
        check(f"1 {name}",
              seconds is not None and abs(seconds - expected) <= 2
              and isinstance(reset_at, str) and reset_at,
              f"seconds={seconds!r} reset_at={reset_at!r}")
    seconds, reset_at = client._retry_after_seconds(status_error(429, "slow"))
    check("1 no hint -> (None, None)", seconds is None and reset_at is None,
          f"seconds={seconds!r} reset_at={reset_at!r}")


def case_2_hint_drives_the_wait() -> None:
    """A 12s Retry-After is waited out and reported on the retry meta."""
    metas: list[dict] = []
    out, exc, stub, sleeps = run(
        [status_error(429, "slow down", {"retry-after": "12"}),
         completion("done")],
        POLICY, meta_hook=metas.append)
    retries = [m for m in metas if m["event"] == "llm_retry"]
    check("2 success after honoring the hint",
          exc is None and out == "done" and stub.n == 2, f"exc={exc!r}")
    check("2 the wait is the provider's hint", sleeps.calls == [12],
          f"sleeps={sleeps.calls}")
    check("2 llm_retry carries retry_after_s and the actual backoff",
          len(retries) == 1 and retries[0].get("retry_after_s") == 12
          and retries[0].get("backoff_s") == 12,
          f"retries={retries}")


def case_3_hint_beyond_the_cap() -> None:
    """A reset further out than max_wait_seconds aborts without sleeping."""
    out, exc, stub, sleeps = run(
        [status_error(429, "daily cap", {"retry-after": "999"})],
        {**POLICY, "max_wait_seconds": 60})
    check("3 immediate LLMExhausted, one request",
          type(exc) is client.LLMExhausted and stub.n == 1, f"exc={exc!r}")
    check("3 nothing slept", sleeps.calls == [], f"sleeps={sleeps.calls}")
    check("3 the hint survives on the exception and in the message",
          getattr(exc, "retry_after_s", None) == 999
          and "retry_after_s=999" in str(exc), f"exc={exc!r}")


def case_4_exhaustion_classes() -> None:
    """Availability failures are resumable; content failures are not."""
    out, exc, stub, _ = run([status_error(503, "down")] * POLICY["max_attempts"],
                            POLICY)
    check("4 5xx exhaustion -> LLMExhausted",
          type(exc) is client.LLMExhausted and stub.n == POLICY["max_attempts"],
          f"exc={exc!r}")
    check("4 a bare 5xx has no hint", getattr(exc, "retry_after_s", None) is None,
          f"exc={exc!r}")

    conn = openai.APIConnectionError(
        request=httpx2.Request("POST", "http://llm.test/v1/chat/completions"))
    out, exc, stub, _ = run([conn] * POLICY["max_attempts"], POLICY)
    check("4 connection exhaustion -> LLMExhausted",
          type(exc) is client.LLMExhausted, f"exc={exc!r}")

    out, exc, stub, _ = run(
        [status_error(400, "model rejected the request")] * POLICY["max_attempts"],
        POLICY, json_schema=SCHEMA)
    check("4 retryable-400 exhaustion stays a plain LLMError",
          type(exc) is client.LLMError, f"exc={exc!r}")


def case_5_backoff_mechanics() -> None:
    """The schedule repeats its last entry; a hint overrides jitter."""
    w = client._wait_seconds({**POLICY, "backoff_seconds": [5, 30]}, 2, None)
    check("5 second failure uses the second entry", w == 30, f"w={w}")
    w = client._wait_seconds({**POLICY, "backoff_seconds": [5, 30]}, 9, None)
    check("5 past the end repeats the last entry", w == 30, f"w={w}")
    w = client._wait_seconds({**POLICY, "backoff_seconds": [5], "jitter": 1},
                             1, 42)
    check("5 a provider hint is never jittered", w == 42, f"w={w}")


def case_6_config_policy() -> None:
    """Defaults, partial overrides, validation, and resolve-time materialization."""
    p = config.retry_policy({})
    check("6 layer-0 defaults",
          p["max_attempts"] == 4 and p["backoff_seconds"] == [5, 30, 120]
          and p["jitter"] == 0.25 and p["respect_retry_after"] is True,
          f"p={p!r}")
    p = config.retry_policy({"retry": {"max_attempts": 2, "jitter": 0}})
    check("6 partial override keeps the other defaults",
          p["max_attempts"] == 2 and p["jitter"] == 0
          and p["backoff_seconds"] == [5, 30, 120], f"p={p!r}")
    for bad in ({"max_attempts": 0}, {"max_attempts": 2.5},
                {"backoff_seconds": []}, {"backoff_seconds": [1, -1]},
                {"max_wait_seconds": -1}, {"jitter": 2},
                {"respect_retry_after": "yes"}, "nope"):
        raised = False
        try:
            config.retry_policy({"retry": bad})
        except ValueError:
            raised = True
        check(f"6 malformed retry {bad!r} raises", raised)

    cfg = config.resolve_config(
        {"providers": {"translator": {"base_url": "http://llm.test"}}}, None)
    check("6 resolve_config materializes the resolved policy",
          cfg.get("retry", {}).get("max_attempts") == 4, f"retry={cfg.get('retry')!r}")
    raised = False
    try:
        config.resolve_config({
            "providers": {"translator": {"base_url": "http://llm.test"}},
            "retry": {"max_attempts": -2},
        }, None)
    except ValueError:
        raised = True
    check("6 resolve_config rejects a malformed retry block at load", raised)


def main() -> int:
    case_1_hint_parsing()
    case_2_hint_drives_the_wait()
    case_3_hint_beyond_the_cap()
    case_4_exhaustion_classes()
    case_5_backoff_mechanics()
    case_6_config_policy()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())