# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "openai>=1.60", "anthropic>=1.0", "httpx2>=2.0"]
# ///
"""Transport-level smoke tests: the REAL openai/anthropic SDK clients, with
a MockTransport http client injected, driving client.py's own factories.

The factory-seam suites stub the client objects, so a stub accepts any
kwargs and cannot see what the real SDK rejects. These cases pin the
load-bearing SDK behaviors at the wire instead -- exactly the facts the P1
lenses probed by hand:

  * the openai wire body is what today's client sent: temperature always
    (even null), top_p/top_k/repetition_penalty/chat_template_kwargs/
    response_format merged from extra_body, the cap clamped;
  * the anthropic wire body: max_tokens always present (the API requires
    it), knobs via extra_body, output_config.format for guided JSON, and
    NO output_format key (the stale name the SDK rejects);
  * no-auth construction: a keyless block sends NO Authorization header on
    either SDK -- openai raises "Missing credentials" on api_key="" at
    construction and anthropic 2.x would send an EMPTY "Authorization:
    Bearer" header, both suppressed via the per-call Omit sentinel;
  * usage is flattened through the SDK's real pydantic objects and
    normalized for anthropic before it reaches the trace meta;
  * max_retries=0 and the per-call timeouts (models.list at 30s, probe at
    its own timeout) -- the SDK's internal retry ladder stays OFF;
  * resolve_model's /v1-normalized openai URL and bare-origin anthropic URL.

Hermetic: both SDKs run against httpx2.MockTransport handlers that capture
URL, headers and body; no network.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_sdk_transport.py
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx2
import anthropic as anthropic_sdk
import openai as openai_sdk

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


class OpenAIProxy:
    """Forwards every attribute to the real openai module; OpenAI(...) is
    rebuilt with the suite's MockTransport http client injected, so
    client.py's own factory constructs the REAL client over a captured
    transport."""

    def __init__(self, http_client) -> None:
        self._http_client = http_client

    def __getattr__(self, name):
        return getattr(openai_sdk, name)

    def OpenAI(self, **kwargs):
        return openai_sdk.OpenAI(http_client=self._http_client, **kwargs)


class AnthropicProxy:
    def __init__(self, http_client) -> None:
        self._http_client = http_client

    def __getattr__(self, name):
        return getattr(anthropic_sdk, name)

    def Anthropic(self, **kwargs):
        return anthropic_sdk.Anthropic(http_client=self._http_client, **kwargs)


OPENAI_CAPTURED: list[dict] = []
ANTHROPIC_CAPTURED: list[dict] = []


OPENAI_SCRIPTED: list[httpx2.Response] = []


def openai_handler(request: httpx2.Request) -> httpx2.Response:
    OPENAI_CAPTURED.append({
        "url": str(request.url),
        "auth": request.headers.get("Authorization"),
        "body": json.loads(request.content) if request.content else {},
    })
    if OPENAI_SCRIPTED:
        return OPENAI_SCRIPTED.pop(0)
    if request.url.path.endswith("/models"):
        return httpx2.Response(200, json={"data": [{"id": "glm-5.3"}]})
    return httpx2.Response(200, json={
        "id": "cmpl-1", "object": "chat.completion", "created": 1, "model": "glm-5.3",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "  hi  "},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def anthropic_handler(request: httpx2.Request) -> httpx2.Response:
    ANTHROPIC_CAPTURED.append({
        "url": str(request.url),
        "auth": request.headers.get("Authorization"),
        "body": json.loads(request.content) if request.content else {},
    })
    if request.url.path.endswith("/models"):
        return httpx2.Response(200, json={
            "data": [{"id": "claude-1", "type": "model", "display_name": "Claude"}]})
    return httpx2.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-1",
        "content": [{"type": "text", "text": "  hi  "}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 7, "output_tokens": 3,
                  "cache_creation_input_tokens": 2},
    })


@contextmanager
def transport():
    """Swap client.openai / client.anthropic for proxies running the REAL
    clients over the captured MockTransport handlers."""
    openai_proxy = OpenAIProxy(httpx2.Client(transport=httpx2.MockTransport(openai_handler)))
    anthropic_proxy = AnthropicProxy(
        httpx2.Client(transport=httpx2.MockTransport(anthropic_handler)))
    orig_openai, orig_anthropic = client.openai, client.anthropic
    client.openai = openai_proxy
    client.anthropic = anthropic_proxy
    OPENAI_CAPTURED.clear()
    ANTHROPIC_CAPTURED.clear()
    try:
        yield
    finally:
        client.openai = orig_openai
        client.anthropic = orig_anthropic


BASE_O = "http://x.test"
BASE_A = "http://y.test"
SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


def openai_block(**kw) -> dict:
    b = {"base_url": BASE_O, "model": "glm-5.3", "thinking": False, "api_key": "sk-1"}
    b.update(kw)
    return b


def anthropic_block(**kw) -> dict:
    b = {"base_url": BASE_A, "model": "claude-1", "thinking": False,
         "protocol": "anthropic", "api_key": "k-1"}
    b.update(kw)
    return b


def case_construction_pins() -> None:
    """max_retries=0 (OUR ladder is the only retry policy) and the chat
    timeout live on the constructed clients."""
    with transport():
        c = client._openai_client(openai_block())
        check("o1 construction: the openai client carries max_retries=0",
              c.max_retries == 0, f"max_retries={c.max_retries!r}")
        check("o2 construction: the openai client carries the 600s chat timeout",
              int(c.timeout) == client._CHAT_TIMEOUT,
              f"timeout={c.timeout!r}")
        a = client._anthropic_client(anthropic_block())
        check("a1 construction: the anthropic client carries max_retries=0",
              a.max_retries == 0, f"max_retries={a.max_retries!r}")


def case_openai_wire_body() -> None:
    """The openai wire body is what today's requests-based client sent: the
    known knobs, then the user's extra_body, then response_format -- merged
    through the SDK's own extra_body serialization."""
    with transport():
        client.chat(openai_block(temperature=0.7, top_p=0.9, top_k=40,
                                 repetition_penalty=1.1, thinking=True,
                                 extra_body={"user_extra": 1}),
                    "hello", json_schema=SCHEMA)
    wire = OPENAI_CAPTURED[0]
    check("o3 wire: posts to the /v1-normalized chat URL",
          wire["url"] == "http://x.test/v1/chat/completions", f"url={wire['url']!r}")
    check("o4 wire: the keyed block sends the Bearer header",
          wire["auth"] == "Bearer sk-1", f"auth={wire['auth']!r}")
    body = wire["body"]
    check("o5 wire: model/messages/temperature/max_tokens as declared params",
          body["model"] == "glm-5.3"
          and body["messages"] == [{"role": "user", "content": "hello"}]
          and body["temperature"] == 0.7
          and body["max_tokens"] == client.config.DEFAULT_MAX_TOKENS,
          f"body keys={sorted(body)}")
    check("o6 wire: top_p as a declared param",
          body.get("top_p") == 0.9, f"top_p={body.get('top_p')!r}")
    check("o7 wire: top_k/repetition_penalty/chat_template_kwargs merged "
          "from extra_body (the SDK has no declared params for them)",
          body.get("top_k") == 40
          and body.get("repetition_penalty") == 1.1
          and body.get("chat_template_kwargs") == {"enable_thinking": True},
          f"body knobs={ {k: body.get(k) for k in ('top_k', 'repetition_penalty', 'chat_template_kwargs')} }")
    check("o8 wire: the user's extra_body rides through",
          body.get("user_extra") == 1, f"user_extra={body.get('user_extra')!r}")
    check("o9 wire: response_format last-wins json_schema",
          body.get("response_format") == {"type": "json_schema",
                                          "json_schema": {"name": "response",
                                                          "schema": SCHEMA}},
          f"response_format={body.get('response_format')!r}")


def case_openai_wire_identity_facts() -> None:
    """Two wire-identity facts the factory-seam suites cannot see: the 0.2
    temperature default, the null-temperature passthrough (today's client
    sent even null), and the ZAI fatal channel through the REAL SDK's
    unwrapped error body."""
    with transport():
        client.chat(openai_block(), "hi")
        body = OPENAI_CAPTURED[0]["body"]
        check("o14 wire: a block without temperature sends the 0.2 default",
              body.get("temperature") == 0.2, f"temperature={body.get('temperature')!r}")

    with transport():
        client.chat(openai_block(temperature=None), "hi")
        body = OPENAI_CAPTURED[0]["body"]
        check("o15 wire: an explicit null temperature is SENT as null (not dropped)",
              "temperature" in body and body["temperature"] is None,
              f"temperature={body.get('temperature')!r}")

    with transport():
        OPENAI_SCRIPTED.append(httpx2.Response(
            400, json={"error": {"code": "1214", "message": "invalid params"}},
            request=httpx2.Request("POST", "http://x.test/v1/chat/completions")))
        exc = None
        with no_backoff():
            try:
                client.chat(openai_block(), "hi", json_schema=SCHEMA)
            except Exception as caught:
                exc = caught
        check("o16 fatal: a real SDK 400 carrying a ZAI code raises LLMFatal",
              isinstance(exc, client.LLMFatal) and "1214" in str(exc)
              and "irrecoverable" in str(exc), f"exc={exc!r}")


@contextmanager
def no_backoff():
    orig = client._BACKOFF
    client._BACKOFF = (0, 0, 0)
    try:
        yield
    finally:
        client._BACKOFF = orig


def case_openai_no_auth() -> None:
    """A keyless openai block sends NO Authorization header: the SDK refuses
    api_key='' at construction, so the factory sets a placeholder and the
    per-request Omit sentinel suppresses the header."""
    with transport():
        client.chat(openai_block(api_key=None), "hi")
        check("o10 no-auth: the request goes out without an Authorization header",
              OPENAI_CAPTURED[0]["auth"] is None,
              f"auth={OPENAI_CAPTURED[0]['auth']!r}")

        client._MODEL_CACHE.clear()
        got = client.resolve_model(BASE_O)
        check("o11 no-auth: a keyless resolve lists models with no auth header",
              got == "glm-5.3"
              and OPENAI_CAPTURED[-1]["url"] == "http://x.test/v1/models"
              and OPENAI_CAPTURED[-1]["auth"] is None,
              f"got={got!r} url={OPENAI_CAPTURED[-1]['url']!r} "
              f"auth={OPENAI_CAPTURED[-1]['auth']!r}")
        client._MODEL_CACHE.clear()


def case_anthropic_wire_body() -> None:
    """The native Messages wire body: max_tokens required and present, knobs
    via extra_body, output_config.format for guided JSON, no stale names."""
    with transport():
        metas: list[dict] = []
        client.chat(anthropic_block(temperature=0.7, top_k=40,
                                    extra_body={"cache_hint": 1}),
                    "hello", json_schema=SCHEMA, meta_hook=metas.append)
    wire = ANTHROPIC_CAPTURED[0]
    check("a2 wire: posts to the bare-origin /v1/messages URL",
          wire["url"] == "http://y.test/v1/messages", f"url={wire['url']!r}")
    check("a3 wire: the keyed block sends the Bearer header",
          wire["auth"] == "Bearer k-1", f"auth={wire['auth']!r}")
    body = wire["body"]
    check("a4 wire: max_tokens is always present (the API requires it)",
          body.get("max_tokens") == client.config.DEFAULT_MAX_TOKENS,
          f"max_tokens={body.get('max_tokens')!r}")
    check("a5 wire: temperature/top_k ride extra_body into the body",
          body.get("temperature") == 0.7 and body.get("top_k") == 40
          and body.get("cache_hint") == 1,
          f"body knobs={ {k: body.get(k) for k in ('temperature', 'top_k', 'cache_hint')} }")
    check("a6 wire: guided JSON is output_config.format, never output_format",
          body.get("output_config") == {"format": {"type": "json_schema",
                                                   "schema": SCHEMA}}
          and "output_format" not in body,
          f"output_config={body.get('output_config')!r}")
    check("a7 wire: the openai-only knobs never reach the anthropic wire",
          "chat_template_kwargs" not in body and "repetition_penalty" not in body,
          f"body keys={sorted(body)}")
    with transport():
        client.chat(anthropic_block(temperature=None, top_p=None, top_k=None), "hi")
    body = ANTHROPIC_CAPTURED[0]["body"]
    check("a9 wire: a null-knob block sends no temperature/top_p/top_k at all",
          "temperature" not in body and "top_p" not in body and "top_k" not in body,
          f"body keys={sorted(body)}")


def case_anthropic_no_auth() -> None:
    """A keyless anthropic block sends NO Authorization header -- anthropic
    2.x turns an empty api_key into an EMPTY 'Bearer' header, which the
    per-request Omit sentinel suppresses."""
    with transport():
        client.chat(anthropic_block(api_key=None), "hi")
        check("a10 no-auth: the request goes out without an Authorization header",
              ANTHROPIC_CAPTURED[0]["auth"] is None,
              f"auth={ANTHROPIC_CAPTURED[0]['auth']!r}")


def case_usage_and_resolve() -> None:
    """Usage flattens through the real pydantic objects (openai) and
    normalizes for anthropic before the trace meta; resolve_model hits the
    per-protocol models URLs."""
    with transport():
        metas: list[dict] = []
        client.chat(openai_block(), "hi", meta_hook=metas.append)
        usage = metas[-1]["usage"]
        check("o12 usage: the real pydantic usage flattens to the OpenAI dict",
              usage == {"prompt_tokens": 10, "completion_tokens": 5,
                        "total_tokens": 15},
              f"usage={usage!r}")

    with transport():
        metas: list[dict] = []
        client.chat(anthropic_block(), "hi", meta_hook=metas.append)
        usage = metas[-1]["usage"]
        check("a11 usage: the real anthropic usage normalizes, extras kept",
              usage == {"cache_creation_input_tokens": 2, "prompt_tokens": 7,
                        "completion_tokens": 3, "total_tokens": 10},
              f"usage={usage!r}")
        check("a12 finish_reason: stop_reason passes through",
              metas[-1]["finish_reason"] == "end_turn", f"meta={metas[-1]!r}")

    client._MODEL_CACHE.clear()
    with transport():
        got = client.resolve_model(BASE_O, headers={"Authorization": "Bearer sk-1"})
        check("o13 resolve: GETs the /v1-normalized models URL with auth",
              got == "glm-5.3" and OPENAI_CAPTURED[-1]["url"]
              == "http://x.test/v1/models"
              and OPENAI_CAPTURED[-1]["auth"] == "Bearer sk-1",
              f"got={got!r} url={OPENAI_CAPTURED[-1]['url']!r}")
    client._MODEL_CACHE.clear()
    with transport():
        got = client.resolve_model(BASE_A, protocol="anthropic")
        check("a13 resolve: GETs the bare-origin anthropic models URL, no header",
              got == "claude-1" and ANTHROPIC_CAPTURED[-1]["url"]
              == "http://y.test/v1/models"
              and ANTHROPIC_CAPTURED[-1]["auth"] is None,
              f"got={got!r} url={ANTHROPIC_CAPTURED[-1]['url']!r} "
              f"auth={ANTHROPIC_CAPTURED[-1]['auth']!r}")
    client._MODEL_CACHE.clear()


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_construction_pins()
    case_openai_wire_body()
    case_openai_wire_identity_facts()
    case_openai_no_auth()
    case_anthropic_wire_body()
    case_anthropic_no_auth()
    case_usage_and_resolve()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
