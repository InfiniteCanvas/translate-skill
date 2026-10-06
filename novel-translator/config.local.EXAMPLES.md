# config.local.json examples

Two ready-to-copy overlays for `novel-translator/config.local.json` — one
per hosted provider family. Each is validated against the real loader, so it
loads, normalizes, and lands on every job correctly.

| File | Provider | Endpoint | Auth env var |
|---|---|---|---|
| `config.local.example.zai.json` | Z.AI (GLM) | `https://api.z.ai/api/coding/paas/v4` | `ZAI_API_KEY` |
| `config.local.example.minimax.json` | MiniMax (M3) | `https://api.minimax.io/v1` | `MINIMAX_API_KEY` |

Both files were verified end-to-end against the live APIs: all seven provider
jobs ping reachable, and a full chapter translates with no reasoning-tag
leakage.

## Use

```powershell
Copy-Item novel-translator\config.local.example.minimax.json `
          novel-translator\config.local.json
$env:MINIMAX_API_KEY = "sk-your-key"
uv run novel-translator\scripts\translate.py ping --project .
```

`config.local.json` is gitignored, so the copy is safe. `ping` is the
cheapest confirmation that the endpoint, model and key all resolve. New
projects pick it up at `init`; existing ones take it via `sync-config`.

## Every provider-block field

| Field | Type | Sent as | Notes |
|---|---|---|---|
| `base_url` | string | – | Bare origin gets `/v1` appended; a base with a real path (`https://api.z.ai/api/paas/v4`) is used verbatim. |
| `model` | string | `model` | Omit to auto-resolve from `GET {base_url}/models` (first id). |
| `api_key` | string | `Authorization: Bearer` | **Inline secret.** Merged into the project `config.json`, which the project repo commits — prefer `api_key_env`. |
| `api_key_env` | string | `Authorization: Bearer` | Name of an env var holding the key. Preferred; the key never touches a file. If unset, the request goes out with no auth header and no warning. |
| `temperature` | number | `temperature` | Always sent (defaults to the job's value). |
| `top_p` | number | `top_p` | Sent only when non-`null`. |
| `top_k` | int | `top_k` | Sent only when non-`null`. `-1` legitimately means "disabled". |
| `max_tokens` | int | `max_tokens` | Always sent. |
| `repetition_penalty` | number | `repetition_penalty` | Sent only when non-`null`. |
| `thinking` | bool | `chat_template_kwargs.enable_thinking` | **sglang-specific.** See the caveat below. |
| `extra_body` | object | merged verbatim | Escape hatch for provider-specific parameters. Merged after the known knobs, so it can override them. |

Top-level keys used here: `providers` (the map above) plus
`translate_max_output_tokens` (per-chapter cap the pipeline passes to the
translator call) and `max_attempts` (retries per LLM call).

## Three provider quirks these files encode

**`thinking: {"type": "disabled"}` is not optional — without it every call
returns empty content.** Both GLM-5.3 and MiniMax-M3 spend the output budget
on reasoning first. Measured against the live APIs with `max_tokens: 50`,
GLM-5.3 returned `finish_reason: length`, `content: ""`, and
`usage.completion_tokens_details.reasoning_tokens: 48` — the entire budget
gone to thinking. That is the exact failure `lib/client.py` guards against
(it raises "empty completion content ... set thinking=false"). Adding the
`extra_body` entry returns clean content. MiniMax-M3 has the same default,
and inlines its reasoning as `<think>...</think>` **inside `content`** when
left on, which would otherwise be written straight into the translated
chapter.

**Z.AI Coding Plan keys only work on the coding endpoint.** Use
`https://api.z.ai/api/coding/paas/v4`. The general endpoint
(`https://api.z.ai/api/paas/v4`) is for pay-per-token developer keys; a
Coding Plan key sent there is rejected. The two are not interchangeable.

**MiniMax accepts `max_tokens` as well as `max_completion_tokens`.** Their
docs name `max_completion_tokens` for Chat Completions, but the live API
accepts either and also tolerates both being present (which is what this
skill sends, since the client always sets `max_tokens`). Keep the two
`extra_body` and top-level values in sync if you change `max_tokens`.
MiniMax ignores `presence_penalty` and `frequency_penalty`, which is why
neither appears here.

## Known caveat: the sglang-only `thinking` key is also sent

`thinking` (the provider-block field, not the `extra_body` one) maps to
sglang's `chat_template_kwargs.enable_thinking`, which neither hosted API
reads. It cannot currently be suppressed: `PROVIDER_DEFAULTS` sets it for
every job and `client.chat` sends it whenever the key is non-`null`, so
every request to a hosted provider carries
`chat_template_kwargs: {"enable_thinking": false}` regardless.

Both APIs ignore unknown body fields — verified live on the exact request
bodies these examples produce — so it is harmless in practice. If a provider
ever rejects it with a strict 400, the fix is to make `thinking` opt-in in
`_with_defaults` instead of defaulted.

Setting the provider-block `thinking: false` remains meaningful for a **local
sglang server**, where it is what stops reasoning from eating the budget.

## Mixing the two providers

Nothing prevents mixing them — point one job at each:

```json
{
  "providers": {
    "translator": { "base_url": "https://api.z.ai/api/coding/paas/v4", "model": "glm-5.3",
                    "api_key_env": "ZAI_API_KEY",
                    "extra_body": { "thinking": { "type": "disabled" } } },
    "annotator":  { "base_url": "https://api.minimax.io/v1", "model": "MiniMax-M3",
                    "api_key_env": "MINIMAX_API_KEY",
                    "extra_body": { "thinking": { "type": "disabled" } } }
  }
}
```

Jobs you omit inherit the translator's block, so name every job you want
pointed somewhere specific.

## Related

`references/file-formats.md` documents the overlay merge rules and the full
`config.json` schema; `SKILL.md` covers when to run `sync-config`.