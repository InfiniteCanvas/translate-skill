# config.local.json examples

Two ready-to-copy overlays for `novel-translator/config.local.json` — one
per hosted provider family. Each is validated against the real loader, so it
loads, normalizes, and lands on every job correctly.

| File | Provider | Endpoint | Auth env var |
|---|---|---|---|
| `config.local.example.zai.json` | Z.AI (GLM) | `https://api.z.ai/api/coding/paas/v4` | `ZAI_API_KEY` |
| `config.local.example.minimax.json` | MiniMax (M3) | `https://api.minimax.io/v1` | `MINIMAX_API_KEY` |
| `config.local.example.mixed.json` | both — see below | both | both |

The **mixed** file is the one to reach for when you hold both plans: it
routes each job to the model that is actually good at it. Both single-provider
files remain for when only one plan is available.

All three were verified end-to-end against the live APIs: every provider job
pings reachable, and a full chapter translates cleanly.

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

## Thinking is ON, and why the budget is 64k

Both examples leave thinking **enabled** — the sglang-only `thinking` field is
not set to false, and neither is `extra_body.thinking`. Reasoning is worth
having for consistency-critical translation work; the problem is only that
reasoning and answer share one `max_tokens` ceiling.

Measured live on the actual APIs with a real chapter passage, thinking ON:

| Call | `max_tokens` | completion | reasoning | usable content |
|---|---|---|---|---|
| Z.AI glm-5.3, plain translate | 8,192 | 906 | 774 | 581 ch ✅ |
| Z.AI glm-5.3, plain translate | 65,536 | 843 | 718 | 554 ch ✅ |
| Z.AI glm-5.3, translate + cultural notes | 65,536 | 9,385 | 7,880 | 6,101 ch ✅ |

So reasoning costs roughly **700–800 tokens** on a normal chapter and about
**7.9k** on a heavy translator's-notes prompt — the second being the case
that overruns the stock 8,192 cap and truncates. `65536` is accepted by both
APIs and leaves wide headroom for GLM's tendency to overthink.

Two consequences worth knowing:

- `translate_max_output_tokens` must match. It is the per-call cap the
  TRANSLATE stage actually sends, and it also drives chapter packing
  (`floor(0.8 × cap) − 256`), so a bigger cap means longer chapters pack into
  fewer parts and fewer calls. Setting provider `max_tokens` alone would NOT
  raise it — the pipeline would still send 8,192.
- A runaway think now has 64k to burn instead of failing fast at 8k. That is
  the deliberate trade: a truncated translation is worse than a slow one.
  `max_attempts` bounds retries.

MiniMax inlines its reasoning as `<think>…</think>` inside `content` when
thinking is on. `lib/client.py` strips a leading think block before the
empty-content check, so this never reaches the translated chapter — verified
by translating a chapter end-to-end with the output checked for tags.

## Provider quirks these files encode

**Z.AI Coding Plan keys only work on the coding endpoint.** Use
`https://api.z.ai/api/coding/paas/v4`. The general endpoint
(`https://api.z.ai/api/paas/v4`) is for pay-per-token developer keys; a
Coding Plan key sent there is rejected. The two are not interchangeable.

**MiniMax accepts `max_tokens` as well as `max_completion_tokens`.** Their
docs name `max_completion_tokens` for Chat Completions, but the live API
accepts either and also tolerates both being present (which is what this
skill sends, since the client always sets `max_tokens`). The MiniMax example
carries `max_completion_tokens` in `extra_body` to match the docs; keep it in
sync with the top-level `max_tokens` if you change the budget. MiniMax
ignores `presence_penalty` and `frequency_penalty`, which is why neither
appears here.

## Known caveat: the sglang-only `thinking` field is still sent

The provider-block `thinking` field (distinct from any `extra_body` entry)
maps to sglang's `chat_template_kwargs.enable_thinking`, which neither hosted
API reads. It cannot currently be suppressed: `PROVIDER_DEFAULTS` sets it for
every job and `client.chat` sends it whenever the key is non-`null`, so
every request carries `chat_template_kwargs` regardless of what the overlay
says.

This is harmless — both APIs ignore unknown body fields, verified live on the
exact request bodies these examples produce — and it is also inert for the
reasoning these models do perform, which is controlled server-side by their
own parameters. Setting the provider-block `thinking: false` remains
meaningful for a **local sglang server**, where it is what stops reasoning
from eating the budget.

## Routing a two-plan setup

The two subscription plans meter **differently**, so they should not be used
the same way:

- **Z.AI** bills **credits derived from tokens**, and the model multiplier is
  steep: `glm-5.3` costs **1x off-peak / 3x peak**, while `glm-5.3-flash`
  costs **0.4x / 1.2x** — roughly a quarter of the flagship. Off-peak (outside
  Mon–Fri 14:00–18:00 UTC+8, weekends included) bills at half.
- **MiniMax** bills a flat **monthly token pool** (~1.7B tokens/month on the
  Go tier), independent of which model you name.

So: spend Z.AI credits on judgment, spend MiniMax's pool on volume.

| Job | Model | Why |
|---|---|---|
| `translator` | `glm-5.3` **+** `MiniMax-M3.1-Flash-Preview` | two-model array — the consensus fan-out; each candidate reaches a different plan |
| `annotator` | `glm-5.3-flash` **+** `MiniMax-M3.1-Flash-Preview` | same idea for translation notes, where two cheap models agreeing beats one |
| `reviewer` | `glm-5.3` | the faithfulness gate; a false rejection costs more than a cheap review |
| `consensus` | `glm-5.3` | synthesizes every fan-out, so it gets the strongest |
| `glossary` / `recap` / `profile` | `glm-5.3-flash` | structured extraction and summaries; a cheap model genuinely suffices |

Fan-out is **per job, not translator-only**: any job whose array carries two
or more blocks runs both models in parallel and merges them through the
`consensus` provider (`consensus.py` `chat()` is what every job call routes
through). So each fan-out job costs 3 calls — 2 candidates plus 1 synthesis —
and the synthesis always runs on the `consensus` block, whatever that is set
to. Both fan-outs run in parallel across blocks, so wall-clock stays at the
slowest model rather than the sum, but credit/token spend is multiplied.

Keep `consensus` on the flagship if translator quality is the priority: it is
the synthesizer for *every* fan-out job, so demoting it to save credits would
degrade the translator too. If credits are the binding constraint instead, the
lever with the best ratio is dropping a fan-out job back to a single block —
it removes a candidate *and* its synthesis call.

Batch big runs outside **Mon–Fri 14:00–18:00 UTC+8** and Z.AI bills at half
— a free 2x on the credit-heavy jobs.

### `MiniMax-M3.1-Flash-Preview` vs `MiniMax-M3`

M3.1-Flash-Preview is the better pick here, with two caveats worth knowing:

- It is **not listed by `GET /v1/models`** (which still advertises only
  `MiniMax-M3`, `M2.7`, `M2.5`, `M2.1`, `M2`) yet the API accepts it. It is
  unpublished, so pin it deliberately rather than trusting auto-resolution —
  a model this new can be renamed or retired without notice.
- It reasons **~25% less** (140 vs 189 tokens on the same prompt) and, unlike
  M3, returns **no `<think>` block in `content` at all** — M3 inlined ~968
  characters of visible reasoning into the payload. The client strips a
  leading think block, so either is safe for the pipeline, but keeping the
  reasoning at the source is better.

Note that `translate_max_output_tokens` and each block's `max_tokens` must
stay consistent, and with a two-block translator array the SMALLEST block
`max_tokens` governs packing — here both are 65536, so it is a non-issue.

## Mixing the two providers

Nothing prevents mixing them on any job — point one job at each:

```json
{
  "providers": {
    "translator": { "base_url": "https://api.z.ai/api/coding/paas/v4", "model": "glm-5.3",
                    "api_key_env": "ZAI_API_KEY", "max_tokens": 65536 },
    "annotator":  { "base_url": "https://api.minimax.io/v1",
                    "model": "MiniMax-M3.1-Flash-Preview",
                    "api_key_env": "MINIMAX_API_KEY", "max_tokens": 65536,
                    "extra_body": { "max_completion_tokens": 65536 } }
  },
  "translate_max_output_tokens": 65536
}
```

Jobs you omit inherit the translator's block, so name every job you want
pointed somewhere specific. `config.local.example.mixed.json` is this pattern
written out for all seven jobs.

## Related

`references/file-formats.md` documents the overlay merge rules and the full
`config.json` schema; `SKILL.md` covers when to run `sync-config`.