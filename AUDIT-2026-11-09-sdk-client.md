# AUDIT-2026-11-09-sdk-client

Campaign ledger for `PLAN-2026-11-09-sdk-client.md`. The pinned functional goal
(lives in the PLAN §"Pinned functional goal") is the audit reference; the
PLAN's internal consistency is not.

Model tiers actually used (user override of PLAN §4's pin, 2026-11-09):
implementer/fixer = `minimax/MiniMax-M3.1-Flash` tier (session Flash model
implements; the plan's role separation holds — implementer never runs gates);
**all audit/confirm dispatches (P1 lenses, D-round adversarial audits, Dn
whole-branch audit) = `qwen-alibaba-model-studio-intl/deepseek-v4.1-flash-1017`**,
not `minimax/MiniMax-M3`.

---

## P1 — static lens fan-out (2026-11-09)

Four lenses (goal-testability, goal-achievement, feasibility,
convention-poisons) ran on deepseek v4.1 flash over the PLAN + `AGENTS.md` +
the code they reference. 36 findings total; dispositions merged once below.
Both the auditors and the orchestrator independently probed the resolved SDK
majors (`uv run --with "openai>=1.60"` → **openai 3.48.0**;
`--with "anthropic>=1.0"` → **anthropic 2.7.0**, both on `httpx2`, not
`httpx`) and reproduced every SDK-side finding with a real
`MockTransport` capture. The repo test suite was not run by the auditors
(read-only lenses; the orchestrator runs the gates).

### Adopted — design amendments (high severity)

| # | Finding (evidence) | Adopted as |
|---|---|---|
| A1 | PLAN's anthropic native structured outputs use `output_format=...`; anthropic 2.7.0 declares **no** `output_format` (`messages.create(output_format=...)` → TypeError) and its native field is `output_config={"format": {"type": "json_schema", "schema": ...}}` (wire-verified via `httpx2.MockTransport`) | Anthropic backend sends `output_config={"format": {"type": "json_schema", "schema": json_schema}}`; the guided fallback strips `output_config`; `_GUIDED_UNSUPPORTED_RE` extended with `output_config\|output_format` (covers both wire generations); tests + docs use `output_config` |
| A2 | `top_k` / `repetition_penalty` are not declared `chat.completions.create` params (TypeError on openai 3.48.0); today they are plain body keys | They move into the synthesized `extra_body` (non-null only), keeping today's exact wire JSON; merge order: `chat_template_kwargs` → user `extra_body` → `response_format` LAST (verified: extra_body beats declared params, so response_format is un-overridable only inside extra_body) |
| A3 | No-auth is broken as planned: `openai.OpenAI(api_key="")` raises `OpenAIError: Missing credentials`; `anthropic.Anthropic(api_key="")` sends `Authorization: Bearer ` (empty) — both wire-verified | openai: placeholder key (`api_key="unused"`) + per-call `extra_headers={"Authorization": Omit()}` (public `openai.Omit`; wire-verified: header absent). anthropic: `api_key=""` + per-call `extra_headers={"Authorization": Omit()}` (wire-verified: header absent). Both pinned by no-auth construction tests against the real SDKs |
| A4 | The PLAN's "other 4xx → immediate LLMError" drops today's **retryable-400** branch (400 while the guided schema is armed whose text does NOT match the regex → full 4-attempt 2/4/8 ladder; `test_client_cache.py` case 5 pins it) | Ladder mapping restored: armed-400 non-matching → ladder; other 4xx → immediate. Case 5 stays green unchanged |
| A5 | openai usage is a pydantic `CompletionUsage`, not a dict — `logger._count` skips it and `json.dumps` fails (silently swallowed), zeroing token accounting | The openai backend passes `usage.model_dump()` into `_response_meta`; new coverage asserts a real chat meta's usage through `logger._count` for both protocols |
| A6 | `resolve_model(base_url, headers=None)` has no protocol input — per-protocol listing and the `(base, auth, protocol)` cache key are unreachable through production callers | Signature becomes `resolve_model(base_url, headers=None, protocol="openai")`; `consensus.py` + `translate.py` ping pass the block's protocol; the two test fakes updated. PLAN §5 amended: zero *chat-path* caller changes; the resolve_model call sites pass `protocol` |
| A7 | Factories constructed with `timeout=_CHAT_TIMEOUT` would make `probe` (30 s) and `/models` (30 s) hang 600 s | Client constructed with `timeout=_CHAT_TIMEOUT`; per-call `timeout=` overrides: `_MODELS_TIMEOUT` for `models.list`, the probe's own `timeout` param for `probe` |
| A8 | D1 landing without its doc mirrors violates AGENTS.md's same-change rule | D1's commit carries the client-behavior doc mirrors (file-formats: guided JSON per protocol, provider-failure ladder, trace-log usage note; SKILL.md description + Providers bullet; README prerequisites); D2's commit carries config validation + v020 + its own mirrors (validation contract, examples, migration row) |

### Adopted — process/test amendments (medium & low)

- **A9** `references/cli.md` is GENERATED (`test_cli_doc.py` byte-compares to `gen_cli_doc.build()`): the `--api-base` wording change edits `translate.py`'s argparse help and regenerates the file; never hand-edit cli.md.
- **A10** v020 breaks `test_migrate.py`'s head pin (case 13k asserts chain head 19): update to 20 and add v020 cases in the same phase.
- **A11** New-coverage additions: `call_id` pairing (first/last meta share it) + a table over terminal paths (success / retry-success / fallback / fatal / exhausted / malformed) asserting exactly one `llm_response` each; **real-SDK transport smoke suite per protocol** (real OpenAI/Anthropic clients on `httpx2.MockTransport` asserting the serialized wire body + headers — catches stub-invisible TypeErrors like A2/A1); response-shape guards (non-JSON 200 → str payload; missing choices → `LLMError` + terminal meta, mirroring today's payload checks).
- **A12** Tests use `httpx2.Response`/`httpx2.MockTransport` (both SDKs depend on `httpx2`; `httpx` is not installed). Where a suite needs it directly, `httpx2` is declared in that suite's PEP 723 block.
- **A13** The protocol ValueError joins the documented provider-refusal contract (file-formats' "four messages" becomes five) with a verbatim test (`test_provider_arrays.py` asserts the texts verbatim).
- **A14** `config.local.example.anthropic.json` ships with updated intro counts/claims in `config.local.EXAMPLES.md` (the "Two ready-to-copy overlays" line is pre-existing drift) and joins `test_sync_config.py` case_10's loader-validation list; its live-verification status is stated explicitly (owner-run ping pending).
- **A15** Existing OpenAI-only doc bullets get per-protocol wire rules (base_url normalization, auth header, null-knob omission, `chat_template_kwargs` OpenAI-only, temperature "always sent" → per-protocol).
- **A16** `tests/run_all.py`'s docstring union list gains openai/anthropic in the same edit as its dependency line.
- **A17** P2 copies the gitignored `config.local.json` into the worktree (done — provider-resolution phase); the live provider check names its invocation (`uv run novel-translator/scripts/translate.py ping --project <example>`) and its output is recorded in this ledger as an owner-run step.
- **A18** Provider SDK docs (user pointer, 2026-11-09): MiniMax, Z.AI and Qwen Cloud publish how their endpoints are called through the openai/anthropic SDKs — D2 consults them when writing the per-protocol wire rules and the new anthropic example, and the examples reflect the providers' documented base_url/auth shapes.

### Rejected

- **R1** Pin exact SDK versions (`openai==3.48.0`, `anthropic==2.7.0`) in every PEP 723 block. House convention is `>=` floors with no lockfiles (`*.py.lock` is gitignored; every existing block uses floors). Mitigation instead: the real-SDK transport smoke suite (A11) pins the load-bearing behaviors — construction, header suppression, param binding, wire shape — against whichever major resolves at gate time, and the recipes were verified today against the resolved majors.

### Recorded

- **P2 isolation**: baseline recorded (`~/.nt-isolation-baseline.json`, main HEAD `fc04503803ae`, 1 pre-existing change: the untracked `novel-translator/config.local.mixed.json`).
- **Vacuous-green probe**: real suite by absolute path from an unrelated cwd → `20 passed, 0 failed` (exit 0); nonexistent suite → **exit 2**.
- The success condition's "gate ran green in the isolated checkout" evidence = this ledger's D-round entries naming the gate command, exit and output excerpt; "checkout removed" = `git worktree list` captured at close.

---

## D1 — SDK client rewrite (2026-11-09)

**Implemented** (orchestrator-implemented per the Flash-tier implementer role;
all amendments A1–A8 encoded): `lib/client.py` rewritten onto the official
packages — `chat` dispatches on `provider_cfg.get("protocol", "openai")`
(refusing anything else with an LLMError naming the block), `_openai_client` /
`_anthropic_client` are the swappable factory seam (`timeout=_CHAT_TIMEOUT`,
`max_retries=0`; openai gets a placeholder key + per-call `Authorization: Omit`
for keyless blocks, anthropic gets `api_key=""` + the same Omit), the openai
backend merges `chat_template_kwargs`/`top_k`/`repetition_penalty` into
`extra_body` then the user's `extra_body` then `response_format` LAST
(wire-identical to the requests client — the SDK merges extra_body into the
body and extra_body beats declared params), the anthropic backend speaks
`messages.create` with `output_config={"format": ...}` guided JSON,
null-knob omission, usage normalization and no fatal table. `resolve_model`
gained `protocol="openai"` (cache key `(base, auth, protocol)`; the two
production call sites and the two test fakes updated). `_fatal_code` now
takes a parsed payload; `_status_fatal_code` reads `exc.body` with a
`response.json()` fallback. `_usage_dict` flattens SDK pydantic usage with
`model_dump(exclude_none=True)` — a plain `model_dump()` injects null-valued
detail fields the server never sent, which is WORSE fidelity than the old
verbatim server-dict passthrough.

**Suites**: the three transport-coupled suites re-seamed onto the factory
seam (real `openai`/`anthropic` exception instances over `httpx2`
responses; effective wire bodies = declared params merged under
`extra_body`, the SDK's own merge order); every existing assertion kept —
cache keys gain the protocol dimension (planned), case_4 gains a fifth
guided token (`output_config`); three NEW suites: `test_anthropic_backend.py`
(wire shape, guided fallback, ladder, dispatch, cache, no-auth),
`test_meta_contract.py` (call_id pairing + exactly-one-llm_response per
terminal path + JSON-serializable metas + usage through the REAL
`logger._count` for both protocols), `test_sdk_transport.py` (the REAL SDK
clients over `httpx2.MockTransport`: wire bodies, headers, no-auth,
construction pins). PEP 723 sweep: 40 files (37 transitive-client suites by
whole-file import-line scan, translate.py, gen_cli_doc.py, run_all.py + its
docstring union list).

**Doc mirrors in this change** (A8): SKILL.md description line +
Providers-bullet job list (the `verify` job was missing — pre-existing drift
the edit touched); README prerequisites + hosted-provider bullet
(per-protocol rules); file-formats.md guided-JSON-per-protocol, the
openai-protocol-only ZAI fatal table, and the usage-normalization note.

**Gate** (orchestrator-run, `uv run <worktree>/tests/run_all.py`):
- Run 1 (post-implementation): **56 passed, 1 failed** (2739 checks) — the 1
  is `tests/test_autobuild.py`, **pre-existing on this platform**: it fails
  identically on the pristine main tree (`case_6` kill-tool stubbing;
  `ValueError: substring not found` at tests/test_autobuild.py:380 both
  trees; the file is byte-identical to base — `git diff fc04503 --
  tests/test_autobuild.py` is empty). Adjudicated pre-existing red: out of
  this campaign's brief, not fixed here.
- Additional runs follow the audit's fix loop ("one green run is not
  evidence of green").

**Probes run while implementing** (all reproducible): openai 3.48.0 /
anthropic 2.7.0 resolved by the floors; `temperature=None` serializes as
wire `null` on the openai path (parity with the requests client); the
anthropic empty-key wire carries `Authorization: Bearer ` (empty) until the
Omit sentinel suppresses it; `messages.create(output_format=...)` raises
TypeError (the dispositions' `output_config` amendment was mandatory).

**D1 adversarial audit** (3 lenses — goal-conformance, conventions,
correctness-and-security — on deepseek v4.1 flash): 20 findings. The
correctness lens confirmed the load-bearing behaviors it probed (wire
bodies identical to base on every non-extra-win row, no-auth suppression,
no key leakage) and caught **one HIGH defect the stub-seam suites could not
see**: the openai SDK hands status-error bodies UNWRAPPED (`exc.body ==
{"code": 1214}` — `_client.py` lifts `error` out of the envelope), so
`_fatal_code`'s `payload["error"]["code"]` never matched and the whole
ZAI_FATAL_CODES channel was dead against real SDK errors while the suites
(whose `zai()` helper hand-built the wrapper shape) stayed green. The
auditors re-ran the six re-seamed/new suites themselves (38/86/44/34/25/26)
and reproduced the `test_autobuild` red on the pristine fc04503 tree.

**Dispositions and fixes (all adopted, implemented in the fixer round)**:
- **F1 (HIGH)** — `_fatal_code` reads BOTH the SDK's unwrapped error object
  and the wrapped envelope; the two suites' `zai()` helpers now build the
  SDK's real shape; `test_sdk_transport` o16 drives a fatal code through
  the REAL SDK client over MockTransport.
- **F2 (MED)** — the anthropic empty-content terminal path normalizes usage
  (`_anthropic_meta_usage` shared by both terminal paths); e7 asserts the
  normalized meta usage + finish_reason there.
- **F3 (MED)** — malformed 200s can no longer escape raw
  (JSONDecodeError/TypeError/KeyError/InvalidURL): client construction moved
  BEFORE the first meta (wrapped → LLMError, no trace lines, so no unpaired
  request line); per-attempt catch-all → terminal `llm_response` meta +
  LLMError; extraction guarded; probe given the same guards.
- **F4 (LOW)** — a 200 carrying a ZAI error envelope raises LLMFatal again
  (via the `completion.error` extra attribute in the not-choices branch).
- **F5 (LOW)** — both request metas report the EFFECTIVE post-extra_body
  param values (the base client logged the post-merge body; the first
  implementation had regressed to pre-merge declared values).
- **F6 (MED)** — the three prose comment blocks in client.py and the inline
  prose comments in the new suites removed (AGENTS.md no-prose-comments).
- **F7/F8 (MED)** — SKILL.md's Providers operating-notes bullet and
  file-formats.md's base_url + sampling-knobs bullets now carry the
  per-protocol wire rules (the first implementation had updated only the
  description line and the guided/trace sections; the mirrors contradicted
  each other).
- **F9 (LOW)** — README's trace wording protocol-neutral.
- **F10 (LOW)** — `tests/run_all.py`'s union list (docstring + dependency
  line) gains `httpx2` (five suites import it directly; the runner spawns
  children with its own interpreter).
- **F11 (LOW)** — `test_sdk_transport`'s two placeholder `check(True)`
  checks replaced with a real keyless-resolve assertion (o11) and a
  deletion (a8; a9 does the real work).
- **F12 (LOW)** — `test_meta_contract`'s terminal-path table gained seven
  anthropic rows (success/retry/fallback/4xx/exhausted/empty/connection)
  and the runner routes each row by the block's protocol.
- **F13 (LOW)** — chat-path connection-ladder cases added (openai case_8,
  anthropic e8: 4 attempts, backoff [2,4,8], paired metas); the temperature
  default (o14) and null-temperature passthrough (o15) pinned on the real
  wire.
- **F14 (LOW, pre-existing flake)** — `test_token_cap_ceiling`'s candidate
  rows (e3/j3/j4) compared thread-scheduling-dependent append order (the
  auditor measured 2/13 failures on base AND branch); the three comparisons
  are now order-independent (`sorted`) — the per-block clamp VALUES are
  what they pin; no assertion lost a value. 5 consecutive runs green.

**D1 delta re-audit** (2 lenses on deepseek v4.1 flash over the fixes):
7 findings — F1/F2/F4/F11–F14 **confirmed sound and complete** (the auditors
re-ran the six named suites and drove real-SDK MockTransport probes: the
unwrapped fatal body reaches LLMFatal on the first response with paired
metas; malformed 200s → LLMError with exactly one terminal meta; the
anthropic empty path normalizes usage). Residuals, all fixed:
- probe()/resolve_model() constructed SDK clients outside every guard —
  raw `httpx2.InvalidURL`/`ValueError` escaped on malformed base_urls →
  construction (and the openai `_v1_url` normalization, whose IPv6
  `ValueError` edge is pre-existing) is now inside the guards on chat,
  probe and resolve_model on both protocols: an LLMError names the raw
  base and, construction being pre-meta, no trace lines are written (the
  disposition's design; the delta ask's stricter "terminal meta" reading
  for construction failures is adjudicated as the disposition governing).
- the openai request meta omitted a top_p supplied ONLY via extra_body →
  the meta now reports the effective top_p.
- three prose comments survived in the new suites → removed.
- file-formats' sampling-knobs bullet had NOT landed (the edit was in the
  script that died on the README anchor before writing) → applied, plus the
  new provider-failure sentence's "every failure takes the ladder" wording
  corrected (anthropic non-retryable 4xx raise immediately — d4 pins it).

**Post-fix gate** (four runs): `56 passed, 1 failed` each (2780 checks), the
1 being the adjudicated pre-existing `test_autobuild` platform red.
Suite set green individually after the residuals: client_cache 41,
anthropic_backend 36, sdk_transport 28, meta_contract 59,
fatal_provider_errors 86, token_cap_ceiling 44 (×5 stable), ping 19,
consensus 34 — all 0 failed.

**D1 verdict**: the pinned goal holds on the executed diff — every clause
has an implementation path and a proving check (public API unchanged;
ladder semantics incl. the retryable-400 branch; guided fallback per
protocol; meta pairing incl. exactly-one-response on every terminal path;
OpenAI-shaped usage on every terminal path; openai-protocol wire identity
verified old-vs-new by the auditors' wirecmp on the minimal/null-temp/
all-knobs/extra-win/extra-rf-conflict/cap-clamp rows; anthropic blocks
speak the native Messages API with `output_config.format`). Residuals
adjudicated: the `test_autobuild` platform red (pre-existing, out of
brief) and the suite-flake's order-independent comparisons (recorded).

---

## D2 — config validation + v020 + docs mirror (2026-11-09)

**Implemented**: `config.py` gains `PROTOCOLS = ("openai", "anthropic")` and
`_check_protocol(job, element, index)` — called in `_normalize_providers`'s
fold loop (every authored block of every known job) and in
`_normalize_per_job_consensus` (arbitrators under their own key name). The
ValueError text `providers.{job}[{i}] protocol must be "openai" or
"anthropic"` is contractual (mirrored + asserted verbatim). A PRESENT null is
refused like any other invalid value (it survives the defaults fold and
would otherwise die per call at the client dispatch); ABSENT means the
"openai" default, so existing configs load unchanged. The client-side
dispatch `LLMError` remains the backstop for hand-built cfgs. No
PROVIDER_DEFAULTS change.

`migrations/v020.py`: report-only (v013/v015 precedent) — VERSION=20,
one-line DESCRIPTION, reads the RAW config (bare-dict tolerated), walks
PROVIDER_JOBS + consensus_<job> keys, reports which blocks declare which
protocol (`[ok]`), refuses `[FAIL]` on unknown values (predicting the
matching load-time refusal), writes nothing, delegates to
`common.sync_templates` (house idiom — silent no-op when nothing to sync).

**Tests**: `test_migrate.py` head pin 13k → 20 + v019/v020 module-shape
checks (13w–13z); `test_provider_arrays.py` e5–e8 (the verbatim refusal for
arrays/bare dicts/arbitrators, null-is-refused, a load-clean config carrying
both protocols + absence + an arbitrator); `test_sync_config.py` case_10
gains `config.local.example.anthropic.json` (loader-validated; its
`extra_body.thinking: {"type": "disabled"}` is the NATIVE anthropic thinking
param, satisfying the MiniMax-M3 thinking guard per provider family).

**Docs mirror**: file-formats.md (the resolved-view protocol field, a
`protocol` bullet after `base_url`'s per-protocol rules, the four→five
messages validation contract, the v020 prose entry, the per-protocol
sampling-knobs wire rules, the anthropic ladder wording);
config.local.EXAMPLES.md (intro table + the anthropic row, honest
live-verification claims, per-protocol sending rules, the protocol field
row); NEW `config.local.example.anthropic.json` (MiniMax via its documented
Anthropic-compatible endpoint `https://api.minimax.io/anthropic` — Z.AI's
`https://api.z.ai/api/anthropic` documented alongside; the provider SDK-doc
shapes per A18); SKILL.md's migration summary now covers v018–v020 (v018 and
v019 were missing — pre-existing drift the edit touch made worth fixing);
translate.py's `--api-base` help reworded and `references/cli.md`
REGENERATED via `gen_cli_doc.py` (never hand-edited — test_cli_doc passes).

**Gate** (orchestrator-run): 4 runs post-D2 — `56 passed, 1 failed` (2800
checks), the 1 being the adjudicated pre-existing `test_autobuild` platform
red; three more repeats follow the audit.

**D2 adversarial audit** (3 lenses on deepseek v4.1 flash): 19 findings.
The validation itself held — the goal-conformance lens diffed
`_normalize_providers` against the base config.py over 10 valid shapes (0
normalized-dict mismatches; every base contractual refusal reproduced
verbatim), found no `_check_protocol` bypass (bare-dict, arbitrators, the
consensus fallback, defaults-only, merged loads all clean or exact-text),
and drove an anthropic block through `load_config` → `client.chat` dispatch
end to end. v020 was where it broke:

- **G1 (HIGH)** — v020's `[ok]` f-string carried a backslash inside an
  expression: a **SyntaxError on the declared Python 3.11 floor** (the gate
  ran on 3.14, so it passed; `migrations.chain()` would fail on 3.11). The
  quoted protocol list is now built outside the f-string; a
  `compileall` sweep under 3.11 passes.
- **G2 (MED)** — `_collect` conflated a present `protocol: null` with
  absent; now `\"protocol\" not in block` distinguishes them and the null
  gets the `[FAIL]` (matching the load-time refusal), with the `got` value
  double-quoted to match the documented sample.
- **G3 (MED)** — a config with no authored `providers` printed nothing;
  now it gets its `[ok]` line.
- **G4 (MED)** — the "resolution map" wording was wrong twice over: the
  report is a raw-project-file DECLARATION scan (layers 0–2 only; a layer-1
  protocol is invisible — `sync-config` reports the actual resolution), and
  `migrate`'s load pre-flight refuses an invalid-protocol config BEFORE any
  step, so the CLI never reaches the step's `[FAIL]`. The docstring,
  file-formats' v020 entry and SKILL.md's summary now say exactly that (the
  `[FAIL]` branch is the belt for direct module use).
- **G5 (MED)** — the two prose `#` lines above `PROTOCOLS` removed (the
  no-prose-comments rule).
- **G6 (LOW/MED)** — docs nits: the arbitrator sentence in the five-message
  contract, EXAMPLES' mixed-row protocol cell + the Four/three/All-four
  counts + per-protocol qualifiers on the base_url/temperature/
  repetition_penalty rows, SKILL's v019 sentence (synthesis prompt/one
  block per candidate → verify stage 2's prompt/one window per hit).
- **G7 (LOW)** — `test_migrate.py` gains `case_20b_v020_report`: six
  behavioral checks driving `v020.migrate` directly (default line, declared
  block named like the refusal, byte-identity, null `[FAIL]`, unknown-value
  `[FAIL]` with the got quoting, the no-providers line).

**Adjudicated, no change**: the protocol check runs before the
fan-out/base_url refusals (only configs failing the NEW check change which
error they see; no previously-loading config is affected); the
array-wholesale overlay rule can silently drop a layer-1 protocol when a
project re-points the job as an array (the documented merge contract;
residual risk recorded in this ledger); the worktree's hermetic
`config.local.json` was verified present (an auditor noted a stranded
`.runall-hidden` from a concurrently-running gate — the gate's own
recovery reinstalled it).

**Post-fix gate**: 3.11 `compileall` sweep OK; `uv run tests/run_all.py` →
`56 passed, 1 failed` (2806 checks, the 1 the adjudicated pre-existing
`test_autobuild` red).

**D2 delta re-audit** (deepseek v4.1 flash): 6 findings. G1 (the 3.11
SyntaxError) verified fixed by an in-memory 3.11 compile of all 51 script
sources PLUS a `compileall` run on a byte-identical copy (the worktree kept
read-only); G2/G3/G5/G6/G7 confirmed by code read and 19 temp-config
v020.migrate probes; no regression (gate re-run by the auditor: 56/1).
G4 was only PARTLY fixed — the delta caught the residuals, now closed:
- the docstring's "the run's actual resolution is what sync-config reports"
  pointer was FALSE (sync-config prints only base_url + model, never
  protocol — probed live): removed; no CLI command prints a resolved
  protocol, and the docs say so;
- the user-visible DESCRIPTION and the emitted `[ok]` lines still claimed
  resolution ("each block resolves to" / "uses the default") — rewritten to
  declare-framing ("in config.json declare a protocol" / "declare none"),
  with a layer-1 caveat appended to every `[ok]` line ("A protocol set in
  the skill's config.local.json is layer 1 and not visible to this
  report.");
- a non-dict `providers` now reports the shape it saw ("providers is list,
  not a JSON object - no protocol declarations to report") instead of a
  false "no provider blocks are authored";
- the `[FAIL]` got value renders as the JSON literal the config carries
  (`json.dumps`: `null`/`123`/`"gRPC"`), not a fake string literal;
- EXAMPLES' one remaining stale count ("Every Z.AI block in all three
  files" — Z.AI blocks exist in two of four) fixed;
- the docstring's default-line sample matches the emitted text and the
  `[FAIL]` lead-in precedes the `[FAIL]` sample.

**Post-delta gate**: `56 passed, 1 failed` (2806 checks; `test_migrate` 242
with all six 20b behavioral checks). 3.11 compile OK.

**D2 verdict**: the config surface holds the pinned goal — no
previously-loading config changes behavior (auditor-verified across 10
valid shapes), the refusal text is contractual and byte-identical across
config.py / file-formats.md / the app mirror, the hand-built-cfg dispatch
backstop covers the bypass paths, the anthropic example loads through both
loaders, and v020's report says exactly what it reads and what it cannot
see. Adjudicated residuals recorded above.
