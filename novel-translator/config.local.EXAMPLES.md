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
| `thinking` | bool | `chat_template_kwargs.enable_thinking` | **sglang-specific — ignored by hosted APIs.** See "Reasoning budget" below; hosted reasoning is bounded with `extra_body`. |
| `extra_body` | object | merged verbatim | Escape hatch for provider-specific parameters. Merged after the known knobs, so it can override them. |

Top-level keys used here: `providers` (the map above) plus
`translate_max_output_tokens` (the output CEILING the pipeline asks the
translator call for; each block is then clamped to its own `max_tokens`, so
where a block is lower the block wins) and `max_attempts` (retries per LLM
call).

## Thinking, and how big the budget needs to be

Reasoning is worth having for consistency-critical translation work; the
problem is that reasoning and answer share one `max_tokens` ceiling. How you
keep the ceiling from being consumed depends on the model:

- **Z.AI `glm-5.3`** — leave thinking on. It reasons ~700–800 tokens on a
  normal chapter and ~7.9k on a heavy translator's-notes prompt, so it
  converges well inside any of these caps.
- **`MiniMax-M3`** — thinking **must** be disabled (`extra_body.thinking.type`).
  It has no depth knob and would otherwise spend the entire cap thinking.
- **`MiniMax-M3.1-Flash-Preview`** — thinking **cannot** be disabled (HTTP
  400); depth is bounded with `extra_body.reasoning_effort` instead.

Both MiniMax blocks in the mixed file set `reasoning_effort`; the MiniMax-only
file sets `extra_body.thinking.type: "disabled"`. The sglang-only `thinking`
field is never what turns reasoning off on a hosted model. Full mechanics,
measured tables, and the three HTTP 400 constraints are in **Reasoning budget**
below — read that before changing a cap or copying a provider block.

Measured live on the actual APIs with a real chapter passage, thinking ON:

| Call | `max_tokens` | completion | reasoning | usable content |
|---|---|---|---|---|
| Z.AI glm-5.3, plain translate | 8,192 | 906 | 774 | 581 ch ✅ |
| Z.AI glm-5.3, plain translate | 65,536 | 843 | 718 | 554 ch ✅ |
| Z.AI glm-5.3, translate + cultural notes | 65,536 | 9,385 | 7,880 | 6,101 ch ✅ |

So GLM's reasoning costs roughly **700–800 tokens** on a normal chapter and
about **7.9k** on a heavy translator's-notes prompt — the second being the case
that overruns the stock 8,192 cap and truncates.

Two consequences worth knowing:

- `translate_max_output_tokens` is a **CEILING**, not an override (since v012).
  Each translator block's `max_tokens` is a hard provider limit the pipeline may
  lower but never raise, so the value SENT to a block is
  `min(ceiling, that block's max_tokens)` — computed per block. Setting provider
  `max_tokens` alone therefore does not raise the sent cap, but it does LOWER it,
  and where it is lower than the ceiling the block wins and chapters pack to
  `min(ceiling, smallest block)`. The mixed file sets all three
  (`max_tokens`, `max_completion_tokens`, `translate_max_output_tokens`) to
  256000 so the `max`-depth reasoning has room to converge; the others stay at
  64k, which is ample for GLM and for the smaller jobs. A block below 8192 cannot
  be packed into at all.
- More budget delays truncation but does not prevent exhaustion. For GLM that
  is the right trade — bounded reasoning, so a runaway still converges. For
  MiniMax an unbounded think can consume any budget and return no translation
  at all, so the cap has to be large enough for the chosen
  `reasoning_effort`; 65,536 is not enough for `max`, 256,000 is.
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
sync with the block's own `max_tokens` if you change the budget.

**`extra_body` is outside the ceiling clamp.** The client clamps `max_tokens`
down to the block's declared limit, but `extra_body` merges *after* that and
wins — so a stale `max_completion_tokens` can push the server-side budget back
above what the ceiling says. When the two disagree, the request carries both
keys and MiniMax honors whichever its docs name for the endpoint. Keep them in
sync by hand; nothing enforces it. MiniMax
ignores `presence_penalty` and `frequency_penalty`, which is why neither
appears here.

## Reasoning budget: the one setting that actually matters

**The provider-block `thinking` field does NOT control hosted-model
reasoning.** It maps to sglang's `chat_template_kwargs.enable_thinking`,
which both hosted APIs ignore. `PROVIDER_DEFAULTS` sets it for every job and
`client.chat` sends it whenever the key is non-`null`, so every request
carries `chat_template_kwargs` regardless of the overlay. That is harmless —
both APIs ignore unknown body fields — but it is inert for hosted reasoning,
which is controlled by different parameters. The provider-block `thinking:
false` remains meaningful only for a **local sglang server**.

Do not trust a smoke test here: a trivial prompt appears to show
`chat_template_kwargs` working (reasoning drops to zero), while a real chapter
prompt reasons at full depth regardless. That discrepancy hid a 20-minute
per-call failure before it was caught.

### `MiniMax-M3.1-Flash-Preview`: bound depth with `reasoning_effort`

Thinking **cannot be switched off** on this model. All three attempts are
hard-rejected with HTTP 400: `thinking: {"type": "disabled"}` (*"requires
adaptive thinking"*), `reasoning_effort: "none"`, and `reasoning_split: false`
(*"requires reasoning_split=true"*). `reasoning_split` is an output-format
switch anyway — it does not enable or disable thinking.

The knob is `reasoning_effort` (`low` | `medium` | `high` | `xhigh` | `max`),
and **its default is `max`**. Omit it and every call silently runs at maximum
depth. Measured live on a real 66-line chapter translation with the
translator's JSON schema, `temperature` 1.0:

**At a 65,536 cap:**

| `reasoning_effort` | reasoning tokens | wall | result |
|---|---|---|---|
| `low` | 35 | 26s | 66 lines ✅ |
| `medium` | 910 | 36s | 66 lines ✅ |
| `high` | 1,855 | 51s | 66 lines ✅ |
| `xhigh` | 9,047 (4,503–9,047 across runs) | 73–143s | 66 lines ✅ |
| omitted (= `max`) | 65,536, still planning | 19m51s | **nothing** ❌ |

**At a 256,000 cap:**

| `reasoning_effort` | reasoning chars | wall | result |
|---|---|---|---|
| **`max`** | 239,573 / 94,736 across runs | 260–578s | 66 lines ✅ |
| `xhigh` | (unchanged — fits in 64k) | 73–143s | 66 lines ✅ |

The lesson is about **cap, not effort**. `max` is not more thorough, it is
*longer* — and 65,536 tokens is not long enough to hold it, so the run dies
still in the planning stage having emitted no `{`. Give `max` a cap that can
contain its reasoning and it converges every time measured, producing the
highest-quality output of any setting: terminology table up front, per-line
drafting, per-line self-review, JSON-safety validation. `xhigh` produces the
same shape of work at roughly a fifth of the reasoning. Neither `response_format`
nor `max_completion_tokens` changes any of this.

Because `temperature` is 1.0, reasoning volume varies run to run — `xhigh`
landed anywhere from 4,503 to 9,047 tokens and `max` from 94,736 to 239,573
chars. Both completed every time at these caps; the earlier failures were
cap-bound, not effort-bound.

**The mixed file therefore runs the translator at `max` with a 256,000 cap**
(`max_tokens`, `max_completion_tokens`, and `translate_max_output_tokens` all
256000), which is the best measured output quality. The smaller jobs keep
`xhigh` at 64k — they reason over far less text and stay well clear of the
cliff at any setting. Drop the translator to `xhigh` if you would rather trade
some quality for a ~74s call instead of ~5–10min.

Two consequences of raising the translator cap:

- `translate_max_output_tokens` is the CEILING TRANSLATE asks for, and every
  block must be at or above it for that ceiling to be the cap actually in force
  — below it the block wins (a supported configuration, reported once per run as
  `[info]`, not an error). Raising the ceiling alone does nothing while a block
  sits below it.
- It also drives chapter packing at `floor(0.8 × pack_cap) − 256`, where
  `pack_cap` is `min(ceiling, smallest translator block)` — about 52k
  characters per part at 64k, **about 200k at 256k**. A chapter that
  previously split now translates in one call. That is fine for typical
  chapters (the measured one was ~8k characters) but means an unusually large
  chapter is no longer split at all.

Small jobs and the retry bound are unchanged: `glossary`, `recap`, `profile`
and `annotator` stay at 64k, and `max_attempts` still bounds retries.

### `MiniMax-M3`: use `thinking`, and note the different failure shape

`reasoning_effort` is **ignored** by `MiniMax-M3` — verified live: setting it
to `xhigh` behaves identically to omitting it (same runaway). M3 accepts
`thinking: {"type": "disabled"}`, which does work and answers directly, so the
MiniMax-only example file uses that instead.

M3 also fails differently: with `reasoning_split` unset it inlines thinking
into `content` as `<think>…</think>`, so the budget is consumed by a think
block that sits in the answer text rather than in a separate field.
`lib/client.py` strips a leading think block before the empty-content check,
so this never reaches the translated chapter — verified by translating a
chapter end-to-end with the output checked for tags.

Because M3 cannot bound its reasoning depth, **use `MiniMax-M3.1-Flash-Preview`
when you want deep thinking**; use M3 when you want a reliable direct answer.

### When the budget does run out

A reasoning model that exhausts its budget returns `finish_reason: "length"`
with `content` **absent** — not empty — and `reasoning_content` as the only
populated field. `lib/client.py` reads it with `.get()` so that shape reaches
the empty-content diagnostic, which names the real cause:

```
empty completion content from <url> (reasoning_content present - the budget
went to thinking: lower providers.<job>.extra_body.reasoning_effort for a
hosted reasoning model, or set providers.<job>.thinking=false for a local
sglang server)
```

Do not let this read as a malformed-payload or schema error. If you see it on a
hosted model, the answer is depth, not decoding.

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

Note that with a two-block translator array the SMALLEST block `max_tokens`
governs both packing and the per-block send — here both are 65536 and the
ceiling is also 65536, so it is a non-issue. Where they differ, the block wins
and `translate_max_output_tokens` is not the cap in force.

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