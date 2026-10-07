"""Minimal OpenAI-compatible chat client (sglang) with retries and robust
JSON extraction from model replies."""

import json
import os
import re
import time
import uuid
from typing import Any

import requests
from typing import Callable
from urllib.parse import urlparse

# Plain package import (no flat-module fallback): the skill runs in package
# mode only -- translate.py puts scripts/ on sys.path before importing lib.
from lib import config

_MODELS_TIMEOUT = 30
_CHAT_TIMEOUT = 600
_BACKOFF = (2, 4, 8)
_MAX_ATTEMPTS = 4


class LLMError(Exception):
    """Connection, HTTP, or response-shape failure talking to the LLM server."""


# Model-resolution cache keyed on (normalized base URL, Authorization header
# value): two jobs may share a base URL with different API keys and see
# different model lists, so one job's resolution must not pin the other's.
# Keys stay in memory only and are never logged.
_MODEL_CACHE: dict[tuple[str, str | None], str] = {}
_PAIRS = {"{": "}", "[": "]"}
_FENCE_RE = re.compile(r"^```[\w+-]*[ \t]*\n?(.*?)\n?[ \t]*```$", re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
# A 400 counts as "guided JSON unsupported" only when the server's error text
# blames the mechanism -- wording varies across OpenAI-compatible servers
# ("response_format", "json_schema", "guided json", "x-guided"). Any other 400
# (bad model name, oversized context, malformed request) is a real failure,
# not a license to silently drop response_format.
_GUIDED_UNSUPPORTED_RE = re.compile(
    r"response_format|json_schema|guided json|x-guided", re.IGNORECASE)


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


def auth_headers(provider_cfg: dict) -> dict | None:
    """Authorization header for hosted providers: "api_key" directly, or
    "api_key_env" naming an environment variable (preferred - keeps keys
    out of config.json). None when the provider block carries neither."""
    api_key = provider_cfg.get("api_key")
    if not api_key and provider_cfg.get("api_key_env"):
        api_key = os.environ.get(str(provider_cfg["api_key_env"]))
    return {"Authorization": f"Bearer {api_key}"} if api_key else None


def resolve_model(base_url: str, headers: dict | None = None) -> str:
    """GET {base_url}/v1/models and return the first data[].id (cached per
    base_url + auth identity in a module dict). Raises LLMError on connection
    failure or an unexpected payload."""
    base = _v1_url(base_url)
    # Auth identity = the resolved Authorization value the models request
    # just used (None when anonymous), so per-key model lists stay distinct.
    auth = headers.get("Authorization") if headers else None
    key = (base, auth)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    url = base + "/models"
    try:
        resp = requests.get(url, headers=headers, timeout=_MODELS_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise LLMError(f"failed to list models at {url}: {exc}") from exc
    except ValueError as exc:
        raise LLMError(f"non-JSON payload from {url}: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise LLMError(f"unexpected payload from {url}: no 'data' list ({str(payload)[:200]})")
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            _MODEL_CACHE[key] = item["id"]
            return item["id"]
    raise LLMError(f"unexpected payload from {url}: no model id in 'data' ({str(payload)[:200]})")


def probe(provider_cfg: dict, timeout: int = 30) -> str:
    """One minimal chat completion with no retries - a connectivity + auth
    check for ping. Sends only model/messages/max_tokens so optional
    provider-specific body fields (chat_template_kwargs, response_format)
    can't skew the result. Returns choices[0].message.content (possibly
    empty - any 200 with choices proves routing + auth); raises LLMError on
    any failure. Requires an explicit model in the provider block."""
    model = provider_cfg.get("model")
    if not model:
        raise LLMError("probe needs an explicit model (set providers.<job>.model)")
    base_url = _v1_url(str(provider_cfg["base_url"]))
    url = base_url + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }
    try:
        resp = requests.post(url, json=body, headers=auth_headers(provider_cfg),
                             timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(f"probe request to {url} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise LLMError(f"probe HTTP {resp.status_code} from {url}: {resp.text[:200]}")
    try:
        return str(resp.json()["choices"][0]["message"].get("content") or "")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"probe got an unexpected payload from {url}: {resp.text[:200]}") from exc


def _resolve_cap(provider_cfg: dict, max_tokens: int | None,
                 enforce_ceiling: bool) -> int:
    """The output cap this request actually sends.

    A caller-supplied `max_tokens` is a CEILING, not an instruction: it may
    lower a block's own max_tokens but never raise it, because a provider's
    real limit is not negotiable. `enforce_ceiling=False` is the deliberate
    opt-out, used only by the consensus synthesis (consensus.chat), which
    must be able to exceed its own block's cap to merge full-size candidates
    -- consensus.chat computes max(task cap, block cap) on purpose.

    Provider `max_tokens` is NOT schema-validated (references/file-formats.md:
    provider sampling knobs are read per request and not validated), so it can
    arrive as null or a non-int. `or DEFAULT_MAX_TOKENS` is the repo-wide idiom
    (pipeline._provider_max, consensus.chat) and keeps null/0 meaning "unset",
    exactly as the previous `max_tokens or provider_cfg.get(...)` did.
    """
    block_max = int(provider_cfg.get("max_tokens") or config.DEFAULT_MAX_TOKENS)
    if not max_tokens:
        return block_max
    return min(int(max_tokens), block_max) if enforce_ceiling else int(max_tokens)


def chat(provider_cfg: dict, prompt: str, json_schema: dict | None = None,
         max_tokens: int | None = None,
         meta_hook: Callable[[dict], None] | None = None,
         enforce_ceiling: bool = True) -> str:
    """One chat completion against an OpenAI-compatible server; returns
    choices[0].message.content.strip().

    Auth for hosted providers: the provider block may carry "api_key"
    directly or "api_key_env" naming an environment variable (preferred -
    keeps keys out of config.json); either sends "Authorization: Bearer ...".
    The header is never included in trace-log metadata.

    Retries up to 4 attempts total (backoff 2s/4s/8s) on connection errors,
    HTTP >= 500, 429, and a 400 received while response_format is set whose
    error text does not blame guided JSON (a real failure -- bad model,
    oversized context -- must not silently drop response_format). A 400
    whose text names the mechanism (response_format, json_schema, "guided
    json", x-guided) triggers one immediate retry WITHOUT response_format
    (the server may not support guided JSON). Other 4xx raise LLMError with
    the status code and the first 400 chars of the response text.

    meta_hook, when given, is invoked with call metadata for trace logs:
    once with the request (url, model, params, full prompt) BEFORE the call,
    and once with the response (raw content, finish_reason, usage, elapsed,
    error) after it completes or exhausts retries. Both metas carry the same
    call_id and an "event" field ("llm_request" / "llm_response") so they
    pair up as two JSONL lines per call -- a call that hits the 400 fallback
    (retry without response_format) adds one extra "llm_request" line for
    the retried request.

    `max_tokens` is the caller's ceiling and is clamped DOWN to the block's
    own max_tokens (see _resolve_cap); it can never raise a block above its
    declared limit. `enforce_ceiling=False` disables that clamp and exists for
    the consensus synthesis only.
    """
    base_url = _v1_url(str(provider_cfg["base_url"]))
    url = base_url + "/chat/completions"
    # Optional auth for hosted providers; local sglang needs none.
    headers = auth_headers(provider_cfg)
    model = provider_cfg.get("model") or resolve_model(base_url, headers=headers)
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # Temperature comes from the provider block only -- every job bakes
        # its sampling profile into config defaults (translator 0.7, etc.).
        "temperature": provider_cfg.get("temperature", 0.2),
        "max_tokens": _resolve_cap(provider_cfg, max_tokens, enforce_ceiling),
    }
    # Optional sampling knobs: a key is sent only when the provider block
    # carries it with a non-None value (top_k may legitimately be -1,
    # meaning "disabled").
    if provider_cfg.get("top_p") is not None:
        body["top_p"] = float(provider_cfg["top_p"])
    if provider_cfg.get("top_k") is not None:
        body["top_k"] = int(provider_cfg["top_k"])
    if provider_cfg.get("repetition_penalty") is not None:
        body["repetition_penalty"] = float(provider_cfg["repetition_penalty"])
    # Hybrid-thinking models (sglang chat_template_kwargs): false spends the
    # output budget on the answer instead of a reasoning chain.
    if provider_cfg.get("thinking") is not None:
        body["chat_template_kwargs"] = {"enable_thinking": bool(provider_cfg["thinking"])}
    # Escape hatch for provider-specific parameters: merged verbatim into the
    # request body after the known knobs (so it can override them) and before
    # response_format (guided JSON stays pipeline-controlled). Not applied to
    # probe() - the ping probe deliberately sends a minimal body.
    extra = provider_cfg.get("extra_body")
    if extra is not None:
        if not isinstance(extra, dict):
            raise LLMError("providers.<job>.extra_body must be a JSON object")
        body.update(extra)
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": json_schema},
        }

    call_id = uuid.uuid4().hex[:12]

    def _request_meta() -> dict[str, Any]:
        params: dict[str, Any] = {k: body[k] for k in
                                  ("temperature", "max_tokens", "top_p", "top_k",
                                   "repetition_penalty", "chat_template_kwargs") if k in body}
        if isinstance(extra, dict) and extra:
            params["extra_body"] = extra
        return {
            "event": "llm_request",
            "call_id": call_id,
            "url": url,
            "model": model,
            "params": params,
            "guided_json": "response_format" in body,
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
    failures = 0  # retryable failures so far (network error, 5xx, 429)
    while True:
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=_CHAT_TIMEOUT)
        except requests.RequestException as exc:
            failures += 1
            if failures >= _MAX_ATTEMPTS:
                err = f"request to {url} failed after {failures} attempts: {exc}"
                if meta_hook:
                    meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
                raise LLMError(err) from exc
            time.sleep(_BACKOFF[failures - 1])
            continue

        retryable_400 = False
        if resp.status_code == 400 and "response_format" in body:
            # Only an error text blaming the mechanism means guided JSON is
            # unsupported: disarm it (pop) and retry once immediately. Any
            # other 400 keeps response_format and falls through to the shared
            # retryable-failure handling below, so a bad model name or an
            # oversized context consumes retry budget instead of silently
            # degrading to unguided decoding.
            if _GUIDED_UNSUPPORTED_RE.search(resp.text):
                body.pop("response_format")
                if meta_hook:  # log the retried request; it differs from the first
                    meta_hook(_request_meta())
                continue
            retryable_400 = True

        if retryable_400 or resp.status_code == 429 or resp.status_code >= 500:
            failures += 1
            if failures >= _MAX_ATTEMPTS:
                err = f"HTTP {resp.status_code} from {url} after {failures} attempts: {resp.text[:400]}"
                if meta_hook:
                    meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
                raise LLMError(err)
            time.sleep(_BACKOFF[failures - 1])
            continue

        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code} from {url}: {resp.text[:400]}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err)

        try:
            payload = resp.json()
        except ValueError as exc:
            err = f"non-JSON response from {url}: {resp.text[:400]}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err) from exc
        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            err = f"unexpected response payload from {url}: {str(payload)[:400]}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err) from exc
        message = choice.get("message") if isinstance(choice, dict) else None
        # A reasoning model that exhausts its budget returns finish_reason
        # "length" with `content` ABSENT rather than empty -- reasoning_content
        # is the only populated field. Read it with .get() so that shape falls
        # through to the empty-content diagnostic below, which names the real
        # cause, instead of being misreported as a malformed payload.
        content = message.get("content") if isinstance(message, dict) else None
        # Servers without a reasoning parser may inline a leading <think>
        # block into content; strip it before the empty-content check.
        if isinstance(content, str):
            content = _THINK_BLOCK_RE.sub("", content, count=1)
        if not isinstance(content, str) or not content.strip():
            reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
            hint = (" (reasoning_content present - the budget went to thinking: lower "
                    "providers.<job>.extra_body.reasoning_effort for a hosted reasoning "
                    "model, or set providers.<job>.thinking=false for a local sglang "
                    "server)") if reasoning else ""
            err = f"empty completion content from {url}{hint}"
            if meta_hook:
                meta_hook(_response_meta(
                    response=content if isinstance(content, str) else None,
                    finish_reason=choice.get("finish_reason"),
                    usage=payload.get("usage"),
                    elapsed=time.monotonic() - started,
                    error=err,
                ))
            raise LLMError(err)

        if meta_hook:
            meta_hook(_response_meta(
                response=content,
                finish_reason=choice.get("finish_reason"),
                usage=payload.get("usage"),
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
