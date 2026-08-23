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

_MODELS_TIMEOUT = 30
_CHAT_TIMEOUT = 600
_BACKOFF = (2, 4, 8)
_MAX_ATTEMPTS = 4


class LLMError(Exception):
    """Connection, HTTP, or response-shape failure talking to the LLM server."""


_MODEL_CACHE: dict[str, str] = {}
_PAIRS = {"{": "}", "[": "]"}
_FENCE_RE = re.compile(r"^```[\w+-]*[ \t]*\n?(.*?)\n?[ \t]*```$", re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def _v1_url(base_url: str) -> str:
    """Normalize a base URL to end with /v1."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def resolve_model(base_url: str) -> str:
    """GET {base_url}/v1/models and return the first data[].id (cached per
    base_url in a module dict). Raises LLMError on connection failure or an
    unexpected payload."""
    base = _v1_url(base_url)
    if base in _MODEL_CACHE:
        return _MODEL_CACHE[base]
    url = base + "/models"
    try:
        resp = requests.get(url, timeout=_MODELS_TIMEOUT)
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
            _MODEL_CACHE[base] = item["id"]
            return item["id"]
    raise LLMError(f"unexpected payload from {url}: no model id in 'data' ({str(payload)[:200]})")


def chat(provider_cfg: dict, prompt: str, json_schema: dict | None = None,
         temperature: float | None = None, max_tokens: int | None = None,
         meta_hook: Callable[[dict], None] | None = None) -> str:
    """One chat completion against an OpenAI-compatible server; returns
    choices[0].message.content.strip().

    Auth for hosted providers: the provider block may carry "api_key"
    directly or "api_key_env" naming an environment variable (preferred -
    keeps keys out of config.json); either sends "Authorization: Bearer ...".
    The header is never included in trace-log metadata.

    Retries up to 4 attempts total (backoff 2s/4s/8s) on connection errors,
    HTTP >= 500, and 429. A 400 while response_format is set triggers one
    immediate retry WITHOUT response_format (the server may not support
    guided JSON). Other 4xx raise LLMError with the status code and the
    first 400 chars of the response text.

    meta_hook, when given, is invoked with call metadata for trace logs:
    once with the request (url, model, params, full prompt) BEFORE the call,
    and once with the response (raw content, finish_reason, usage, elapsed,
    error) after it completes or exhausts retries. Both metas carry the same
    call_id and an "event" field ("llm_request" / "llm_response") so they
    pair up as two JSONL lines per call.
    """
    base_url = _v1_url(str(provider_cfg["base_url"]))
    url = base_url + "/chat/completions"
    model = provider_cfg.get("model") or resolve_model(base_url)
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature if temperature is not None else provider_cfg.get("temperature", 0.2),
        "max_tokens": max_tokens or provider_cfg.get("max_tokens", 16384),
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
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": json_schema},
        }

    # Optional auth for hosted providers; local sglang needs neither.
    api_key = provider_cfg.get("api_key")
    if not api_key and provider_cfg.get("api_key_env"):
        api_key = os.environ.get(str(provider_cfg["api_key_env"]))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None

    call_id = uuid.uuid4().hex[:12]

    def _request_meta() -> dict[str, Any]:
        return {
            "event": "llm_request",
            "call_id": call_id,
            "url": url,
            "model": model,
            "params": {k: body[k] for k in
                       ("temperature", "max_tokens", "top_p", "top_k",
                        "repetition_penalty", "chat_template_kwargs") if k in body},
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

        if resp.status_code == 400 and "response_format" in body:
            body.pop("response_format")
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
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
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            err = f"unexpected response payload from {url}: {str(payload)[:400]}"
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started, error=err))
            raise LLMError(err) from exc
        # Servers without a reasoning parser may inline a leading <think>
        # block into content; strip it before the empty-content check.
        if isinstance(content, str):
            content = _THINK_BLOCK_RE.sub("", content, count=1)
        if not isinstance(content, str) or not content.strip():
            message = choice["message"]
            reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
            hint = (" (reasoning_content present - set providers.<job>.thinking=false so the "
                    "output budget goes to the answer)") if reasoning else ""
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
