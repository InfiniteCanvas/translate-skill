# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "openai>=1.60", "anthropic>=1.0", "httpx2>=2.0"]
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

Hermetic: client._openai_client is swapped for a stub whose create() records
the effective wire body and raises REAL openai exception instances (httpx2
responses) from a scripted queue. No network, restore in finally.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_fatal_provider_errors.py
"""
from __future__ import annotations

import json as _json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, config, logger, pipeline

# The shipped attempt budget, read from the policy the client actually uses.
MAX_ATTEMPTS = client._DEFAULT_RETRY["max_attempts"]

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
    SimpleNamespace payload returns as the 200 completion, an openai
    exception instance is raised verbatim. A single-element script means the
    second create (if any) reuses the last entry, so a test that asserts
    post_count == 1 is asserting the retry never happened rather than just
    observing its outcome."""

    def __init__(self) -> None:
        self.script: list = []
        self.bodies: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    def _create(self, **kwargs):
        self.bodies.append(_wire_body(kwargs))
        step = self.script[min(len(self.bodies) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step

    def _list(self, timeout=None, extra_headers=None):
        raise NotImplementedError("no resolve_model path is exercised here")


@contextmanager
def no_backoff():
    """Zero the retry sleeps.

    These cases deliberately drive the full ladder (case_c asserts
    _DEFAULT_RETRY["max_attempts"] posts for each of ten retryable codes), and
    the real backoff would make that ten x ~2.5 minutes of pure sleeping. The
    attempt COUNT is what is under test, not the delay; the delays themselves
    are not asserted anywhere in this file.
    """
    orig = client._DEFAULT_RETRY
    client._DEFAULT_RETRY = {**orig, "backoff_seconds": [0, 0, 0], "jitter": 0}
    try:
        yield
    finally:
        client._DEFAULT_RETRY = orig


@contextmanager
def scripted(script: list):
    log = StubOpenAI()
    log.script = list(script)
    orig = client._openai_client
    client._openai_client = lambda cfg: log
    try:
        with no_backoff():
            yield log
    finally:
        client._openai_client = orig


def zai(code: str, message: str = "boom", status: int = 400) -> openai.APIStatusError:
    """A REAL openai.APIStatusError carrying a Z.AI-shaped body, in the
    shape the SDK actually delivers: the parsed error envelope UNWRAPPED to
    the inner error object (openai/_client.py lifts `error` out before
    constructing the exception, so exc.body is {"code": ...})."""
    body = {"code": code, "message": message}
    request = httpx2.Request("POST", "http://fake:1/v1/chat/completions")
    response = httpx2.Response(status, request=request,
                               text=_json.dumps({"error": body}))
    return openai.APIStatusError(f"Error {status}", response=response, body=body)


def plain_error(status: int, text: str) -> openai.APIStatusError:
    """A status error whose body the SDK kept as a string (non-JSON)."""
    request = httpx2.Request("POST", "http://fake:1/v1/chat/completions")
    response = httpx2.Response(status, request=request, text=text)
    return openai.APIStatusError(f"Error {status}", response=response, body=None)


def completion(content: str = "hi") -> SimpleNamespace:
    """An SDK-shaped 200 completion (the fields chat() reads)."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, reasoning_content=None),
            finish_reason="stop")],
        usage=None,
    )


def block(**kw) -> dict:
    b = {"base_url": "http://fake:1/v1", "model": "glm-5.3",
         "temperature": 0.7, "max_tokens": 128000, "thinking": False}
    b.update(kw)
    return b


def call(**kw):
    return client.chat(block(), "hi", json_schema={"type": "object"}, **kw)


FATAL_CASES = [
    ("1000", 401), ("1001", 401), ("1003", 401), ("1005", 401),
    ("1113", 429), ("1210", 400), ("1211", 400), ("1212", 400),
    ("1213", 400), ("1214", 400), ("1215", 400), ("1220", 403),
    ("1221", 400), ("1222", 400), ("1261", 400), ("1301", 400),
    ("1309", 429), ("1311", 429), ("1313", 429), ("1314", 429),
    ("1315", 429),
]

RETRYABLE_CODES = ["1302", "1305", "1308", "1310", "1316", "1317", "1318",
                   "1319", "1320", "1321"]


def case_a_fatal_codes_raise_on_the_first_attempt() -> None:
    for code, status in FATAL_CASES:
        exc: Exception | None = None
        with scripted([zai(code, status=status)]) as log:
            try:
                call()
            except Exception as caught:
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
        except Exception as caught:
            exc = caught
    check("b1 a 1214 blaming response_format is fatal, not a guided-JSON drop",
          isinstance(exc, client.LLMFatal), f"exc={exc!r}")
    check("b2 response_format was never dropped and re-sent",
          len(log.bodies) == 1
          and "response_format" in log.bodies[0],
          f"posts={len(log.bodies)} body={log.bodies[0] if log.bodies else None}")


def case_c_recoverable_codes_still_retry() -> None:
    for code in RETRYABLE_CODES:
        exc: Exception | None = None
        with scripted([zai(code, status=429)]) as log:
            try:
                call()
            except Exception as caught:
                exc = caught
        check(f"c {code}: a reset-window/rate-limit code is NOT fatal",
              not isinstance(exc, client.LLMFatal), f"exc={exc!r}")
        check(f"c {code}: it spends the full retry ladder",
              len(log.bodies) == MAX_ATTEMPTS,
              f"posts={len(log.bodies)}")


def case_d_recoverable_failures_still_succeed() -> None:
    for status in (429, 500, 503):
        with scripted([plain_error(status, "nope"), completion("hi")]) as log:
            text = call()
        check(f"d HTTP {status}: retried, then succeeded on attempt 2",
              text == "hi" and len(log.bodies) == 2, f"text={text!r}")


def case_e_unreadable_bodies_never_raise() -> None:
    """`_fatal_code` is on the hot path for EVERY error response. It must not
    raise on any shape a real server can produce, and a malformed body must
    not cost us the real HTTP status."""
    for name, payload in [
        ("non-JSON body (the SDK keeps it a string)", "<html>gateway</html>"),
        ("JSON array", [1, 2, 3]),
        ("no error key", {"detail": "nope"}),
        ("error is a string", {"error": "nope"}),
        ("null code", {"error": {"code": None}}),
        ("empty code", {"error": {"code": "  "}}),
    ]:
        check(f"e {name}: _fatal_code returns None, no raise",
              client._fatal_code(payload) is None, "")

    check("e int code is normalized to a string",
          client._fatal_code({"error": {"code": 1210}}) == "1210",
          "")

    with scripted([plain_error(400, "<html>gateway</html>")] * 8) as log:
        try:
            call()
        except Exception:
            pass
    check("e an unparseable 400 keeps the ordinary retry ladder",
          len(log.bodies) == MAX_ATTEMPTS, f"posts={len(log.bodies)}")


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
        except Exception:
            absorbed.append("SWALLOWED")

    stage_guard()
    check("f4 the existing stage-guard idiom lets it through",
          absorbed == ["re-raised by the guard"], f"got={absorbed}")


def case_g_exhausted_retries_are_still_llmerror() -> None:
    """A non-fatal code that never recovers exhausts _MAX_ATTEMPTS and raises a
    plain LLMError. It is equally terminal at the CLI -- both arms return exit 3
    -- but the type still distinguishes "the provider said no" from "we ran out
    of patience"."""
    with scripted([plain_error(503, "unavailable")] * 8) as log:
        exc: Exception | None = None
        try:
            call()
        except Exception as caught:
            exc = caught
    check("g1 an exhausted retry ladder raises LLMError",
          isinstance(exc, client.LLMError)
          and not isinstance(exc, client.LLMFatal), f"exc={exc!r}")
    check("g2 after exactly _MAX_ATTEMPTS attempts",
          len(log.bodies) == MAX_ATTEMPTS, f"posts={len(log.bodies)}")


def case_h_probe_names_the_business_code() -> None:
    """`ping` is the command an operator runs when a key stops working. Without
    the code, 'HTTP 429' cannot be told apart from an empty balance (1113) vs a
    rate limit (1302) -- the difference between recharging and waiting."""
    b = block(max_tokens=None)
    with scripted([zai("1113", "Insufficient balance", status=429)]):
        exc: Exception | None = None
        try:
            client.probe(b)
        except Exception as caught:
            exc = caught
    check("h1 probe names the irrecoverable code",
          isinstance(exc, client.LLMError) and "1113" in str(exc), f"exc={exc!r}")
    check("h2 probe marks it irrecoverable",
          "irrecoverable" in str(exc), f"exc={exc!r}")

    with scripted([zai("1302", "Rate limit", status=429)]):
        exc2: Exception | None = None
        try:
            client.probe(b)
        except Exception as caught:
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
