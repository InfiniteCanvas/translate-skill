"""Minimal OpenAI-compatible chat client (sglang) with retries and robust
JSON extraction from model replies."""

import json
import re
import time
from typing import Any

import requests

_MODELS_TIMEOUT = 30
_CHAT_TIMEOUT = 600
_BACKOFF = (2, 4, 8)
_MAX_ATTEMPTS = 4


class LLMError(Exception):
    """Connection, HTTP, or response-shape failure talking to the LLM server."""


_MODEL_CACHE: dict[str, str] = {}
_PAIRS = {"{": "}", "[": "]"}
_FENCE_RE = re.compile(r"^```[\w+-]*[ \t]*\n?(.*?)\n?[ \t]*```$", re.DOTALL)


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
         temperature: float | None = None, max_tokens: int | None = None) -> str:
    """One chat completion against an OpenAI-compatible server; returns
    choices[0].message.content.strip().

    Retries up to 4 attempts total (backoff 2s/4s/8s) on connection errors,
    HTTP >= 500, and 429. A 400 while response_format is set triggers one
    immediate retry WITHOUT response_format (the server may not support
    guided JSON). Other 4xx raise LLMError with the status code and the
    first 400 chars of the response text.
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
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": json_schema},
        }

    failures = 0  # retryable failures so far (network error, 5xx, 429)
    while True:
        try:
            resp = requests.post(url, json=body, timeout=_CHAT_TIMEOUT)
        except requests.RequestException as exc:
            failures += 1
            if failures >= _MAX_ATTEMPTS:
                raise LLMError(f"request to {url} failed after {failures} attempts: {exc}") from exc
            time.sleep(_BACKOFF[failures - 1])
            continue

        if resp.status_code == 400 and "response_format" in body:
            body.pop("response_format")
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            failures += 1
            if failures >= _MAX_ATTEMPTS:
                raise LLMError(
                    f"HTTP {resp.status_code} from {url} after {failures} attempts: {resp.text[:400]}"
                )
            time.sleep(_BACKOFF[failures - 1])
            continue

        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code} from {url}: {resp.text[:400]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise LLMError(f"non-JSON response from {url}: {resp.text[:400]}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response payload from {url}: {str(payload)[:400]}") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"empty completion content from {url}")
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
