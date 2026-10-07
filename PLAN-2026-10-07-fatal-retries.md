# PLAN — fatal on exhausted retries, and no retry at all for irrecoverable provider codes

Date: 2026-10-07
Status: **implemented.** Rev 2, post-audit — see
`AUDIT-2026-10-07-fatal-retries.md` (verdict REWORK) and its §"Folded into the
plan" for the eleven changes made before implementation.
Suite: 48 scripts / 2100 checks, 0 failed.
Predecessor: `PLAN-2026-10-07-consensus-cap.md` (rev 2, shipped). This continues
from it — the `max_tokens_limit` key exists, and the failure it was fixing is
exactly the failure policy this change makes fatal.
Baseline: `745433e` + the shipped consensus-cap work. Suite green: 47 scripts,
1995 checks.

---

## 1. What is being asked, and what it means here

Two requirements:

1. **On any failed call, once retries are exhausted, cancel the process and log
   a fatal error.** Retries still happen first — the point is that exhaustion is
   terminal and loud.
2. **Some provider error codes are irrecoverable.** Z.AI documents them at
   <https://docs.z.ai/api-reference/api-code>. Retrying an identical request
   against these can never succeed, so they must not spend the retry budget:
   "just increase the retry count to its maximum when encountering these".

### 1.1 What requirement 1 removes

Today two failure paths absorb an exhausted call and keep going:

| path | current behavior | location |
|---|---|---|
| one fan-out candidate fails | `[warn] ... continuing with the remaining candidates` | `consensus.py:180-184` |
| all candidates fail | `raise errors[-1]` (an `LLMError`) | `consensus.py:185-186` |
| only one candidate survives | `degraded` event, used verbatim, no consensus call | `consensus.py:187-195` |
| the synthesis call fails | `degraded` event, candidate 1 used without merging | `consensus.py:234-243` |

All four become fatal. The last two are precisely the silent quality loss that
motivated the previous plan — the `[warn] consensus: ... using candidate 1
without merging` line in the run that prompted this work.

**The consequence worth stating once, before shipping:** this removes
multi-model redundancy. If one of two translator models fails after its retries,
the run dies instead of continuing with the survivor. That is the direct reading
of "on any failed calls", and it is consistent with `AGENTS.md` ("quality
outranks quota cost") — a single-model answer is not the answer the project
configured. It is called out here, not buried, and implemented as asked.

### 1.2 What requirement 1 also exposes

`LLMError` is not caught at the top level today. `translate.py:2240-2266` handles
`CliError`, `PipelineError`, `ValueError`, `KeyboardInterrupt` and `OSError`, so
an `LLMError` reaching `main()` escapes as a **traceback** and exit 1. The
"all candidates failed" path already does this today — a raw traceback, which
`references/file-formats.md:380` ("never a traceback") forbids in spirit for
every other failure class. Making failures fatal requires handling them properly
too, or requirement 1 buys a louder traceback instead of a clean fatal.

---

## 2. The Z.AI error table, classified

From the vendor table (fetched 2026-10-07). The test is one question: **can
retrying the byte-identical request ever succeed?**

### Irrecoverable — jump straight to fatal

| code | HTTP | why retries cannot help |
|---|---|---|
| 1000, 1001, 1003, 1005 | 401 | auth failed / header missing / token expired / 2FA required. *(Already fatal today — 401 is not retryable in the ladder. Listed for completeness and for the message.)* |
| 1113 | 429 | insufficient balance; recharge required |
| 1210 | 400 | invalid API parameter — this is the code from the run that motivated this work |
| 1211 | 400 | unknown model |
| 1212 | 400 | model does not support the call method |
| 1213 | 400 | a required parameter was not sent |
| 1214 | 400 | a parameter is invalid |
| 1215 | 400 | two mutually exclusive parameters both sent |
| 1220 | 403 | no permission *(already fatal — 403)* |
| 1221 | 400 | API taken offline |
| 1222 | 400 | API does not exist |
| 1261 | 400 | prompt too long |
| 1301 | 400 | content filtered |
| 1308, 1310 | 429 | usage/weekly/monthly limit exhausted — resets later, never within this run |
| 1309, 1314 | 429 | coding / enterprise package expired |
| 1311 | 429 | subscription does not include this model |
| 1313 | 429 | fair-usage policy violation |
| 1315 | 429 | key restricted to a different product tier |
| 1316–1321 | 429 | usage limit reached (5h/7d/monthly spend cap) |

### Deliberately still retried

| code | HTTP | why |
|---|---|---|
| 1302 | 429 | rate limit reached — the definition of transient |
| 1305 | 429 | "service may be temporarily overloaded, please try again later" |
| 1200, 1230 | 500 | vendor-side; the ladder already retries 5xx |
| 1234 | 500 | "network error … please try again later" |
| (bare 500) | 500 | internal error |

1308/1310 are the arguable ones — they name a reset time, so a *later* run can
succeed. Within the run that hit them, nothing can, which is the only horizon
this client has.

---

## 3. The change

### 3.1 `client.py` — one hook, two wirings

**`ZAI_FATAL_CODES`** — a module-level frozenset of the strings above, with the
vendor URL in the comment so the table can be re-checked against the docs.

**`_fatal_code(resp) -> str | None`** — reads `resp.json()["error"]["code"]`.
Must survive a body that is not JSON, not a dict, missing `error`, or an
`int`-versus-`str` code (the vendor returns `"1214"` as a string in the shape
example, but `1214` as an int is equally plausible), so it compares on `str()`.
Never raises: a malformed body is not itself a reason to lose the real status.

**In the ladder**, one branch before the retryable ones:

```python
code = _fatal_code(resp)
if code is not None and code in ZAI_FATAL_CODES:
    err = (f"HTTP {resp.status_code} from {url}: provider code {code} "
           f"(irrecoverable - retrying cannot help): {resp.text[:400]}")
    ... meta_hook(...) ; raise LLMFatal(err)
```

The user's own prescription — "increase the retry count to its maximum" — is the
literal alternative of `failures = _MAX_ATTEMPTS; continue`, which re-enters the
same check and raises on the next pass. `raise` directly is the same outcome
with one fewer HTTP call and no `_BACKOFF` sleep, so it is taken instead; the
mechanism is recorded here because it is the property that matters.

**`LLMFatal(LLMError)`** — a subclass. Every existing `except LLMError` keeps
working unchanged (it is a subclass), so the only consumer that must opt in is
the new top-level handler. A non-fatal exhausted retry still raises plain
`LLMError`; both are fatal to the run now, but the subclass carries "the
provider told us retrying is pointless", which is the difference between "try
again in a minute" and "fix your config".

### 3.2 `consensus.py` — stop absorbing

- the per-candidate `except Exception` loop stays (it must collect *all*
  failures so the error names every model, not just the first), but the
  `[warn] ... continuing` branch becomes a collected fatal rather than a
  fallback;
- `survivors == 1` no longer returns verbatim — fatal;
- the synthesis `except` no longer returns `survivors[0][1]` — fatal.

Both `degraded` event emissions are removed. `logger.TIER_2_EVENTS` keeps the
name (removing it would be a separate schema change with no remaining writer) —
or, if the audit finds it dead, drop it and note the migration consequence.

The one thing that survives: a **KeyboardInterrupt** in the fan-out still needs
its dedicated re-raise, because `translate.py:2259` hard-exits on
`consensus._ACTIVE_FANS` and that behaviour is about thread cleanup, not
provider failure.

### 3.3 `translate.py` — clean fatal, no traceback

Add to the `main()` ladder:

```python
except client.LLMError as exc:
    _fail(str(exc))          # [FAIL] <what failed>, never a traceback
    return 3
```

**Exit 3, not 1 or 2.** `1` already means "a chapter ended `needs-review`" and
`2` means "usage or setup error" (`file-formats.md:1633-1641`). A provider
failure is neither: it is "the machine you configured cannot do the job", which
is the one thing an operator or a wrapper script needs to distinguish from a
quality warning. Reusing `1` would make a broken API key indistinguishable from a
chapter needing review.

It also needs the same hard-exit treatment as `KeyboardInterrupt`: a fan-out
raises from `future.result()` while other candidates may still be in HTTP
retries, and the interpreter's `atexit` join would wait out their whole ladder.
So `if consensus._ACTIVE_FANS: os._exit(3)`.

### 3.4 A fatal log event

`logger.log_event(project_dir, {"event": "fatal", ...})` at the point of
failure, carrying chapter, job, model and the error. `[FAIL]` goes to stderr;
the event goes to the orchestration log, which is where a post-mortem looks.
`fatal` joins the tier-1 event set (not tier 2) — it is a pipeline event, and
tier 2's documented meaning is "the pipeline's own reading of individual model
outputs" (`file-formats.md:809`).

---

## 4. Scope

| # | Change | File |
|---|---|---|
| 1 | `ZAI_FATAL_CODES`, `_fatal_code()`, ladder branch, `LLMFatal` | `scripts/lib/client.py` |
| 2 | candidate / single-survivor / synthesis failures become fatal; drop both `degraded` emissions | `scripts/lib/consensus.py` |
| 3 | `except client.LLMError` → `[FAIL]` + exit 3 + `_ACTIVE_FANS` hard exit | `scripts/translate.py` |
| 4 | `fatal` event in the tier-1 set | `scripts/lib/logger.py` |
| 5 | exit 3 in the table; failure-ladder prose rewritten | `references/file-formats.md` § Exit codes, § config.json |
| 6 | same trigger line in `SKILL.md` and `README.md` | `SKILL.md`, `README.md` |
| 7 | tests | `tests/test_token_cap_ceiling.py`, `tests/test_consensus.py`, `tests/test_main_exit_codes.py`, `tests/test_log_routing.py` — see §6 |
| 8 | migration `v014.py`, report-only (no config key changes) | `scripts/migrations/v014.py` |

No config key is added, removed or re-keyed, and no template changes — so §8
follows v012/v013's report-only precedent rather than the rewrite rule. The step
exists to record the policy change in the upgrade history and to report a
project whose providers point at Z.AI without a usable key, which now fails at
the first call instead of degrading quietly.

---

## 5. Behaviour matrix

| situation | before | after |
|---|---|---|
| 429 / 5xx, recovers on attempt 3 | retries, succeeds | unchanged |
| 429 / 5xx, 4 attempts exhausted | `LLMError` → traceback (or consensus degrade) | `[FAIL]`, `fatal` event, **exit 3** |
| one candidate of two fails | `[warn]`, run continues on the survivor | **exit 3** |
| only one candidate survives | `degraded`, used verbatim | **exit 3** |
| synthesis fails | `degraded`, candidate 1 unmerged | **exit 3** |
| Z.AI 1210 / 1211 / 1301 / 1113 / 1308 … | 4 attempts, ~14s of backoff, then degrade | **1 attempt, `[FAIL]`, exit 3** |
| Z.AI 1302 / 1305 / 1200 / 1230 / 1234 | retried | unchanged |
| a non-Z.AI 400 without a code | retried (existing `retryable_400`) | unchanged |
| a 400 whose body is not JSON | retried | unchanged |
| KeyboardInterrupt mid-fan-out | hard-exit 130 | unchanged |

---

## 6. Decisions, with reasons

1. **Exit 3, not 1 or 2.** §3.3. The only reason to add a code is that the
   existing two are already spoken for by documented meanings.
2. **`LLMFatal` subclasses `LLMError`.** Every existing handler keeps working;
   only the new top-level one discriminates. A subclass that were *not* an
   `LLMError` would silently break any `except LLMError` in the tree.
3. **Detect the code from the body, do not pattern-match the message.** The
   vendor returns a structured `error.code`; matching prose is how the current
   `_GUIDED_UNSUPPORTED_RE` works, and it is brittle across a doc rewrite.
4. **The code list is Z.AI-specific and lives in `client.py`.** The client is
   already provider-aware (`_GUIDED_UNSUPPORTED_RE`), and a generic
   "irrecoverable" heuristic cannot exist — only a vendor knows. Unknown codes
   keep the existing behavior, so adding a provider later degrades to today's
   ladder rather than to something wrong.
5. **Exit 3 hard-exits when a fan-out is live**, mirroring `KeyboardInterrupt`.
6. **Both `degraded` emitters go.** They become unreachable, and leaving an
   event no writer can emit is exactly the rot `AGENTS.md` asks to clean up in
   the same change. *Open question for the audit: does anything else read the
   `degraded` event, and does dropping it from `TIER_2_EVENTS` need a migration?*

---

## 7. Explicit non-goals

- **Not** retrying harder. Requirement 2 reduces retries for hopeless requests;
  nothing here raises `_MAX_ATTEMPTS`.
- **Not** adding a resumable/partial-output mode. A fatal run leaves the
  chapter untranslated and the project resumable at the last completed stage —
  that already works and is not being changed.
- **Not** touching the `tn` / `review notes` failure paths, which have their own
  documented non-fatal semantics (`file-formats.md:809-812`). Flagged for the
  audit: does "any failed call" reach those too, or are they out of scope?
- **Not** changing `_BACKOFF`.

---

## 8. Verification

1. `uv run tests/run_all.py` green — baseline 47 scripts / 1995 checks.
2. New: each irrecoverable code raises `LLMFatal` on the **first** attempt, with
   the number of `requests.post` calls asserted at 1.
3. New: a recoverable 429 / 5xx still retries and still succeeds.
4. New: a 400 body that is not JSON, or lacks `error`, or has an int code, does
   not raise and keeps the existing retry path.
5. New: each of the four consensus paths raises rather than degrading, and the
   `degraded` event is not emitted.
6. New: an exhausted retry at the CLI is one `[FAIL]` line, exit 3, no
   traceback — and with `_ACTIVE_FANS` live, a hard exit.
7. Migration `v014` lines and idempotence, plus the chain-head pin moved to 14.
8. Every existing test that pinned a degrade or a tolerated candidate failure is
   found and deliberately rewritten, not deleted — the count is reported.