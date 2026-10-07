# AUDIT — `PLAN-2026-10-07-fatal-retries.md`

Date: 2026-10-07
Plan audited: `PLAN-2026-10-07-fatal-retries.md` (rev 0)
Auditor: `verifier` subagent (adversarial, read-only) + maintainer's own code read.
Verdict: **REWORK.** The Z.AI table transcribes correctly and the exit-3 argument
holds, but requirement 1 as designed does not reach the path that matters.

Repo unmodified during the audit.

---

## How to read this document

Findings split into:

- **Pre-answered** — the auditor read rev 0; the plan had been superseded by a
  design decision I had verified independently but not yet written down. Kept,
  because the reasoning is the evidence for the design.
- **New** — a genuine defect in the plan, folded into rev 2.
- **Accepted / Overruled** — explicit disposition.

---

## BLOCKER 1 — `except client.LLMError` in `main()` would be unreachable for chapter translation *(PRE-ANSWERED, but only partly)*

The auditor's finding: `pipeline.py:1535-1539`

```python
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - becomes retry feedback
                state["feedback"].append(f"TRANSLATE failed: {type(exc).__name__}: {exc}")
```

turns a failed TRANSLATE into retry feedback, `pipeline.py:1850` loops to
`max_attempts`, and `pipeline.py:1888` absorbs the re-raise — so the run ends at
**exit 1**, never 3. For an irrecoverable code this burns
`max_attempts × 2 chunk attempts` per chapter across the whole batch: the exact
opposite of "just increase the retry count to its maximum".

**The design that answers this, which rev 0 did not contain.** I had already
worked it out independently while the audit was running, and verified the MRO:

```python
class LLMFatal(LLMError, PipelineError): ...
```

`PipelineError` is the codebase's *existing* fatal channel — every stage guard in
`pipeline.py` (1535, 1617, 1660, 1716, 1725, 1752) is written as
`except PipelineError: raise` immediately before its broad `except Exception`.
Making `LLMFatal` a `PipelineError` too routes it through **all six with zero
edits to them**. Verified live:

```
MRO: ['LLMFatal', 'LLMError', 'PipelineError', 'Exception', ...]
caught by except PipelineError (fatal channel) OK
```

This works only because `PipelineError` moves out of `pipeline.py` into a new
leaf `lib/errors.py` (`lib/__init__.py` is empty; `PipelineError` is referenced
outside `pipeline.py` only at `translate.py:2245`, so re-exporting the name keeps
every caller working). `client.py` cannot import `pipeline` — `pipeline.py:32`
imports `client`, so the cycle is real and the move is necessary.

**But the auditor is right that this does not cover everything.** Two absorb
sites have no `PipelineError` guard and must be edited explicitly:

- `pipeline.py:1888` — the batch loop, "one bad chapter must not abort the
  batch". This is the literal "cancel the process" point, and its `continue` is
  what turns a fatal into exit 1.
- `pipeline.py:721` — glossary cleanup's `degraded` event.

Adopted into rev 2 §3.2 and §4.

## BLOCKER 2 — the blast radius is ≥10 absorb sites, not 4; §7's non-goal was a hole *(NEW — folded in)*

The auditor enumerated every site that can swallow a provider failure:
`pipeline.py:1537, 1888, 1619-1624, 1727-1728, 1754-1756, 721-728`,
`story.py:250-252` and `276-277`, `tn_recheck.py:244-247`, `review.py:250-252`,
`review_notes.py:306-308`, `profile.py:78`, `translate.py:489`.

Rev 0 listed only the four `consensus.py` paths and declared `tn` / `review notes`
out of scope citing `file-formats.md:809-812`. The auditor read that section and
found I had cited the wrong lines: **806-807** is the actual contract being
revoked — *"one failed batch is reported and skipped, never fatal."*

The auditor's framing is the one worth keeping, and it is better than rev 0's:

> draw the line at `LLMFatal`, not at `LLMError`

But requirement 1 is explicit that an **exhausted retry** also cancels, so both
types are fatal. The plan therefore enumerates every absorb site and states, per
site, whether it dies. §3.2 and §5 of rev 2 carry the list. This is the largest
behavior change in the whole task and it is now written out rather than implied.

## MAJOR 3 — the fatal branch must go BEFORE the guided-JSON fallback, or "1 attempt" is unachievable *(NEW — folded in)*

The sharpest finding in the audit. `client.py:300-313`:

```python
        if resp.status_code == 400 and "response_format" in body:
            if _GUIDED_UNSUPPORTED_RE.search(resp.text):
                body.pop("response_format")
                ...
                continue
```

`_GUIDED_UNSUPPORTED_RE` (`client.py:42-43`) matches
`response_format|json_schema|guided json|x-guided`. A Z.AI `1214` body reading
*"Parameter `response_format` is invalid"* **matches**, so the existing code
would drop `response_format` and re-POST an irrecoverable request — and rev 0's
branch, written as "before the retryable ones", would naturally be placed at
line 315, i.e. *after* this. Every 429-class fatal code has the same problem.

Corrected: the branch sits immediately after `requests.post` (`client.py:289`),
before `client.py:300`. Verified against the real regex, not assumed.

## MAJOR 4 — the `_ACTIVE_FANS` hard-exit guard is dead code, and its justification is false *(NEW — folded in)*

Rev 0 proposed `if consensus._ACTIVE_FANS: os._exit(3)` with the reasoning that
a fan-out raises while siblings are still in HTTP retries. Under rev 0's own
design (collect every failure, *then* raise), the raise happens at
`consensus.py:180-186` — **after** `pool.shutdown(wait=True)` (`:175`) and after
`_ACTIVE_FANS -= 1` (`:176`). The counter is 0. The auditor verified
`consensus.py:151` is its only increment and `consensus.py` is the only
`ThreadPoolExecutor` user in `lib/`, so the state is unreachable — forbidden by
the maintainer's standing rule.

Compounding it, the guard as written would have two further defects: it omits
the `sys.stdout.flush()` / `sys.stderr.flush()` the KeyboardInterrupt arm does
(`translate.py:2260-2261`), so the `[FAIL] {file}:` lines from `pipeline.py:1890`
would be lost; and `os._exit` skips `atexit`, orphaning an in-flight
`build-epub` child (see MISSED 2).

**Dropped entirely.** The KeyboardInterrupt arm keeps its guard — that one is
genuinely reachable, because it *abandons* workers rather than joining them.

## MAJOR 5 — `degraded` is neither dead nor a tier-2 event; rev 0 was wrong on both *(NEW — folded in)*

Rev 0 §6.6 said "removing it would be a separate schema change" and asked whether
dropping it from `TIER_2_EVENTS` needs a migration. Both wrong:

- `logger.py:56-58` — `TIER_2_EVENTS = frozenset({"llm_request", "llm_response", "result", "chunk", "feedback"})`. `degraded` was never in it; it already routes tier 1 (`file-formats.md:1254`, `README.md:819`). Nothing to drop, no migration consequence.
- There is a **third** emitter rev 0 left intact: `pipeline.py:721-728` emits `degraded` for `glossary_cleanup`. I had found this independently while the audit ran, which is the only claim of mine in this audit that held up.

Corrected: `degraded` survives (one writer left), `TIER_2_EVENTS` is untouched,
and §8.5's assertion is scoped to the consensus paths only.

## MAJOR 6 — the quota-window codes should not be irrecoverable *(NEW — folded in)*

The auditor re-fetched the vendor table and confirmed the transcription is
faithful: every code's HTTP status and every bucket matches. The *classification
argument* is what fails.

`1308`, `1310`, `1316`–`1321` all publish `next_flush_time` — 5-hour, 7-day and
monthly windows. A whole-novel `translate` run routinely spans hours. Rev 0
asserted they "reset later, never within this run", which is not something the
vendor guarantees and is not something the runner knows.

Requirement 1 already makes an exhausted retry fatal, so these codes differ from
the retryable set only by 1 call versus 4 calls plus ~14s of `_BACKOFF`. Trading
that for converting a possibly-self-healing condition into a guaranteed kill is a
bad bargain.

**Adopted.** Irrecoverable is now only what no clock can fix:
`1000, 1001, 1003, 1005, 1113, 1210, 1211, 1212, 1213, 1214, 1215, 1220, 1221, 1222, 1261, 1301, 1309, 1311, 1313, 1314, 1315`.
Retryable adds `1308, 1310, 1316, 1317, 1318, 1319, 1320, 1321` alongside
`1302, 1305, 1200, 1230, 1234`.

## MAJOR 7 — two provider entry points never see a fatal code, including `ping` *(NEW — folded in)*

`client.py:117-123` (`probe`) raises plain `LLMError` without reading the code,
and `client.py:82-89` (`resolve_model`) goes through `raise_for_status()`. So
`ping` — the command an operator runs when the key is broken — reports a generic
"provider unreachable" and cannot distinguish a wrong URL from an empty balance.

Adopted: `_fatal_code` is applied in `probe()` too, and the code reaches the
`[FAIL]` line. `resolve_model` is reachable only for a block that omits `model`;
all shipped examples set it, so its message is improved but it is not given the
full fatal treatment.

## MINOR 8 — the test-impact list was wrong in both directions *(NEW — folded in)*

- `tests/test_consensus.py` case_d (d1/d2), case_e (e1), case_f (f2), case_g (g1/g2) — **must change**. f2 is the trap: it asserts both `[warn] ... - continuing with the remaining candidates` lines print before the raise, and that is the exact string being deleted.
- `tests/test_migrate.py:1519-1523` pins the head at 13 — **must move to 14, and was missing from rev 0's scope table entirely**.
- `tests/test_token_cap_ceiling.py` — **must NOT change**. Every fake returns `status_code = 200`; the Z.AI-1210 story there is docstring-only. Rev 0 wrongly listed it under "rewrite the pinned tests".
- `tests/test_main_exit_codes.py` — **needs an addition** (a case for the exit-3 arm), not a rewrite.

## MINOR 9 — missed doc mirrors *(NEW — folded in)*

`AGENTS.md` makes these normative and rev 0 missed all four:
`references/maintenance.md:246-249` ("candidate failures warn and continue"),
`SKILL.md:634-642` (the v013 narrative's "silently degrades to one candidate"),
`consensus.py:16-21` (module docstring: *"a failed consensus call degrades to the
first surviving candidate verbatim instead of failing the task"*), and the
`max_tokens_limit` rationale in `client.py` / `config.py`, which now understates
a 1210 — it is no longer a degrade, it is an immediate fatal.

## MINOR 10 — the fatal branch must pass `elapsed` to `meta_hook` *(NEW — folded in)*

Every other raise site in the ladder passes `elapsed=time.monotonic() - started`
(`client.py:295, 320, 328, 336, 343`). Rev 0's snippet elided it, so
`_response_meta`'s `0.0` default (`client.py:279`) would log an `llm_response`
claiming zero seconds. Trivial and correct.

---

## MISSED ENTIRELY

1. **`init`'s style profile** (`translate.py:489-490`) — `except Exception` → `[warn] style profile generation failed`. A Z.AI 1113 at init is swallowed and init proceeds. Not in rev 0's site list at all.
2. **`os._exit(3)` orphans `autobuild`'s child.** A hard exit skips `atexit`, so `AutoBuildScheduler.abort()`/`finalize()` never run and an in-flight `build-epub` is orphaned mid-write. Rev 0 argued the hard exit only about the fan-out join and never mentioned this. Independent reason to drop the guard (MAJOR 4).
3. **Resume semantics after a fatal are not "already works."** `pipeline.py:1797` unlinks the state file only after ASSEMBLE, and `state["chunks"]` from the failed attempt may be stale on disk. Worth stating, since re-running is the operator's first instinct after exit 3.
4. **`retry` shares `run_range`.** Blocker 1 applies identically to `retry` — the command a user runs *after* a fatal. Rev 0's §5 matrix did not mention it.

---

## What survived

- **All four `consensus.py` line references are exact** — 180-184, 185-186, 187-195, 234-243 — as are `translate.py:2240-2266` and `:2259`.
- **`raise` ≈ `failures = _MAX_ATTEMPTS; continue`** for genuinely irrecoverable codes: same single `llm_response` meta, same exception type; differs only by one wasted HTTP call and one `_BACKOFF` sleep. Rev 0's reasoning holds — and only holds, which is exactly why MAJOR 6 moves the merely-likely-to-fail codes back to retryable.
- **401/403 really are already non-retryable today** (`client.py:315` retries only `retryable_400 or 429 or >=500`). The "already fatal today" annotation for `1000/1001/1003/1005/1220` is correct.
- **`LLMFatal(LLMError)` is safe**: the only `except client.LLMError` in the tree is `pipeline.py:1440`, wrapping `client.extract_json`, which cannot raise it.
- **No deadlock**: the raise is downstream of `pool.shutdown(wait=True)`.
- **Exit 3 breaks no wrapper** — every subprocess consumer tests `!= 0` (`fix.py:525`, `epub.py:382`, `autobuild.py:177`).
- **`logger.log_event` cannot break the fatal path** (`logger.py:326-327` swallows).
- **v014 is the right filename**; the head is 13.
- **No deadlock and no unjoined worker** under the adopted design.

---

## Folded into the plan before implementation

1. `LLMFatal(LLMError, PipelineError)` + `lib/errors.py`, with the MRO rationale and the six guards it satisfies for free.
2. Explicit `except` re-raises at `pipeline.py:721` and `pipeline.py:1888`, plus the full absorb-site table with a per-site fatal decision.
3. Fatal branch placed at `client.py:289`, before the guided-JSON fallback.
4. `_ACTIVE_FANS` hard-exit **dropped**; the autobuild orphan is the second reason.
5. `degraded` survives with one writer; `TIER_2_EVENTS` untouched; no migration consequence.
6. Quota-window codes moved to retryable.
7. `_fatal_code` applied in `probe()` so `ping` can name the code.
8. `test_migrate` head pin moved to 14 and added to the scope table; `test_token_cap_ceiling` removed from the rewrite list; `test_main_exit_codes` listed as an addition.
9. Four missed doc mirrors added, including `maintenance.md` and the `consensus.py` module docstring.
10. `elapsed` passed to `meta_hook` in the fatal branch.
11. `init`'s style profile, the `retry` command, and post-fatal resume state written into the scope and the behaviour matrix.