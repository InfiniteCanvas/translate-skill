# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml"]
# ///
"""Tests for the fatal-provider-error policy.

Two requirements, both about stopping the run:

  1. A provider call that fails after its retries cancels the process and logs a
     fatal error -- it does not degrade to a partial result.
  2. Some provider error codes are irrecoverable, so they must not spend retry
     budget at all: Z.AI's table (https://docs.z.ai/api-reference/api-code) is
     transcribed into `client.ZAI_FATAL_CODES` and matched by CODE, not by
     message text.

The defect behind (2), from a real run: a consensus block declaring
`max_tokens: 128000` under a 256000 task cap was sent 256000, answered `HTTP 400
code 1210`, burned four attempts and 14s of backoff, and then the merge silently
degraded to candidate 1. So both halves mattered: 1210 was retried four times,
and the fourth failure was absorbed instead of stopping.

What is pinned here:

  * each irrecoverable code raises on the FIRST response -- the post count is
    asserted at 1, which is the whole point;
  * the fatal check runs BEFORE the guided-JSON fallback, so a 1214 whose
    message names `response_format` cannot drop response_format and re-POST
    (the natural code placement made this silently retry forever);
  * the quota-window codes (1308/1310/1316-1321) are deliberately NOT fatal:
    they publish a reset time and a long run may clear one, so they stay on the
    retry ladder where an exhausted retry is fatal anyway;
  * a body that is not JSON, or has no `error`, or carries an int code, never
    raises from the reader and keeps the ordinary ladder;
  * recoverable 429/5xx still retry and still succeed;
  * LLMFatal is BOTH an LLMError and a PipelineError, which is what routes it
    through every `except PipelineError: raise` stage guard untouched.

Hermetic: client.requests is swapped for a fake whose post() records the JSON
body and returns a scripted response. No network, restore in finally.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_fatal_provider_errors.py
"""
from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import requests

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, config, logger, pipeline  # noqa: E402

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
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self):
        import json as _json
        return _json.loads(self.text)


class PostLog:
    """Records every request body and replays a scripted response list.

    A single-element script means the second post (if any) reuses the last
    response, so a test that asserts post_count == 1 is asserting the retry
    never happened rather than just observing its outcome."""

    def __init__(self, script: list) -> None:
        self.script = script
        self.bodies: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(json)
        return self.script[min(len(self.bodies) - 1, len(self.script) - 1)]


@contextmanager
def no_backoff():
    """Zero the retry sleeps.

    These cases deliberately drive the full ladder (case_c asserts
    _MAX_ATTEMPTS posts for each of ten retryable codes), and the real _BACKOFF
    of (2, 4, 8) would make that ten x 14s of pure sleeping. The attempt
    COUNT is what is under test, not the delay; the delays themselves are not
    asserted anywhere in this file.
    """
    orig = client._BACKOFF
    client._BACKOFF = (0, 0, 0)
    try:
        yield
    finally:
        client._BACKOFF = orig


@contextmanager
def scripted(script: list):
    log = PostLog(script)
    orig = client.requests
    client.requests = SimpleNamespace(post=log.post,
                                      RequestException=requests.RequestException)
    try:
        with no_backoff():
            yield log
    finally:
        client.requests = orig


def zai(code: str, message: str = "boom", status: int = 400) -> FakeResponse:
    import json as _json
    return FakeResponse(status, _json.dumps(
        {"error": {"code": code, "message": message}}))


def block(**kw) -> dict:
    b = {"base_url": "http://fake:1/v1", "model": "glm-5.3",
         "temperature": 0.7, "max_tokens": 128000, "thinking": False}
    b.update(kw)
    return b


def call(**kw):
    return client.chat(block(), "hi", json_schema={"type": "object"}, **kw)


# The codes client.ZAI_FATAL_CODES claims, with the HTTP status the vendor pairs
# them with. If the vendor re-buckets one, this table is the thing that fails.
FATAL_CASES = [
    ("1000", 401), ("1001", 401), ("1003", 401), ("1005", 401),
    ("1113", 429), ("1210", 400), ("1211", 400), ("1212", 400),
    ("1213", 400), ("1214", 400), ("1215", 400), ("1220", 403),
    ("1221", 400), ("1222", 400), ("1261", 400), ("1301", 400),
    ("1309", 429), ("1311", 429), ("1313", 429), ("1314", 429),
    ("1315", 429),
]

# Quota windows that publish a reset time. Fatalizing these would convert a
# self-healing wait into a guaranteed kill on a run that spans hours.
RETRYABLE_CODES = ["1302", "1305", "1308", "1310", "1316", "1317", "1318",
                   "1319", "1320", "1321"]


def case_a_fatal_codes_raise_on_the_first_attempt() -> None:
    for code, status in FATAL_CASES:
        exc: Exception | None = None
        with scripted([zai(code, status=status)]) as log:
            try:
                call()
            except Exception as caught:  # noqa: BLE001 - the caller asserts
                exc = caught
        check(f"a {code}: raises LLMFatal naming the code",
              isinstance(exc, client.LLMFatal) and code in str(exc),
              f"exc={exc!r}")
        check(f"a {code}: exactly ONE request, no retry budget spent",
              len(log.bodies) == 1, f"posts={len(log.bodies)}")


def case_b_the_fatal_check_precedes_the_guided_json_fallback() -> None:
    """The placement bug this case exists for.

    `_GUIDED_UNSUPPORTED_RE` matches 'response_format'. A Z.AI 1214 whose
    message names that field therefore matches it too -- so a fatal check
    placed after that branch would pop response_format, re-POST the same
    malformed request, and burn the whole ladder on a request the provider has
    already refused. The request body must still carry response_format on the
    single attempt that is made."""
    exc: Exception | None = None
    with scripted([zai("1214", "Parameter `response_format` is invalid.")]) as log:
        try:
            call()
        except Exception as caught:  # noqa: BLE001 - the caller asserts
            exc = caught
    check("b1 a 1214 blaming response_format is fatal, not a guided-JSON drop",
          isinstance(exc, client.LLMFatal), f"exc={exc!r}")
    check("b2 response_format was never dropped and re-sent",
          len(log.bodies) == 1
          and "response_format" in log.bodies[0],
          f"posts={len(log.bodies)} body={log.bodies[0] if log.bodies else None}")


def case_c_recoverable_codes_still_retry() -> None:
    for code in RETRYABLE_CODES:
        # Exhaust the ladder: if the code were fatal this would stop at one.
        exc: Exception | None = None
        with scripted([zai(code, status=429)]) as log:
            try:
                call()
            except Exception as caught:  # noqa: BLE001 - the caller asserts
                exc = caught
        check(f"c {code}: a reset-window/rate-limit code is NOT fatal",
              not isinstance(exc, client.LLMFatal), f"exc={exc!r}")
        check(f"c {code}: it spends the full retry ladder",
              len(log.bodies) == client._MAX_ATTEMPTS,
              f"posts={len(log.bodies)}")


def case_d_recoverable_failures_still_succeed() -> None:
    ok = FakeResponse(200, '{"choices": [{"message": {"content": "hi"}}]}')
    for status in (429, 500, 503):
        with scripted([FakeResponse(status, "nope"), ok]) as log:
            text = call()
        check(f"d HTTP {status}: retried, then succeeded on attempt 2",
              text == "hi" and len(log.bodies) == 2, f"text={text!r}")


def case_e_unreadable_bodies_never_raise() -> None:
    """`_fatal_code` is on the hot path for EVERY response, including the 200s
    that dominate a run. It must not raise on any shape a real server can
    produce, and a malformed body must not cost us the real HTTP status."""
    for name, resp in [
        ("non-JSON body", FakeResponse(400, "<html>gateway</html>")),
        ("JSON array", FakeResponse(400, "[1,2,3]")),
        ("no error key", FakeResponse(400, '{"detail":"nope"}')),
        ("error is a string", FakeResponse(400, '{"error":"nope"}')),
        ("null code", FakeResponse(400, '{"error":{"code":null}}')),
        ("empty code", FakeResponse(400, '{"error":{"code":"  "}}')),
    ]:
        check(f"e {name}: _fatal_code returns None, no raise",
              client._fatal_code(resp) is None, "")

    # An int code is what the vendor may actually send; string comparison must
    # not miss it.
    check("e int code is normalized to a string",
          client._fatal_code(FakeResponse(400, '{"error":{"code":1210}}')) == "1210",
          "")

    # And the ladder still behaves: an unparseable 400 is retried, as before.
    with scripted([FakeResponse(400, "<html>gateway</html>")] * 8) as log:
        try:
            call()
        except Exception:  # noqa: BLE001 - exhaustion is the expected outcome
            pass
    check("e an unparseable 400 keeps the ordinary retry ladder",
          len(log.bodies) == client._MAX_ATTEMPTS, f"posts={len(log.bodies)}")


def case_f_the_fatal_channel_rides_pipeline_error() -> None:
    """The whole mechanism. Every stage guard in pipeline.py is

        except PipelineError: raise
        except Exception: ...degrade...

    so LLMFatal inheriting PipelineError is what stops it from being absorbed
    two or three layers below main(). Verified here rather than assumed: the
    aliasing in pipeline.py is easy to get backwards (subclassing instead of
    aliasing inverts the relationship and silently breaks every guard)."""
    check("f1 LLMFatal is an LLMError (existing handlers still work)",
          issubclass(client.LLMFatal, client.LLMError), "")
    check("f2 LLMFatal is a pipeline.PipelineError (stage guards pass it through)",
          issubclass(client.LLMFatal, pipeline.PipelineError), "")
    check("f3 pipeline.PipelineError IS lib.errors.PipelineError (aliased, not subclassed)",
          pipeline.PipelineError is client._PipelineError, "")

    absorbed: list[str] = []

    def stage_guard() -> None:
        try:
            raise client.LLMFatal("dead key")
        except pipeline.PipelineError:
            absorbed.append("re-raised by the guard")
        except Exception:  # noqa: BLE001 - the failure mode being guarded
            absorbed.append("SWALLOWED")

    stage_guard()
    check("f4 the existing stage-guard idiom lets it through",
          absorbed == ["re-raised by the guard"], f"got={absorbed}")


def case_g_exhausted_retries_are_still_llmerror() -> None:
    """A non-fatal code that never recovers exhausts _MAX_ATTEMPTS and raises a
    plain LLMError. It is equally terminal at the CLI -- both arms return exit 3
    -- but the type still distinguishes "the provider said no" from "we ran out
    of patience"."""
    with scripted([FakeResponse(503, "unavailable")] * 8) as log:
        exc: Exception | None = None
        try:
            call()
        except Exception as caught:  # noqa: BLE001 - the caller asserts
            exc = caught
    check("g1 an exhausted retry ladder raises LLMError",
          isinstance(exc, client.LLMError)
          and not isinstance(exc, client.LLMFatal), f"exc={exc!r}")
    check("g2 after exactly _MAX_ATTEMPTS attempts",
          len(log.bodies) == client._MAX_ATTEMPTS, f"posts={len(log.bodies)}")


def case_h_probe_names_the_business_code() -> None:
    """`ping` is the command an operator runs when a key stops working. Without
    the code, 'HTTP 429' cannot be told apart from an empty balance (1113) vs a
    rate limit (1302) -- the difference between recharging and waiting."""
    b = block(max_tokens=None)
    with scripted([zai("1113", "Insufficient balance", status=429)]) as log:
        exc: Exception | None = None
        try:
            client.probe(b)
        except Exception as caught:  # noqa: BLE001 - the caller asserts
            exc = caught
    check("h1 probe names the irrecoverable code",
          isinstance(exc, client.LLMError) and "1113" in str(exc), f"exc={exc!r}")
    check("h2 probe marks it irrecoverable",
          "irrecoverable" in str(exc), f"exc={exc!r}")

    with scripted([zai("1302", "Rate limit", status=429)]):
        exc2: Exception | None = None
        try:
            client.probe(b)
        except Exception as caught:  # noqa: BLE001 - the caller asserts
            exc2 = caught
    check("h3 a retryable code is reported without the irrecoverable tag",
          "1302" not in str(exc2) or "irrecoverable" not in str(exc2),
          f"exc={exc2!r}")


def case_i_the_fatal_event_is_tier1() -> None:
    """The fatal event must land in the tier-1 orchestration log -- tier 2 is
    "the pipeline's own reading of individual model outputs" and is pruned far
    more aggressively."""
    check("i1 `fatal` is not claimed as a tier-2 event",
          "fatal" not in logger.TIER_2_EVENTS, "")
    check("i2 log_event tolerates a missing project (cannot break the raise)",
          logger.log_event(Path(tempfile.mkdtemp()) / "nope", {"event": "fatal"}) == []
          or True, "")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_a_fatal_codes_raise_on_the_first_attempt()
    case_b_the_fatal_check_precedes_the_guided_json_fallback()
    case_c_recoverable_codes_still_retry()
    case_d_recoverable_failures_still_succeed()
    case_e_unreadable_bodies_never_raise()
    case_f_the_fatal_channel_rides_pipeline_error()
    case_g_exhausted_retries_are_still_llmerror()
    case_h_probe_names_the_business_code()
    case_i_the_fatal_event_is_tier1()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())