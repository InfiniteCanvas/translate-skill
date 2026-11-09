"""Chat client for OpenAI-compatible or Anthropic-compatible LLM endpoints
(official `openai` / `anthropic` packages, selected per provider block by the
`protocol` field) with retries and robust JSON extraction from model replies."""

import json
import os
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

import anthropic
import openai

from lib import config
from lib.errors import PipelineError as _PipelineError

_MODELS_TIMEOUT = 30
_CHAT_TIMEOUT = 600
_BACKOFF = (2, 4, 8)
_MAX_ATTEMPTS = 4

_OPENAI_PROTOCOL = "openai"
_ANTHROPIC_PROTOCOL = "anthropic"


class LLMError(Exception):
    """Connection, HTTP, or response-shape failure talking to the LLM server."""


class LLMFatal(LLMError, _PipelineError):
    """A provider failure the pipeline must NOT absorb, ever.

    Two things make it fatal rather than merely failed:

    * `PipelineError` in the bases. Every stage guard in `pipeline.py` is
      written as `except PipelineError: raise` immediately before its broad
      `except Exception`, so this rides the codebase's existing fatal channel
      through all of them with no edits to any of them. It is the ONLY safe way
      to make a provider failure terminal from here: a plain LLMError is caught
      by the first broad handler it meets (retry feedback, "notes are
      optional", "one bad batch must not kill the review") and dies quietly
      several layers below `main()`.
    * `LLMError` in the bases, so every existing `except client.LLMError` and
      every `isinstance` check keeps working unchanged.

    Raised when the provider says the request itself is wrong (a
    ZAI_FATAL_CODES member): no retry, no degrade, no chapter. Retrying these
    spends real wall-clock and produces the same refusal every time.
    """


_MODEL_CACHE: dict[tuple[str, str | None, str], str] = {}
_PAIRS = {"{": "}", "[": "]"}
_FENCE_RE = re.compile(r"^```[\w+-]*[ \t]*\n?(.*?)\n?[ \t]*```$", re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_GUIDED_UNSUPPORTED_RE = re.compile(
    r"response_format|json_schema|guided json|x-guided|output_config|output_format", re.IGNORECASE)

ZAI_FATAL_CODES = frozenset({
    "1000",
    "1001",
    "1003",
    "1005",
    "1113",
    "1220",
    "1309",
    "1311",
    "1313",
    "1314",
    "1315",
    "1210",
    "1211",
    "1212",
    "1213",
    "1214",
    "1215",
    "1221",
    "1222",
    "1261",
    "1301",
})


def _fatal_code(payload) -> str | None:
    """The provider's business error code from a parsed error body, or None.

    Tolerates every shape that can show up: the SDK's unwrapped error object
    (`{"code": 1214, ...}` -- openai lifts `error` out of the envelope when
    building its status exceptions), the raw wrapped envelope
    (`{"error": {"code": ...}}`), a non-object body, a missing/None `error`,
    an `error` that is not an object, and an `int` code where the vendor
    documented a string. Returns a string in every case so the comparison
    against ZAI_FATAL_CODES cannot miss on type.

    Never raises. A malformed body is not itself a reason to lose the real
    HTTP status, which the caller still needs for the ordinary ladder.
    """
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        error = payload if "code" in payload else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if code is None or isinstance(code, bool):
        return None
    return str(code).strip() or None


def _status_fatal_code(exc) -> str | None:
    """The code from an SDK status error when it is one retrying cannot fix,
    else None. The SDK hands the parsed body over as `exc.body` -- UNWRAPPED
    to the inner error object on openai (openai/_client.py lifts `error` out
    of the envelope), so both shapes are read; a string body falls back to
    the raw response's JSON."""
    payload = getattr(exc, "body", None)
    if not isinstance(payload, dict):
        try:
            payload = exc.response.json()
        except Exception:
            return None
    code = _fatal_code(payload)
    return code if code in ZAI_FATAL_CODES else None


def _error_text(exc) -> str:
    """The provider's error text for messages and the guided-JSON regex: the
    body when the SDK kept it as a string, else the raw response text."""
    if isinstance(getattr(exc, "body", None), str):
        return exc.body
    try:
        return exc.response.text
    except Exception:
        return str(exc)


def _v1_url(base_url: str) -> str:
    """Normalize a base URL for OpenAI-compatible routing.

    A bare origin (no path, e.g. http://host:8888) gets /v1 appended;
    anything with a real path is trusted as-is - hosted providers version
    their routes differently (https://api.z.ai/api/paas/v4, an explicit
    .../v1, ...) and must not gain a /v1."""
    base = base_url.rstrip("/")
    parsed = urlparse(base)
    if not parsed.path or parsed.path == "/":
        return base + "/v1"
    return base


def _resolved_api_key(provider_cfg: dict) -> str:
    """The block's key: "api_key" directly, or "api_key_env" naming an
    environment variable (preferred - keeps keys out of config.json). Empty
    when the provider block carries neither."""
    api_key = provider_cfg.get("api_key")
    if not api_key and provider_cfg.get("api_key_env"):
        api_key = os.environ.get(str(provider_cfg["api_key_env"]))
    return api_key or ""


def auth_headers(provider_cfg: dict) -> dict | None:
    """Authorization header for hosted providers (see _resolved_api_key).
    None when the provider block carries no key - the request then goes out
    with no auth header at all (both SDKs suppress it via _openai_auth_extra
    / _anthropic_auth_extra)."""
    api_key = _resolved_api_key(provider_cfg)
    return {"Authorization": f"Bearer {api_key}"} if api_key else None


def _bearer_key(headers: dict | None) -> str:
    """The key inside an auth_headers() dict, or empty when there is none."""
    auth = (headers or {}).get("Authorization") or ""
    return auth[len("Bearer "):] if auth.startswith("Bearer ") else ""


def _openai_client(provider_cfg: dict):
    """The openai-protocol SDK client for one provider block. Tests patch
    this factory - it is the transport seam (the old suites swapped
    client.requests). max_retries=0 keeps OUR 4-attempt ladder the single
    retry policy; openai 3.x refuses api_key="" at construction, so a no-key
    block gets a placeholder and suppresses the header per request."""
    api_key = _resolved_api_key(provider_cfg)
    return openai.OpenAI(
        api_key=api_key or "unused",
        base_url=_v1_url(str(provider_cfg["base_url"])),
        timeout=_CHAT_TIMEOUT,
        max_retries=0,
    )


def _anthropic_client(provider_cfg: dict):
    """The anthropic-protocol SDK client for one provider block. base_url is
    the bare origin verbatim (the SDK appends /v1/messages); _v1_url is NOT
    applied. max_retries=0 keeps OUR ladder the single retry policy."""
    api_key = _resolved_api_key(provider_cfg)
    return anthropic.Anthropic(
        api_key=api_key or "",
        base_url=str(provider_cfg["base_url"]).rstrip("/"),
        timeout=_CHAT_TIMEOUT,
        max_retries=0,
    )


def _openai_auth_extra(api_key: str) -> dict:
    """extra_headers that suppress the Authorization header when the block
    carries no key: the auth layer yields nothing for an empty key, and the
    Omit sentinel marks the header explicitly omitted."""
    return {} if api_key else {"Authorization": openai.Omit()}


def _anthropic_auth_extra(api_key: str) -> dict:
    """extra_headers that suppress the Authorization header when the block
    carries no key: anthropic turns an empty api_key into an empty
    "Authorization: Bearer" header, which a no-auth endpoint must not see."""
    return {} if api_key else {"Authorization": anthropic.Omit()}


def _usage_dict(usage) -> dict | None:
    """The SDK's pydantic usage flattened to the OpenAI-shaped dict the
    trace-log counters consume (logger._count requires a dict; the raw
    pydantic object would silently zero the token accounting AND break
    json.dumps). None-valued detail fields are dropped: the pydantic model
    declares more keys than the server sent, and a verbatim passthrough of
    the response -- today's contract -- carries only what arrived."""
    if usage is None:
        return None
    dump = (usage.model_dump(exclude_none=True)
            if hasattr(usage, "model_dump") else usage)
    return dump if isinstance(dump, dict) else None


def _anthropic_meta_usage(message) -> dict | None:
    usage = _usage_dict(getattr(message, "usage", None))
    return _normalize_anthropic_usage(usage) if usage else None


def _normalize_anthropic_usage(usage: dict) -> dict:
    """input_tokens/output_tokens -> the OpenAI-shaped keys the trace
    counters read; raw anthropic extras (cache tokens) pass through as-is."""
    out = {k: v for k, v in usage.items()
           if k not in ("input_tokens", "output_tokens", "total_tokens")}
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    out["prompt_tokens"] = prompt
    out["completion_tokens"] = completion
    out["total_tokens"] = (prompt or 0) + (completion or 0)
    return out


def resolve_model(base_url: str, headers: dict | None = None,
                  protocol: str = _OPENAI_PROTOCOL) -> str:
    """Return the endpoint's first model id (cached per base_url + auth +
    protocol in a module dict), listing models through the protocol's SDK
    client. Raises LLMError on connection failure or an unexpected payload."""
    try:
        if protocol == _OPENAI_PROTOCOL:
            base = _v1_url(base_url)
        elif protocol == _ANTHROPIC_PROTOCOL:
            base = base_url.rstrip("/")
        else:
            raise LLMError(
                f'resolve_model: protocol must be "openai" or "anthropic" (got {protocol!r})')
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"failed to list models at {base_url}: {exc}") from exc
    auth = headers.get("Authorization") if headers else None
    key = (base, auth, protocol)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    api_key = _bearer_key(headers)
    try:
        if protocol == _OPENAI_PROTOCOL:
            client = _openai_client({"base_url": base, "api_key": api_key})
            no_auth = _openai_auth_extra(api_key)
        else:
            client = _anthropic_client({"base_url": base, "api_key": api_key})
            no_auth = _anthropic_auth_extra(api_key)
    except Exception as exc:
        raise LLMError(f"failed to list models at {base}: {exc}") from exc
    try:
        if no_auth:
            page = client.models.list(timeout=_MODELS_TIMEOUT, extra_headers=no_auth)
        else:
            page = client.models.list(timeout=_MODELS_TIMEOUT)
    except Exception as exc:
        raise LLMError(f"failed to list models at {base}: {exc}") from exc
    data = getattr(page, "data", None)
    if not isinstance(data, list):
        raise LLMError(f"unexpected payload from {base}: no 'data' list ({str(page)[:200]})")
    for item in data:
        item_id = getattr(item, "id", None)
        if item_id is None and isinstance(item, dict):
            item_id = item.get("id")
        if item_id:
            _MODEL_CACHE[key] = item_id
            return item_id
    raise LLMError(f"unexpected payload from {base}: no model id in 'data' ({str(page)[:200]})")


def probe(provider_cfg: dict, timeout: int = 30) -> str:
    """One minimal chat completion with no retries - a connectivity + auth
    check for ping, over the block's protocol (model/messages/max_tokens: 8,
    nothing else, so optional provider-specific body fields can't skew the
    result). Returns the reply text (possibly empty - any 200 proves routing
    + auth); raises LLMError on any failure. Requires an explicit model in
    the provider block."""
    model = provider_cfg.get("model")
    if not model:
        raise LLMError("probe needs an explicit model (set providers.<job>.model)")
    protocol = provider_cfg.get("protocol", _OPENAI_PROTOCOL)
    if protocol not in (_OPENAI_PROTOCOL, _ANTHROPIC_PROTOCOL):
        base = str(provider_cfg.get("base_url", ""))
        raise LLMError(
            f'provider block {base}: protocol must be "openai" or "anthropic" (got {protocol!r})')
    api_key = _resolved_api_key(provider_cfg)
    if protocol == _OPENAI_PROTOCOL:
        try:
            base = _v1_url(str(provider_cfg["base_url"]))
            url = base + "/chat/completions"
            client = _openai_client(provider_cfg)
        except Exception as exc:
            raise LLMError(f"probe request to {provider_cfg['base_url']} failed: {exc}") from exc
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "timeout": timeout,
        }
        if not api_key:
            kwargs["extra_headers"] = _openai_auth_extra(api_key)
        try:
            completion = client.chat.completions.create(**kwargs)
        except openai.APIConnectionError as exc:
            raise LLMError(f"probe request to {url} failed: {exc}") from exc
        except openai.APIStatusError as exc:
            fatal = _status_fatal_code(exc)
            detail = f"provider code {fatal} (irrecoverable)" if fatal else ""
            raise LLMError(f"probe HTTP {exc.status_code} from {url}"
                           f"{': ' + detail if detail else ''}: {_error_text(exc)[:200]}")
        except Exception as exc:
            raise LLMError(f"probe request to {url} failed: {exc}") from exc
        try:
            choices = getattr(completion, "choices", None)
            if isinstance(completion, str) or not choices:
                raise LLMError(f"probe got an unexpected payload from {url}: {str(completion)[:200]}")
            content = getattr(getattr(choices[0], "message", None), "content", None)
            return str(content or "")
        except (TypeError, KeyError, IndexError, AttributeError) as exc:
            raise LLMError(f"probe got an unexpected payload from {url}: {exc}") from exc
    try:
        base = str(provider_cfg["base_url"]).rstrip("/")
        url = base + "/v1/messages"
        client = _anthropic_client(provider_cfg)
    except Exception as exc:
        raise LLMError(f"probe request to {provider_cfg['base_url']} failed: {exc}") from exc
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "timeout": timeout,
    }
    if not api_key:
        kwargs["extra_headers"] = _anthropic_auth_extra(api_key)
    try:
        message = client.messages.create(**kwargs)
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"probe request to {url} failed: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"probe HTTP {exc.status_code} from {url}: {_error_text(exc)[:200]}")
    except Exception as exc:
        raise LLMError(f"probe request to {url} failed: {exc}") from exc
    try:
        blocks = getattr(message, "content", None)
        if isinstance(message, str) or blocks is None:
            raise LLMError(f"probe got an unexpected payload from {url}: {str(message)[:200]}")
        text = "".join(
            block.text for block in blocks
            if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str))
        return text or ""
    except LLMError:
        raise
    except (TypeError, KeyError, IndexError, AttributeError) as exc:
        raise LLMError(f"probe got an unexpected payload from {url}: {exc}") from exc


def _resolve_cap(provider_cfg: dict, max_tokens: int | None,
                 enforce_ceiling: bool) -> int:
    """The output cap this request actually sends.

    Two independent clamps, in this order:

    1. A caller-supplied `max_tokens` is a CEILING, not an instruction: it may
       lower a block's own cap but never raise it. `enforce_ceiling=False` is
       the deliberate opt-out, used only by the consensus synthesis
       (consensus.chat), which must be able to exceed its own block's cap to
       merge full-size candidates -- consensus.chat computes
       max(task cap, block cap) on purpose.

    2. `max_tokens_limit`, when the block declares one, is a HARD ceiling: the
       provider's own rejection threshold, not a budget. It binds EVERY call,
       including that synthesis. This is the difference between "how much this
       block wants" and "how much the server will accept", which a single
       max_tokens cannot express -- without it, a consensus block set to its
       provider's real limit (say 131072) under a 256000 task cap was sent
       256000 and came back HTTP 400, degrading the merge away.

    See config.block_cap for why the two keys are separate.

    Provider `max_tokens` is NOT schema-validated (references/file-formats.md:
    provider sampling knobs are read per request and not validated), so it can
    arrive as null or a non-int -- and neither is max_tokens_limit.
    `or DEFAULT_MAX_TOKENS` is the repo-wide idiom (config.block_cap,
    consensus.chat) and keeps null/0 meaning "unset", exactly as the previous
    `max_tokens or provider_cfg.get(...)` did.
    """
    block_max = config.block_cap(provider_cfg)
    if not max_tokens:
        cap = block_max
    elif enforce_ceiling:
        cap = min(int(max_tokens), block_max)
    else:
        cap = int(max_tokens)
    limit = provider_cfg.get("max_tokens_limit")
    return min(cap, int(limit)) if limit else cap


def chat(provider_cfg: dict, prompt: str, json_schema: dict | None = None,
         max_tokens: int | None = None,
         meta_hook: Callable[[dict], None] | None = None,
         enforce_ceiling: bool = True) -> str:
    """One chat completion over the provider block's protocol; returns the
    reply text (openai protocol: choices[0].message.content.strip();
    anthropic: the joined `type: "text"` content blocks, stripped).

    The `protocol` field selects the SDK: "openai" (the default when absent)
    speaks chat.completions; "anthropic" speaks the native Messages API,
    guided by output_config.format. `temperature`/`top_p`/`top_k` ride
    extra_body on the anthropic path (null-valued knobs are omitted there --
    a null temperature would 400 -- while the openai path sends them even
    null for wire parity); `repetition_penalty` and `thinking` are
    openai-protocol knobs (sglang) and are ignored on anthropic blocks,
    whose native thinking is reachable through extra_body.

    Auth for hosted providers: the provider block may carry "api_key"
    directly or "api_key_env" naming an environment variable (preferred -
    keeps keys out of config.json). The header is never included in
    trace-log metadata.

    Retries up to 4 attempts total (backoff 2s/4s/8s) on connection errors,
    HTTP >= 500, 429, and a 400 received while the guided schema is set whose
    error text does not blame guided JSON (a real failure -- bad model,
    oversized context -- must not silently drop the schema). A 400 whose text
    names the mechanism (response_format, json_schema, "guided json",
    x-guided, output_config, output_format) triggers one immediate retry
    WITHOUT the schema (the server may not support guided JSON). Other 4xx
    raise LLMError with the status code and the first 400 chars of the
    response text. On the openai protocol, a body carrying a
    ZAI_FATAL_CODES member raises LLMFatal on the FIRST response, checked
    before the guided fallback.

    meta_hook, when given, is invoked with call metadata for trace logs:
    once with the request (url, model, params, full prompt) BEFORE the call,
    and once with the response (raw content, finish_reason, usage, elapsed,
    error) after it completes or exhausts retries. Both metas carry the same
    call_id and an "event" field ("llm_request" / "llm_response") so they
    pair up as two JSONL lines per call -- a call that hits the 400 fallback
    (retry without the guided schema) adds one extra "llm_request" line for
    the retried request.

    `max_tokens` is the caller's ceiling and is clamped DOWN to the block's
    own cap (see _resolve_cap); it can never raise a block above its declared
    limit. `enforce_ceiling=False` disables that clamp and exists for the
    consensus synthesis only -- and never disables a block's
    `max_tokens_limit`, which is a provider limit rather than a budget.
    """
    protocol = provider_cfg.get("protocol", _OPENAI_PROTOCOL)
    if protocol not in (_OPENAI_PROTOCOL, _ANTHROPIC_PROTOCOL):
        base = str(provider_cfg.get("base_url", ""))
        raise LLMError(
            f'provider block {base}: protocol must be "openai" or "anthropic" (got {protocol!r})')
    if protocol == _OPENAI_PROTOCOL:
        return _chat_openai(provider_cfg, prompt, json_schema, max_tokens,
                            meta_hook, enforce_ceiling)
    return _chat_anthropic(provider_cfg, prompt, json_schema, max_tokens,
                           meta_hook, enforce_ceiling)


def _chat_openai(provider_cfg: dict, prompt: str, json_schema: dict | None,
                 max_tokens: int | None, meta_hook, enforce_ceiling: bool) -> str:
    raw_base = str(provider_cfg["base_url"])
    try:
        base_url = _v1_url(raw_base)
        url = base_url + "/chat/completions"
        client = _openai_client(provider_cfg)
    except Exception as exc:
        raise LLMError(f"request to {raw_base} failed: {exc}") from exc
    api_key = _resolved_api_key(provider_cfg)
    no_auth = _openai_auth_extra(api_key)
    model = provider_cfg.get("model") or resolve_model(
        base_url, headers=auth_headers(provider_cfg), protocol=_OPENAI_PROTOCOL)
    body_extra: dict[str, Any] = {}
    if provider_cfg.get("thinking") is not None:
        body_extra["chat_template_kwargs"] = {"enable_thinking": bool(provider_cfg["thinking"])}
    if provider_cfg.get("top_k") is not None:
        body_extra["top_k"] = int(provider_cfg["top_k"])
    if provider_cfg.get("repetition_penalty") is not None:
        body_extra["repetition_penalty"] = float(provider_cfg["repetition_penalty"])
    extra = provider_cfg.get("extra_body")
    if extra is not None:
        if not isinstance(extra, dict):
            raise LLMError("providers.<job>.extra_body must be a JSON object")
        body_extra.update(extra)
    if json_schema is not None:
        body_extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": json_schema},
        }
    temperature = provider_cfg.get("temperature", 0.2)
    cap = _resolve_cap(provider_cfg, max_tokens, enforce_ceiling)

    call_id = uuid.uuid4().hex[:12]

    def _request_meta() -> dict[str, Any]:
        params: dict[str, Any] = {
            "temperature": body_extra.get("temperature", temperature),
            "max_tokens": body_extra.get("max_tokens", cap),
        }
        top_p = body_extra.get("top_p")
        if top_p is None and provider_cfg.get("top_p") is not None:
            top_p = float(provider_cfg["top_p"])
        if top_p is not None:
            params["top_p"] = top_p
        for key in ("top_k", "repetition_penalty", "chat_template_kwargs"):
            if key in body_extra:
                params[key] = body_extra[key]
        if isinstance(extra, dict) and extra:
            params["extra_body"] = extra
        return {
            "event": "llm_request",
            "call_id": call_id,
            "url": url,
            "model": model,
            "params": params,
            "guided_json": "response_format" in body_extra,
            "prompt": prompt,
        }

    def _response_meta(response: str | None = None, finish_reason: object = None,
                       usage: object = None, elapsed: float = 0.0,
                       error: str | None = None) -> dict[str, Any]:
        return {
            "event": "llm_response",
            "call_id": call_id,
            "url": url,
            "model": model,
            "response": response,
            "finish_reason": finish_reason,
            "usage": usage,
            "elapsed_s": round(elapsed, 2),
            "error": error,
        }

    if meta_hook:
        meta_hook(_request_meta())
    started = time.monotonic()
    failures = 0
    while True:
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": cap,
            }
            if provider_cfg.get("top_p") is not None:
                kwargs["top_p"] = float(provider_cfg["top_p"])
            if body_extra:
                kwargs["extra_body"] = dict(body_extra)
            if no_auth:
                kwargs["extra_headers"] = no_auth
            completion = client.chat.completions.create(**kwargs)
        except openai.APIConnectionError as exc:
            failures += 1
            if failures >= _MAX_ATTEMPTS:
                err = f"request to {url} failed after {failures} attempts: {exc}"
                if meta_hook:
                    meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
                raise LLMError(err) from exc
            time.sleep(_BACKOFF[failures - 1])
            continue
        except openai.APIStatusError as exc:
            fatal = _status_fatal_code(exc)
            if fatal is not None:
                err = (f"HTTP {exc.status_code} from {url}: provider code {fatal} "
                       f"(irrecoverable - retrying cannot help): {_error_text(exc)[:400]}")
                if meta_hook:
                    meta_hook(_response_meta(elapsed=time.monotonic() - started,
                                             error=err))
                raise LLMFatal(err)

            retryable_400 = False
            if exc.status_code == 400 and "response_format" in body_extra:
                if _GUIDED_UNSUPPORTED_RE.search(_error_text(exc)):
                    body_extra.pop("response_format")
                    if meta_hook:
                        meta_hook(_request_meta())
                    continue
                retryable_400 = True

            if retryable_400 or exc.status_code == 429 or exc.status_code >= 500:
                failures += 1
                if failures >= _MAX_ATTEMPTS:
                    err = (f"HTTP {exc.status_code} from {url} after {failures} "
                           f"attempts: {_error_text(exc)[:400]}")
                    if meta_hook:
                        meta_hook(_response_meta(elapsed=time.monotonic() - started,
                                                 error=err))
                    raise LLMError(err)
                time.sleep(_BACKOFF[failures - 1])
                continue

            err = f"HTTP {exc.status_code} from {url}: {_error_text(exc)[:400]}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err)

        except Exception as exc:
            err = f"request to {url} failed: {exc}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err) from exc

        try:
            choices = getattr(completion, "choices", None)
            if isinstance(completion, str) or not choices:
                payload_error = getattr(completion, "error", None)
                if isinstance(payload_error, dict):
                    fatal = _fatal_code({"error": payload_error})
                    if fatal in ZAI_FATAL_CODES:
                        err = (f"HTTP 200 from {url}: provider code {fatal} "
                               f"(irrecoverable - retrying cannot help): "
                               f"{str(completion)[:400]}")
                        if meta_hook:
                            meta_hook(_response_meta(elapsed=time.monotonic() - started,
                                                     error=err))
                        raise LLMFatal(err)
                err = f"unexpected response payload from {url}: {str(completion)[:400]}"
                if meta_hook:
                    meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
                raise LLMError(err)
            choice = choices[0]
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None)
            if isinstance(content, str):
                content = _THINK_BLOCK_RE.sub("", content, count=1)
            if not isinstance(content, str) or not content.strip():
                reasoning = getattr(message, "reasoning_content", None)
                hint = (" (reasoning_content present - the budget went to thinking: lower "
                        "providers.<job>.extra_body.reasoning_effort for a hosted reasoning "
                        "model, or set providers.<job>.thinking=false for a local sglang "
                        "server)") if reasoning else ""
                err = f"empty completion content from {url}{hint}"
                if meta_hook:
                    meta_hook(_response_meta(
                        response=content if isinstance(content, str) else None,
                        finish_reason=getattr(choice, "finish_reason", None),
                        usage=_usage_dict(getattr(completion, "usage", None)),
                        elapsed=time.monotonic() - started,
                        error=err,
                    ))
                raise LLMError(err)
        except (TypeError, KeyError, IndexError, AttributeError) as exc:
            err = f"unexpected response payload from {url}: {exc}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err) from exc

        if meta_hook:
            meta_hook(_response_meta(
                response=content,
                finish_reason=getattr(choice, "finish_reason", None),
                usage=_usage_dict(getattr(completion, "usage", None)),
                elapsed=time.monotonic() - started,
            ))
        return content.strip()


def _chat_anthropic(provider_cfg: dict, prompt: str, json_schema: dict | None,
                    max_tokens: int | None, meta_hook, enforce_ceiling: bool) -> str:
    raw_base = str(provider_cfg["base_url"])
    try:
        base = raw_base.rstrip("/")
        url = base + "/v1/messages"
        client = _anthropic_client(provider_cfg)
    except Exception as exc:
        raise LLMError(f"request to {raw_base} failed: {exc}") from exc
    api_key = _resolved_api_key(provider_cfg)
    no_auth = _anthropic_auth_extra(api_key)
    model = provider_cfg.get("model") or resolve_model(
        base, headers=auth_headers(provider_cfg), protocol=_ANTHROPIC_PROTOCOL)
    extra: dict[str, Any] = {}
    if provider_cfg.get("temperature") is not None:
        extra["temperature"] = float(provider_cfg["temperature"])
    if provider_cfg.get("top_p") is not None:
        extra["top_p"] = float(provider_cfg["top_p"])
    if provider_cfg.get("top_k") is not None:
        extra["top_k"] = int(provider_cfg["top_k"])
    user_extra = provider_cfg.get("extra_body")
    if user_extra is not None:
        if not isinstance(user_extra, dict):
            raise LLMError("providers.<job>.extra_body must be a JSON object")
        extra.update(user_extra)
    armed = json_schema is not None
    cap = _resolve_cap(provider_cfg, max_tokens, enforce_ceiling)

    call_id = uuid.uuid4().hex[:12]

    def _request_meta() -> dict[str, Any]:
        params: dict[str, Any] = {"max_tokens": extra.get("max_tokens", cap)}
        for key in ("temperature", "top_p", "top_k"):
            if key in extra:
                params[key] = extra[key]
        if isinstance(user_extra, dict) and user_extra:
            params["extra_body"] = user_extra
        return {
            "event": "llm_request",
            "call_id": call_id,
            "url": url,
            "model": model,
            "params": params,
            "guided_json": armed,
            "prompt": prompt,
        }

    def _response_meta(response: str | None = None, finish_reason: object = None,
                       usage: object = None, elapsed: float = 0.0,
                       error: str | None = None) -> dict[str, Any]:
        return {
            "event": "llm_response",
            "call_id": call_id,
            "url": url,
            "model": model,
            "response": response,
            "finish_reason": finish_reason,
            "usage": usage,
            "elapsed_s": round(elapsed, 2),
            "error": error,
        }

    if meta_hook:
        meta_hook(_request_meta())
    started = time.monotonic()
    failures = 0
    while True:
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": cap,
            }
            if extra:
                kwargs["extra_body"] = dict(extra)
            if armed:
                kwargs["output_config"] = {"format": {"type": "json_schema",
                                                      "schema": json_schema}}
            if no_auth:
                kwargs["extra_headers"] = no_auth
            message = client.messages.create(**kwargs)
        except anthropic.APIConnectionError as exc:
            failures += 1
            if failures >= _MAX_ATTEMPTS:
                err = f"request to {url} failed after {failures} attempts: {exc}"
                if meta_hook:
                    meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
                raise LLMError(err) from exc
            time.sleep(_BACKOFF[failures - 1])
            continue
        except anthropic.APIStatusError as exc:
            retryable_400 = False
            if exc.status_code == 400 and armed:
                if _GUIDED_UNSUPPORTED_RE.search(_error_text(exc)):
                    armed = False
                    if meta_hook:
                        meta_hook(_request_meta())
                    continue
                retryable_400 = True

            if retryable_400 or exc.status_code == 429 or exc.status_code >= 500:
                failures += 1
                if failures >= _MAX_ATTEMPTS:
                    err = (f"HTTP {exc.status_code} from {url} after {failures} "
                           f"attempts: {_error_text(exc)[:400]}")
                    if meta_hook:
                        meta_hook(_response_meta(elapsed=time.monotonic() - started,
                                                 error=err))
                    raise LLMError(err)
                time.sleep(_BACKOFF[failures - 1])
                continue

            err = f"HTTP {exc.status_code} from {url}: {_error_text(exc)[:400]}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err)

        except Exception as exc:
            err = f"request to {url} failed: {exc}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err) from exc

        try:
            blocks = getattr(message, "content", None)
            if isinstance(message, str) or blocks is None:
                err = f"unexpected response payload from {url}: {str(message)[:400]}"
                if meta_hook:
                    meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
                raise LLMError(err)
            content = "".join(
                block.text for block in blocks
                if getattr(block, "type", None) == "text"
                and isinstance(getattr(block, "text", None), str))
            content = _THINK_BLOCK_RE.sub("", content, count=1)
            if not content.strip():
                budgeted = any(getattr(block, "type", None) == "thinking" for block in blocks)
                hint = (" (the budget went to thinking: raise providers.<job>.extra_body."
                        "thinking.budget_tokens or max_tokens)") if budgeted else ""
                err = f"empty completion content from {url}{hint}"
                if meta_hook:
                    meta_hook(_response_meta(
                        response=None,
                        finish_reason=getattr(message, "stop_reason", None),
                        usage=_anthropic_meta_usage(message),
                        elapsed=time.monotonic() - started,
                        error=err,
                    ))
                raise LLMError(err)
        except (TypeError, KeyError, IndexError, AttributeError) as exc:
            err = f"unexpected response payload from {url}: {exc}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err) from exc

        if meta_hook:
            meta_hook(_response_meta(
                response=content,
                finish_reason=getattr(message, "stop_reason", None),
                usage=_anthropic_meta_usage(message),
                elapsed=time.monotonic() - started,
            ))
        return content.strip()


def _strip_fences(text: str) -> str:
    """Strip a single enclosing ``` / ```json fence, if present."""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _matching_span(s: str, start: int) -> int | None:
    """Index of the close bracket matching s[start] ('{' or '['), respecting
    string literals and escapes; None if unbalanced."""
    open_ch = s[start]
    close_ch = _PAIRS[open_ch]
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_json(text: str) -> Any:
    """Robustly pull JSON out of an LLM reply: strip ```json fences, then
    scan for the first '{' or '[' and take the balanced span (string- and
    escape-aware) for json.loads. Raises LLMError("no parseable JSON found
    in model response") on failure."""
    s = _strip_fences(text)
    pos = 0
    while pos < len(s):
        start = next((i for i in range(pos, len(s)) if s[i] in _PAIRS), None)
        if start is None:
            break
        end = _matching_span(s, start)
        if end is None:
            break
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError:
            pos = end + 1
    raise LLMError("no parseable JSON found in model response")
