# config.local.json examples

Two ready-to-copy overlays for `novel-translator/config.local.json` — one
per hosted provider family. Each is validated against the real loader, so it
loads, normalizes, and lands on every job correctly.

| File | Provider | Endpoint | Auth env var |
|---|---|---|---|
| `config.local.example.zai.json` | Z.AI (GLM) | `https://api.z.ai/api/paas/v4` | `ZAI_API_KEY` |
| `config.local.example.minimax.json` | MiniMax (M3) | `https://api.minimax.io/v1` | `MINIMAX_TOKEN` |

## Use

```powershell
Copy-Item novel-translator\config.local.example.minimax.json `
          novel-translator\config.local.json
$env:MINIMAX_TOKEN = "sk-your-key"
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

## Two provider quirks these files encode

**MiniMax counts output with `max_completion_tokens`.** Their Chat Completions
API wants that field rather than `max_tokens`, so the examples carry
`"max_completion_tokens"` inside `extra_body`. The client always sends
`max_tokens` as well, so both appear on the wire; the pair is kept in sync by
hand if you change `max_tokens`. MiniMax also ignores `presence_penalty` and
`frequency_penalty`, which is why neither appears here. `reasoning_effort`
thinks over reasoning tokens.

**Z.AI disables thinking with `thinking: {"type": "disabled"}`.** Thinking is
on by default for reasoning-capable GLM models; turning it off keeps the
output budget on the translation itself.

## Known caveat: `thinking` cannot currently be switched off per provider

`thinking` maps to sglang's `chat_template_kwargs.enable_thinking`, which
Z.AI and MiniMax do not read — they use their own top-level parameters, so
the correct move is to leave `thinking` out of the block and let `extra_body`
carry the real control (as these examples do).

There is currently **no way to do that**: `PROVIDER_DEFAULTS` sets
`thinking: false` for every job, and `client.chat` sends
`chat_template_kwargs` whenever the key is non-`null`. So every request to a
hosted provider — including these examples — carries
`chat_template_kwargs: {"enable_thinking": false}` whether or not the block
asks for it. Z.AI and MiniMax generally ignore unknown body fields, so this
is harmless in practice; if you hit a strict-400 rejection mentioning
`chat_template_kwargs`, that is the cause, and removing the key from
`_with_defaults`/`client.chat` is the real fix (a one-line change to make
`thinking` opt-in rather than defaulted).

Set `thinking: false` explicitly only against a local sglang server, where it
is meaningful and recommended.

## Related

`references/file-formats.md` documents the overlay merge rules and the full
`config.json` schema; `SKILL.md` covers when to run `sync-config`.