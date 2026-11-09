# PLAN-2026-11-09 — SDK-based LLM client with per-provider protocol selection

Status: **planned, not implemented**. Executed through
`workflows/verification-loop.md` — either as a standalone campaign or as
additional D-rounds integrated with another in-flight campaign (see § 6
Integration notes).

**Pinned functional goal — round-invariant.** Every audit in the loop tests
against this statement and `AGENTS.md`, never against this plan's internal
consistency. The goal does not move as code drifts or this document is amended
with verified deviations.

> Every LLM call goes through the official `openai` or `anthropic` Python
> package, selected per provider block by a `protocol` field defaulting to
> `"openai"`. The public client API (`chat`/`probe`/`resolve_model`/
> `extract_json`/`auth_headers`/`LLMError`/`LLMFatal`), the retry ladder
> (4 attempts, 2/4/8 s backoff), the guided-JSON strip-and-retry fallback, the
> trace-log meta contract (`llm_request`/`llm_response` paired by `call_id`;
> one request line per call plus one extra on the guided fallback), and
> OpenAI-shaped usage accounting are all preserved. Existing openai-protocol
> configs behave identically; anthropic-protocol blocks speak the native
> Messages API including native structured outputs.

**Success condition:** a phase is done when its gate ran green in the isolated
checkout, the executed code was adversarially audited against the goal above
and `AGENTS.md`, and the branch was fast-forward merged and the checkout
removed.

## 1. Context

`novel-translator/scripts/lib/client.py` is a hand-rolled `requests`-based
OpenAI-compatible chat client ("minimal OpenAI-compatible chat client
(sglang)"). The maintainer wants the official `openai` and `anthropic`
packages instead, for maintainability, and a per-provider-block `protocol`
field so a mixed setup can include Anthropic-protocol endpoints (Claude)
alongside the existing Z.AI / MiniMax / sglang blocks.

Decisions confirmed with the maintainer:

- Field spelling: **`"protocol": "openai" | "anthropic"`**, per block,
  default `"openai"` when absent (existing configs untouched).
- Anthropic blocks get **native guided JSON**: `output_format` with
  `json_schema` (GA on the Messages API), with the same strip-and-retry 400
  fallback as the OpenAI path.

Verified SDK-side facts that shape the design (2026-11-09):

- Anthropic native structured outputs are GA:
  `output_format: {"type": "json_schema", "schema": ...}` on `/v1/messages`.
  Sources: [Anthropic structured outputs hands-on](https://towardsdatascience.com/hands-on-with-anthropics-new-structured-output-capabilities),
  [AWS Bedrock docs](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-structured-outputs.html).
- **Anthropic SDK 1.0 (Aug 2026) removed `temperature`/`top_p`/`top_k` as
  declared params** of `messages.create()`/`stream()`/`parse()`; they pass via
  `extra_body` (merged into the request JSON as-is). `max_retries` still
  supported. Sources: [SDK 1.0 migration guide](https://www.getclaudeskills.com/blog/anthropic-python-sdk-1-0-migration),
  [migration notes](https://blakecrosley.com/blog/anthropic-python-sdk-1-migration).

## 2. Architecture facts this plan builds on (verified)

All paths relative to the repo root; `scripts/` = `novel-translator/scripts/`.

- **Single funnel.** `client.chat` is called from exactly one place:
  `consensus.py` (224 single-block job; 242–246 fan-out via `pool.submit`;
  287–291 synthesis — the ONLY `enforce_ceiling=False` site). Everything else
  goes through `consensus.chat` (`consensus.py:177-291`) or `pipeline._chat`
  (`pipeline.py:883-896`). **Rewriting client.py's internals therefore touches
  zero callers** as long as the public API and the FakeChat signature
  `(provider_cfg, prompt, json_schema, max_tokens, meta_hook,
  enforce_ceiling)` are kept.
- **Public API to preserve.** `chat`, `probe`, `resolve_model`,
  `extract_json`, `auth_headers`, `LLMError`, `LLMFatal`, `_MODEL_CACHE`,
  `_MAX_ATTEMPTS = 4`, `_BACKOFF = (2, 4, 8)` (`client.py:18-21`).
- **Error channel.** `LLMFatal(LLMError, PipelineError)` rides the
  `except PipelineError: raise` stage guards through the pipeline and exits 3
  (`translate.py:2414-2416`); plain `LLMError` is absorbed by the intended
  degrade paths. This duality must survive untouched.
- **Trace contract** (`client.py:305-431`, consumed by
  `consensus._trace_hook` 112–159, `logreport._details` 207–248,
  `logdashboard._collect_calls` 430–529):
  - one `llm_request` meta BEFORE the retry loop (retries emit no extra
    request lines); a second one only on the guided-JSON fallback, same
    `call_id`; exactly one `llm_response` at the terminal outcome on every
    path (success + all error paths).
  - `_request_meta`: event, call_id, url, model, params
    (temperature/max_tokens/top_p/top_k/repetition_penalty/
    chat_template_kwargs + extra_body), guided_json, prompt.
  - `_response_meta`: event, call_id, url, model, response, finish_reason,
    usage, elapsed_s, error. Authorization never logged.
  - `logger._count` (`logger.py:259-278`) counts tokens from
    `usage.prompt_tokens`/`completion_tokens`/`total_tokens` — **OpenAI
    shape; the anthropic backend must normalize** `input_tokens`/
    `output_tokens` into it or token accounting silently zeroes out.
- **Retry ladder semantics to preserve** (`client.py:340-387`): connection
  errors / 429 / ≥500 → retry with 2/4/8 s backoff; 400 carrying a guided
  schema whose error text matches `_GUIDED_UNSUPPORTED_RE`
  (`response_format|json_schema|guided json|x-guided`, `client.py:54-55`) →
  strip the schema and retry immediately; other 4xx → immediate `LLMError`,
  no retry; `ZAI_FATAL_CODES` (`client.py:57-79`, body `error.code`, never
  message text) → `LLMFatal` on the FIRST response, checked BEFORE the guided
  fallback.
- **Cap semantics** (`_resolve_cap`, `client.py:199-237` +
  `config.block_cap` `config.py:552-576`): caller `max_tokens` is a ceiling
  clamped DOWN to the block's `max_tokens` unless `enforce_ceiling=False`
  (consensus synthesis only); `max_tokens_limit` is a hard provider ceiling
  binding every call including the synthesis. Applies unchanged to both
  protocols.
- **Config surface.** `PROVIDER_JOBS` (`config.py:20`), `PROVIDER_DEFAULTS`
  (61–70; only `translator` ships `top_p`), provider blocks validated
  shape-only in `_provider_blocks` (84–103; the three ValueError texts are
  contractual), folded with `_with_defaults` at read time on every
  `load_config`. Sampling knobs are read per request, deliberately NOT
  schema-validated. Highest migration: **v019**. No
  `assets/templates/config.json` exists.
- **Tests.** Three suites fake HTTP at `client.requests` and WILL break:
  `test_client_cache.py` (swaps `client.requests`/`client.time`; pins
  `_MODEL_CACHE` keys, exact `/v1/models` URL, sleep ladder `[2,4,8]`, meta
  ordering `["llm_request","llm_request","llm_response"]` on fallback,
  reasoning-content hint), `test_fatal_provider_errors.py` (swaps
  `client.requests` + `client._BACKOFF`; every ZAI code,
  fatal-before-fallback 1214 case, post counts, `_fatal_code` robustness,
  `LLMFatal` dual inheritance, probe naming the code),
  `test_token_cap_ceiling.py` (swaps `client.requests`; reads recorded wire
  bodies' `max_tokens`; `_resolve_cap` cases a–k). Everything else swaps at
  higher seams (`client.chat`, `consensus.chat`, `pipeline._chat`, `chat=`
  params) and survives if the signatures stay.
- **Dependencies.** No pyproject anywhere — PEP 723 only. Blocks to update:
  `translate.py:2-5`, `gen_cli_doc.py:2-5`, `tests/run_all.py:47-50` (the
  union list the subprocess runner materializes), plus every
  `tests/test_*.py` whose PEP 723 block transitively imports `lib/client`
  (mechanical sweep: any suite importing client, pipeline, consensus, review,
  review_notes, profile, story, tn_recheck, verify, or translate). `requests`
  STAYS (used by `cover.py:187,217` for image downloads).
- **No token streaming.** `client.chat` is blocking (`_CHAT_TIMEOUT = 600`);
  commit fc04503's "stream progress" is stdout progress printing, not SSE.

## 3. Design

### 3.1 `scripts/lib/client.py` rewrite — public API unchanged, zero caller changes

- **Dispatch.** `protocol = provider_cfg.get("protocol", "openai")`; values
  `"openai"` and `"anthropic"` only; anything else raises `LLMError` naming
  the block. Internal per-protocol backend functions; both share
  post-processing (`_THINK_BLOCK_RE` strip, empty-content check,
  `_resolve_cap`, meta emission, retry ladder, error classification).
- **SDK clients via swappable factories.** Module-level `_openai_client(cfg)`
  / `_anthropic_client(cfg)` (tests patch these — the new seam replacing
  `client.requests` swaps). Both constructed with `timeout=_CHAT_TIMEOUT`,
  **`max_retries=0`** (SDK-internal retries OFF: our 4-attempt ladder stays
  the single retry policy and the meta contract's one-request-line-per-call
  holds), and the resolved api key (`api_key` / `api_key_env`; absent → `""`
  so no auth header is sent — falsy strings suppress the header on both
  SDKs; **verify this in a test**, adjust if either SDK emits an empty
  bearer/key header). Base URL: openai protocol keeps `_v1_url()`
  normalization (`client.py:114-125`); anthropic protocol passes `base_url`
  verbatim (bare origin; the SDK appends `/v1/messages`) — `_v1_url` is NOT
  applied.
- **OpenAI backend** — `chat.completions.create`, wire-equivalent to today:
  - model/messages/temperature/max_tokens (via `_resolve_cap`)/top_p/top_k/
    repetition_penalty as declared params; `thinking` →
    `chat_template_kwargs.enable_thinking` merged into `extra_body` FIRST,
    then the user's `extra_body`, then `response_format` LAST — reproducing
    today's exact override order (known knobs, then extra wins, then
    response_format cannot be overridden, `client.py:283-303`).
  - `response_format = {"type": "json_schema", "json_schema":
    {"name": "response", "schema": ...}}` when `json_schema` given.
  - Error mapping: `APIConnectionError` (incl. `APITimeoutError`),
    `RateLimitError`, `APIStatusError` ≥ 500 → retry ladder; 400 with a
    guided schema whose text matches the guided regex → strip, immediate
    retry; other 4xx → immediate `LLMError` (status + first 400 chars);
    `ZAI_FATAL_CODES` parsed from `exc.body` / `exc.response.json()` via the
    refactored `_fatal_code(payload_dict)` → `LLMFatal` first-response,
    checked before the guided fallback. `ZAI_FATAL_CODES` is
    OpenAI-protocol-only by definition.
  - Empty content + `reasoning_content` hint unchanged (SDK pydantic models
    surface extra fields as attributes).
- **Anthropic backend** — `messages.create`:
  - `model`, `messages=[{"role": "user", "content": prompt}]`,
    `max_tokens=_resolve_cap(...)` (REQUIRED by the API — the cap always
    resolves a value), native `output_format={"type": "json_schema",
    "schema": ...}` when `json_schema` given.
  - `temperature`/`top_p`/`top_k` via `extra_body` (SDK 1.0 removed them as
    declared params). **Null-valued knobs are omitted** (a documented
    anthropic-only deviation: `temperature: null` on the wire would 400; the
    openai path keeps today's send-even-null behavior for wire parity).
  - `repetition_penalty` and `thinking` (sglang `chat_template_kwargs`) are
    OpenAI-protocol-only; ignored on anthropic blocks and documented as such.
    Anthropic native thinking is reachable through `extra_body`
    (e.g. `{"thinking": {"type": "enabled", "budget_tokens": N}}`).
  - Guided fallback: 400 whose error text names `output_format`/`json_schema`
    → strip `output_format`, immediate retry (extend
    `_GUIDED_UNSUPPORTED_RE` with `output_format`; the openai path's regex
    keeps working because it is a superset match on the same message text).
  - Usage normalized: `input_tokens`→`prompt_tokens`,
    `output_tokens`→`completion_tokens`, `total_tokens` summed; raw
    anthropic extras (cache tokens) passed through as-is. `stop_reason` →
    `finish_reason` passthrough (`end_turn`/`max_tokens`/`stop_sequence`).
    Content = join of `type: "text"` blocks.
  - Error mapping: connection/timeout → retry; `RateLimitError` → retry;
    `APIStatusError` ≥ 500 → retry; other 4xx → immediate `LLMError`. No
    numeric fatal table (Anthropic errors are typed, not coded).
  - Empty-content diagnostic adapted for anthropic (native-thinking hint:
    raise the `extra_body.thinking` budget or `max_tokens`) instead of the
    sglang `reasoning_effort` wording.
- **`resolve_model`** per protocol via `client.models.list()` (both SDKs
  expose it); `_MODEL_CACHE` key gains the protocol dimension:
  `(base, auth, protocol)`.
- **`probe`** per protocol, minimal body only (model/messages/max_tokens: 8 —
  no optional fields, per its docstring contract), same `[FAIL]`-shaped
  `LLMError` messages including the "(irrecoverable)" note where a fatal code
  applies (openai protocol).
- **meta `url`** records the true endpoint per protocol:
  `{base}/chat/completions` vs `{base}/v1/messages`, so trace logs and the
  dashboards stay truthful.
- Remove `import requests` from client.py.

### 3.2 PEP 723 dependency updates

Add `"openai>=1.60"` and `"anthropic>=1.0"` (uv resolves the current majors;
the gate exercises the resolved versions) to:

- `novel-translator/scripts/translate.py`, `gen_cli_doc.py`,
  `tests/run_all.py` (union list), and every `tests/test_*.py` whose block
  transitively imports `lib/client` (see § 2 sweep rule). Mechanical,
  per-file edits; `requests>=2.31` stays everywhere.

### 3.3 Tests — rework + new coverage (same phase as the rewrite so the gate stays green)

- Re-seam the three transport-coupled suites onto the factory seam. Stub
  clients record the call kwargs (wire body) and script results / SDK
  exception instances (constructible:
  `httpx.Response(status, request=...)` fed to
  `APIStatusError(message, response=..., body=...)`). **No existing
  assertion weakened** — attempt counts, backoff `[2,4,8]`, meta ordering,
  fatal-before-fallback, cache keys, clamp cases, reasoning hint all kept
  against the new seam.
- New suites:
  - anthropic wire shape: body keys, `output_format` present with a schema,
    knobs via `extra_body`, null-knob omission, usage normalization,
    `stop_reason` passthrough, `<think>` strip, empty-content hint.
  - dispatch: unknown protocol → `LLMError`; default-missing → openai;
    `_MODEL_CACHE` keyed per protocol.
  - no-auth construction (empty api key → no auth header) for both SDKs.
  - anthropic guided fallback + error ladder (429/500 retry, other 4xx
    immediate).
- Keep `tests/mock_server.py` as-is (manual-kit, OpenAI-shaped); it does not
  exercise the anthropic path.

### 3.4 D2 — config validation, migration, docs mirror

- `config.py`: load-time validation — `protocol`, when present on a provider
  block, must be `"openai"` or `"anthropic"`, refusing with a ValueError that
  names the job (mirrors the `base_url` fail-fast posture). No
  `PROVIDER_DEFAULTS` change (absence = `"openai"`); sampling knobs stay
  read-per-request. `provider_list` hand-built cfgs bypass load-time
  validation — the client-side dispatch `LLMError` is the backstop.
- `scripts/migrations/v020.py` (head is v019): **report-only** upgrade record
  for the `protocol` field (v012/v013/v015/v018 precedent). `VERSION = 20`,
  one-line `DESCRIPTION`, `migrate(...)` returns `[ok]`/`[warn]` lines,
  writes NOTHING (no key is added, removed or re-keyed for existing projects
  — default behavior is unchanged). Compose no `materialize_config`.
- Docs mirror (AGENTS.md § "After finishing a task"):
  - `references/file-formats.md`: § config.json resolved-view block + field
    bullet for `protocol`; § sampling-knobs bullet gains the per-protocol
    sending rules (repetition_penalty/thinking OpenAI-only; anthropic knobs
    via extra_body; null-knob omission); § Model response schemas — guided
    JSON per protocol (`response_format` vs `output_format`); § Provider
    failure is fatal — ladder per protocol, ZAI table is OpenAI-protocol;
    § Two-tier trace logs — usage normalization note; § Migrations — v020
    row.
  - `config.local.EXAMPLES.md`: field table `protocol` row ("Sent as" per
    protocol), new subsection for anthropic-protocol blocks (base_url bare
    origin, native output_format, thinking via extra_body), quirks section;
    intro table gains the new example file.
  - **new** `config.local.example.anthropic.json`.
  - `SKILL.md`: description line ("OpenAI-compatible or Anthropic-compatible
    endpoint"), operating-notes Providers bullet (protocol field + per-job
    inheritance), migration summary for v020.
  - `README.md`: prerequisites (Anthropic-compatible endpoint also
    supported), Tuning provider bullets, hosted-provider bullet, Debugging
    trace-log note if the request-meta wording changes.
  - `references/cli.md`: `--api-base` wording ("OpenAI-compatible" default;
    protocol chosen per block), exit codes unchanged.
  - `references/maintenance.md`: verified for staleness (expected: no change
    — it carries no provider schema).

## 4. Verification

Executed via `workflows/verification-loop.md`:

- **P1** static lens fan-out (goal-testability, goal-achievement,
  feasibility, convention-poisons) over this plan; merge once; dispositions
  recorded.
- **P2** bootstrap: `git worktree add <abs>/translate-skill-wt-sdk-client
  -b scratch/sdk-client` from the current working-branch tip; commit this
  PLAN + AUDIT docs on the scratch branch; `python
  workflows/check_isolation.py --record --repo <abs>`; run the
  vacuous-green probe once (real suite by absolute path from an unrelated
  cwd passes; nonexistent suite exits 2).
- **D1/D2 rounds**: isolation verify → implementer (never runs gates) →
  orchestrator runs `uv run <worktree-abs>/tests/run_all.py` live with a
  bounded fix loop (cap 5) → adversarial audit fan-out (goal-conformance,
  conventions, correctness-and-security) against the pinned goal verbatim →
  confirm/classify → fixer → delta re-audit → gates re-run → ledger entry in
  `AUDIT-2026-11-09-sdk-client.md` → commit.
- **Dn**: whole-branch diff vs base + manual walkthrough; then P4
  `git merge --ff-only scratch/sdk-client`, remove worktree and branch.
- **Model split enforced at dispatch** (loop §1 caveat — the runtime pins
  nothing): implementers/fixers on the Flash tier, auditors/confirmers on
  `MiniMax-M3`; the `model` argument is passed explicitly on every Agent
  dispatch.
- **Live provider check**: `translate ping` against the real Z.AI + MiniMax
  endpoints (owner's plans) proves the openai-protocol path on real hosts;
  the anthropic path is covered by tests plus an optional real `ping` when an
  `ANTHROPIC_API_KEY` is available. One green gate run is not evidence of
  green (AGENTS.md): re-run the gate several times before landing.

## 5. Explicitly rejected

- **No `materialize_config` migration step**: `protocol` is a new OPTIONAL
  key; existing projects' config.json stays byte-untouched and behavior is
  unchanged, so the report-only v020 suffices (house precedent v013/v015).
- **No token streaming**: none exists today; progress printing is separate
  and untouched.
- **No pyproject**: the repo is PEP 723-only by design.
- **No caller changes**: `consensus.chat`/`pipeline._chat` and every caller
  keep their signatures; the rewrite is contained in `client.py` + tests.

## 6. Integration notes

- The phases are self-contained: D1 (client + deps + tests) and D2
  (validation + migration + docs) land independently behind green gates and
  can be slotted into an existing campaign as additional D-rounds, or run
  standalone from whatever the working-branch tip is at bootstrap time.
- When integrating with another plan: re-audit the combined pinned goals
  against EACH OTHER (the audit reference is the pinned goal, never a plan's
  internal consistency) — a phase that satisfies one goal while breaking the
  other's is a finding, not a merge conflict to resolve locally.
- `check_isolation.py` fails if the main branch advances mid-campaign; if the
  other work lands first, re-bootstrap the worktree from the new tip.
