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

*(entries appended per round)*
