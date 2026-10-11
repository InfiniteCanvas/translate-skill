# Workflow: worktree verification loop (MiniMax Code port)

**Pinned functional goal — round-invariant.** Every audit in this loop tests
against this statement and the repo conventions, never against a plan's
internal consistency. The goal does not move as code drifts or a plan body is
amended with verified deviations.

> Implement each phase of a plan exactly once, in a second checkout of this repo,
> and verify it there by live execution — compilation-equivalent gates and the
> repo's own test suite — so that the branch which is verified is the branch
> which is merged, and nothing experimental ever reaches the working tree.

**Success condition:** a phase is done when its gates ran green in the isolated
checkout, the executed code was adversarially audited against the goal above and
`AGENTS.md`, and that branch was fast-forward merged into the working branch and
the checkout removed. A phase that only ever passed a static review is not done.

**Origin:** ported from the portable spec `SPEC-portable-verification-loop.md`
v2.3 and its zcode implementation in `D:/Repos/web/bkun`. That pair exists
because statically auditing a plan means reasoning about code that has never
compiled or run — compiler and runtime facts land late, by luck, or by manual
reading. The fix is to move auditing into the implementation loop, where the
evidence is executable.

---

## 1. What this harness had to re-derive

SPEC § "What each harness must re-derive" asks four questions. Answers, with the
evidence behind each:

| Question | Answer in MiniMax Code |
|---|---|
| Isolation mechanism, and how gates are pointed at it | `git worktree`; gates run by **absolute path** to the checkout's runner (`uv run <checkout>/tests/run_all.py`). See §3. |
| Verification that gates really run there | The vacuous-green probe in §3. Run it once per new checkout shape; a gate that "passes" on a missing path is worthless. |
| How the model split is enforced | Per-`task` dispatch `model` parameter (§4). **Not** a config field — see the honest caveat below. |
| How lenses, confirmation and bounded fix loops are expressed | `task` fan-out to subagents with distinct roles; convergence by `where`-key counting; caps as loop bounds (§4, §7). |

Two caveats, stated rather than glossed, because both are places where this port
is weaker than the zcode original:

1. **No per-agent model field.** The runtime stores no model for a created
   agent: `vt-auditor/` and `vt-implementer/` contain only `agent.md`, and even
   the built-ins ship an empty `config.yaml`. The model split is therefore
   enforced at `task`-dispatch time by the orchestrator, not by the runtime. In
   zcode, `model('account:...')` was pinned inside the script and survived a
   caller forgetting it; here a forgotten `model` argument silently hands the
   audit to the session default. **The orchestrator must pass `model` explicitly
   on every audit dispatch.**
2. **A created agent is not the built-in `verifier`.** `vt-auditor` is a custom
   agent, so it does not inherit the built-in verifier's deliverable gate — the
   check that returns `ROLE_MISMATCH` when a task would require mutating a
   file. It therefore *has* write tools, and its read-only status is prompt-level
   instruction, not a runtime boundary. Treat "auditors are read-only" as an
   instruction you must honour, not a guarantee. Where mutation-refusal matters,
   dispatch to the built-in `verifier` agent instead.

---

## 2. Pipeline

### P0 — Design doc

Problem, constraints, approach. Written before any plan. Normal practice.

### P1 — Plan + exactly ONE static audit round, as a parallel lens fan-out

The plan is `PLAN-YYYY-MM-DD-<topic>.md` with the **pinned functional goal and
success condition at the top**, matching this repo's existing plan convention.

Launch N adversarial `task` agents in parallel, one per attack surface. Default
lenses:

1. **goal-testability** — checked first, because an ambiguous goal poisons every
   later round. Is it unambiguous and decidable — would two honest auditors
   agree on when it is met?
2. **goal-achievement** — does the plan as written achieve the pinned goal?
   Missing scope, wrong semantics, phases that individually pass but together
   fall short.
3. **feasibility** — mechanical claims against the real repo. This lens reads
   the actual files it names.
4. **convention-poisons** — habits that would replicate across every later
   phase (see the `AGENTS.md` rules: the comment ban on
   `novel-translator/scripts/**.py`, the migration rule for config/template
   changes, the provider-routing rule).

The lens taxonomy adapts to the plan. The constraint is that **each lens is an
attack surface, not a summary pass.**

**Out of scope for P1:** anything a deterministic gate proves. Test outcomes,
import resolution, and config-schema validity are all P3's job.

**Merge once.** Findings are deduplicated and ranked by a single merge step —
lenses overlap, and a pass-through hands the fixer four variants of one finding.
Confirmation rules: convergence (≥2 lenses on the same `where`) = confirmed;
high-severity singles get an independent confirmer; low severity is report-only.

The orchestrator dispositions findings and edits the plan directly. Structural
fixes get a **delta-only** re-audit of what changed. Never a second full round —
past P1 the evidence asymmetry between predicting and reading output only grows,
so extra static rounds re-derive at full cost what one gate run proves.

### P2 — Isolated checkout bootstrap

```sh
git worktree add D:/Repos/vibed/translate-skill-wt-<topic> -b scratch/<topic>
```

- Branch from the working-branch tip.
- `PLAN-*.md` / `AUDIT-*.md` / `DESIGN-*.md` and any referenced assets must be
  **committed on the scratch branch** — they are untracked in the main tree and
  do not travel otherwise. They must be readable by agents working in the
  checkout and visible in review diffs.
- `novel-translator/config.local.json` is gitignored and **must be copied in**
  by hand if the phase touches provider resolution.
- No dependency install step is needed: scripts declare PEP 723 dependencies
  inline and `uv` materializes them per invocation.

### P3 — Dynamic audit rounds D1..Dn, one per plan phase

Each phase must land independently — that is what makes a per-phase round
meaningful.

1. **Implement** the phase in the checkout, from the phase brief embedded
   verbatim. The implementer never runs the gates.
2. **Run the gate live**, as code, branching on exit codes, with a bounded fix
   loop (cap 5). This repo has **one** gate tier — `uv run tests/run_all.py`,
   ~65s, 50 suites — so the two-tier structure of the zcode version collapses to
   a single loop. Gates not green after the cap = the round fails loudly and the
   audit is skipped.
3. **Adversarial audit of the executed work.** Lens fan-out; default lenses are
   goal-conformance, conventions, correctness-and-security. Each auditor gets:
   the pinned goal verbatim, the phase brief for context, the gate output as
   live evidence, and the scope — this round's uncommitted diff, or the whole
   branch diff vs the base for Dn.
4. **Confirm and classify** using the P1 rules. Report each finding as it lands.
5. **Actionable findings → fixer → delta re-audit → gates re-run.**
6. **Ledger entry per round** in `AUDIT-YYYY-MM-DD-<topic>.md`: findings,
   dispositions, and **live evidence** — command, exit code, output excerpt. A
   plan claim invalidated by evidence gets the plan amended and is recorded as a
   verified deviation.
7. **Commit the checkout after each green round**, so the next round's audit
   scopes to its own diff rather than re-reading everything.

**Dn** audits the whole branch diff against the base and carries the manual
walkthrough — one reviewer artifact for executed code, absorbing what used to
be a separate post-implementation review.

### P4 — Land

- Regenerate artifacts inside the checkout pre-merge when the plan calls for it,
  so the merge stays the single landing step.
- Fast-forward merge into the working branch, then remove the checkout and
  branch. Records reference commit hashes, never paths.

```sh
git merge --ff-only scratch/<topic>
git worktree remove D:/Repos/vibed/translate-skill-wt-<topic>
git branch -d scratch/<topic>
```

---

## 3. Gates: pointing them at the checkout, and proving it

**Gate:** `uv run <checkout-abs-path>/tests/run_all.py`

`run_all.py` spawns each suite via `sys.executable` with `cwd=tests/`, and the
suites locate `lib/` relative to `__file__` — so the runner is CWD-independent
and an absolute path from any working directory is correct. Verified: running
one suite by absolute path from `D:\` passes (8 passed, 0 failed).

**The vacuous-green probe.** A gate that silently does nothing is worse than no
gate, because it converts "unverified" into "green". Before trusting a gate
invocation, prove it fails when it should:

| Probe | Expected | Verified |
|---|---|---|
| `uv run <abs>/tests/<real-suite>.py` from an unrelated cwd | passes | ✅ 8 passed, 0 failed |
| `uv run <abs>/tests/<nonexistent>.py` | **exit 2**, loud failure | ✅ exit 2 |

The failure mode this guards against is real in the source workflow: on bun
1.4.2, `bun --cwd <dir> run …` (space form) prints usage and **exits 0 without
running anything**, while `--cwd=<dir>` dispatches correctly. A space-vs-equals
typo there yields an entire verification loop that is green and meaningless.
This repo's `uv` has no such trap, but the probe is cheap and the doctrine
stands.

---

## 4. Roles

| Role | Model | Duty |
|---|---|---|
| Implementer | `minimax/MiniMax-M3.1-Flash-Preview` | Careful developer. Reads before editing, follows the brief exactly, never widens scope, escalates when an instruction cannot be satisfied instead of working around it, **never runs the gates** (the orchestrator runs them). |
| Test author (optional per phase) | Flash | Writes tests/docs against what actually landed. **Never weakens an existing assertion.** |
| Fixer | Flash | Minimal surgical fixes. Never weakens or skips a test to make it pass. Escalates on environmental failure. |
| Auditor | `minimax/MiniMax-M3` | Adversarial: finds what breaks, does not approve. Every finding concrete and evidence-backed (file:line plus the lines read). Read-only. |
| Confirmer | `minimax/MiniMax-M3` | Independently reproduces high-severity single-source findings from evidence alone. Read-only. |

The split is cheap-implements / strong-audits, matching the maintainer's stated
posture in `AGENTS.md`: **output quality is the priority and token cost is
explicitly not a constraint.** `MiniMax-M3` is the auditor because it is the
stronger model of the pair and accepts images and video, which lets an auditor
read a rendered screenshot or a pipeline diagram as evidence rather than
inferring from text. A finding is only worth its cost if it is right.

---

## 5. Audit doctrine (invariants)

- **Every audit opens by testing the artifact against the pinned goal and
  `AGENTS.md` — never the plan's internal consistency.** Rationale: dynamic
  audits audit code that *exists*, so a plan-level error survives whenever tests
  mirror the plan. The goal pin is the mitigation, and it must be a rule, not a
  habit.
- Parallel lenses widen the attack surface; the round remains one shot.
- A static auditor predicts; a dynamic auditor reads output. Hence exactly one
  static round.
- Deterministic gates decide what code can decide. Never pay a model session to
  run a test and report the result.
- **Known risk, kept visible:** dynamic audits cannot fully eliminate
  plan-mirroring tests. The goal pin bounds it.

---

## 6. Concurrency

Unlike the source workflow, this repo imposes **no gate-lane serialization**:
there are no pinned ports and no shared database. The mock server on 8901 is
manual-only (`tests/review-e2e/README.md`), and the suites bind nothing — they
inject fakes and use `TemporaryDirectory` (405 call sites). Gate runs in two
checkouts can therefore proceed in parallel.

Untracked state that does **not** travel to a new checkout, and must be handled
at bootstrap if the phase needs it:

- `novel-translator/config.local.json` (gitignored; per-machine provider config)
- `probe-artifacts/` (gitignored; raw model reasoning, safe to omit)
- `.zcode/` (gitignored; session state — see §8)

No test suite depends on any of these; `config.local.json` is the only one that
can affect a phase.

---

## 7. Failure and bounds

- **Fix caps reset per dynamic round** (cap 5). Each round audits a different
  phase, so a pathological round fails loudly instead of draining a shared
  budget.
- Agents blocked by impossible gates or contradictory instructions **escalate to
  the orchestrator** rather than faking a pass. Personas must make honesty the
  cheap move — that is a prompt-level rule here, not something the runtime
  enforces.

---

## 8. Why these files are not in `.zcode/`

This repo gitignores `.zcode/` (`.gitignore:17`), and that directory holds
agent session state which has no business in version control. It is a fragile
home for durable process files: the source repo's `.zcode/workflows/` holds two
tracked workflows and two untracked ones, because gitignore silently swallows
anything new written there — a workflow added without noticing the ignore is
simply absent from every clone and review diff.

So the workflow lives in `workflows/`, which this repo's `.gitignore` does not
exclude, and the role agents live in the runtime's agent directory. Verify with
`git check-ignore -v workflows/verification-loop.md` — a non-zero exit means the
file is addable as-is.

**What this costs, stated plainly:** MiniMax Code does not auto-discover a
`workflows/` directory. There is no `.dwf.ts` engine here to execute these
files, so the loop is enforced by the orchestrator following this document —
the doctrine is durable and reviewable, but not mechanically enforced the way a
script-run workflow would be. That is the honest trade for tracked, reviewable
files in a harness without a workflow runner.

---

## 9. Six operational decisions

1. **No probe phase** — D1 is the probe. Escape hatch: if the P1 feasibility lens
   flags a claim as likely unimplementable, spike exactly that one claim first.
2. **Plan docs committed on the branch at bootstrap** — they are untracked and
   do not travel otherwise.
3. **One checkout per round**; no cross-round state carryover.
4. **Cleanup immediately after merge.**
5. **Manual walkthrough inside the checkout**, as part of Dn.
6. **Fix caps per dynamic round**, not per campaign.