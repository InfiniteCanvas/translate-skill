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

from lib import config
from lib.errors import PipelineError as _PipelineError

_MODELS_TIMEOUT = 30
_CHAT_TIMEOUT = 600
_BACKOFF = (2, 4, 8)
_MAX_ATTEMPTS = 4


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


_MODEL_CACHE: dict[tuple[str, str | None], str] = {}
_PAIRS = {"{": "}", "[": "]"}
_FENCE_RE = re.compile(r"^```[\w+-]*[ \t]*\n?(.*?)\n?[ \t]*```$", re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_GUIDED_UNSUPPORTED_RE = re.compile(
    r"response_format|json_schema|guided json|x-guided", re.IGNORECASE)

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


def _fatal_code(resp) -> str | None:
    """The provider's business error code from an error response body, or None.

    Tolerates every shape that can show up: a non-JSON body, a JSON body that
    is not an object, a missing/None `error`, an `error` that is not an object,
    and an `int` code where the vendor documented a string. Returns a string in
    every case so the comparison against ZAI_FATAL_CODES cannot miss on type.

    Never raises. A malformed body is not itself a reason to lose the real
    HTTP status, which the caller still needs for the ordinary ladder.
    """
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if code is None or isinstance(code, bool):
        return None
    return str(code).strip() or None


def _code_is_fatal(resp) -> str | None:
    """The code from `resp` when it is one retrying cannot fix, else None."""
    code = _fatal_code(resp)
    return code if code in ZAI_FATAL_CODES else None


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
        fatal = _code_is_fatal(resp)
        detail = f"provider code {fatal} (irrecoverable)" if fatal else ""
        raise LLMError(f"probe HTTP {resp.status_code} from {url}"
                       f"{': ' + detail if detail else ''}: {resp.text[:200]}")
    try:
        return str(resp.json()["choices"][0]["message"].get("content") or "")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"probe got an unexpected payload from {url}: {resp.text[:200]}") from exc


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
    own cap (see _resolve_cap); it can never raise a block above its declared
    limit. `enforce_ceiling=False` disables that clamp and exists for the
    consensus synthesis only -- and never disables a block's `max_tokens_limit`,
    which is a provider limit rather than a budget.
    """
    base_url = _v1_url(str(provider_cfg["base_url"]))
    url = base_url + "/chat/completions"
    headers = auth_headers(provider_cfg)
    model = provider_cfg.get("model") or resolve_model(base_url, headers=headers)
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": provider_cfg.get("temperature", 0.2),
        "max_tokens": _resolve_cap(provider_cfg, max_tokens, enforce_ceiling),
    }
    if provider_cfg.get("top_p") is not None:
        body["top_p"] = float(provider_cfg["top_p"])
    if provider_cfg.get("top_k") is not None:
        body["top_k"] = int(provider_cfg["top_k"])
    if provider_cfg.get("repetition_penalty") is not None:
        body["repetition_penalty"] = float(provider_cfg["repetition_penalty"])
    if provider_cfg.get("thinking") is not None:
        body["chat_template_kwargs"] = {"enable_thinking": bool(provider_cfg["thinking"])}
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
    failures = 0
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

        fatal = _code_is_fatal(resp)
        if fatal is not None:
            err = (f"HTTP {resp.status_code} from {url}: provider code {fatal} "
                   f"(irrecoverable - retrying cannot help): {resp.text[:400]}")
            if meta_hook:
                meta_hook(_response_meta(elapsed=time.monotonic() - started,
                                         error=err))
            raise LLMFatal(err)

        retryable_400 = False
        if resp.status_code == 400 and "response_format" in body:
            if _GUIDED_UNSUPPORTED_RE.search(resp.text):
                body.pop("response_format")
                if meta_hook:
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
        content = message.get("content") if isinstance(message, dict) else None
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
