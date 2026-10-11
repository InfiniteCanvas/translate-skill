# AGENTS.md

Additional instructions for agents working in this repository, on top of the
behavior docs in `novel-translator/` (`SKILL.md`, `README.md`,
`references/*.md`).

## Verification loop (for non-trivial changes)

`workflows/verification-loop.md` defines how a non-trivial change gets planned,
audited and landed here: implement each phase once in an isolated `git worktree`,
verify it there by running `uv run tests/run_all.py` live, adversarially audit
the *executed* code against the plan's pinned goal, then fast-forward merge that
branch and delete the checkout. The branch that is verified is the branch that is
merged, so nothing experimental reaches the working tree and implementation is
paid once.

Two things to know before following it:

- **The audit reference is the pinned goal, never the plan's internal
  consistency.** That is the rule that makes the loop worth running; a test that
  mirrors the plan does not catch a plan-level error.
- **The role agents are `vt-auditor` and `vt-implementer`**, and the model split
  (Flash implements, `MiniMax-M3` audits) is applied by the orchestrator at
  `task`-dispatch time — the runtime pins no model, so a forgotten `model`
  argument silently runs an audit on the session default.

For a small, obviously-correct change, just run the gate and ship it; the loop
earns its cost on changes whose failure modes a static read cannot catch.

## After finishing a task: check the changed files for stale docs and code

When a task is done, re-read every file you changed plus its doc mirrors, and
clean up anything the change made stale in the same change — don't leave it
for a later pass. The docs deliberately mirror the code key-for-key, so they
rot quietly:

- `novel-translator/references/file-formats.md` mirrors
  `scripts/lib/config.py` (`DEFAULTS`, `PROVIDER_DEFAULTS`), every file
  schema, exit codes, and the migration contract.
- `novel-translator/SKILL.md` and `novel-translator/README.md` restate
  subcommand semantics, defaults, console markers (`[ok]`/`[warn]`/`[FAIL]`),
  and migrate prompt wording for the pipeline and project commands.
  README, written for humans running the CLI, restates the maintenance
  workflows as well.
- `novel-translator/references/maintenance.md` restates subcommand
  semantics, defaults, and console markers for the maintenance commands
  (`review glossary|notes|fix`, `tn`, `glossary
  set|merge|retire|replace|search|count`, `util replace`); SKILL.md
  carries only their trigger line.
- `novel-translator/references/ingestion.md` mirrors ingestion behavior.

Defaults, key names, exit codes, flags, console output, or schemas that
changed in code must be propagated to the matching docs. If code changed, run
`uv run tests/run_all.py` and make it pass before calling the task done.

## Comments: the code documents itself

`novel-translator/scripts/**.py` carries no prose `#` comments. Docstrings are
the only prose allowed, because they are attached to the definition they
describe and are read as API. The only comment constructs permitted in source
are the functional ones a tool parses rather than a human reads: a line-1
shebang, a PEP 723 `# /// script` block (uv resolves dependencies from it), and
`# type: ignore[...]`. Everything else goes.

The maintainer's reason: a stale comment that contradicts the code is worse
than no comment, because it misleads a reader -- and an AI reading this repo
cannot tell a stale comment from a true one.

Design rationale does not belong in a code comment. Put it in the doc mirror
(`SKILL.md`, `README.md`, `references/*.md`) per the section above, where it
is maintained deliberately instead of rotting beside the code. Do not
re-add explanatory comments to restore clarity.

`tests/**.py` follows the same rule with one exception, reserved for
integration suites: a suite that exercises real system behaviour end to end
may keep a module docstring stating what the SUITE covers and which system
behaviour it enforces. Never comment an individual test, and never comment a
unit-test file at all -- there, the test's name and its assertions are the
specification.

## Provider routing: quality outranks quota cost

The maintainer holds paid Z.AI and MiniMax coding plans and has stated that
output QUALITY is the priority — token/credit cost is explicitly not a
constraint. Do not "optimize" a working provider setup by collapsing a
multi-model consensus fan-out back to a single block, demoting the
`consensus` synthesizer to a cheaper model, or trimming `max_tokens`, unless
asked. Prefer the strongest model per job and more candidates where it
measurably improves output; report cost as a factor, not as a recommendation
to downgrade. `novel-translator/config.local.EXAMPLES.md` carries the routing
table, the credit math, and the documented cost model.

## Config-key or template changes require a new migration

Any change that adds, removes, renames, or re-keys entries in the project
`config.json` schema (`DEFAULTS` or `PROVIDER_DEFAULTS` in
`novel-translator/scripts/lib/config.py`), or that modifies the skill-shipped
prompt templates in `novel-translator/assets/templates/`, must ship a new
migration script in `novel-translator/scripts/migrations/` building on the
latest one:

- Name it `vNNN.py`, with NNN = highest existing version + 1 — read the
  current head off `scripts/migrations/` rather than from this file, which
  deliberately does not track it.
- The module must define `VERSION` (int, equal to the NNN in the filename),
  `DESCRIPTION` (one line), and
  `migrate(project_dir, templates_src, dry_run=False, force=False, confirm=None) -> list[str]`
  returning `[ok]`/`[warn]` report lines. No registry edit is needed:
  `chain()` discovers and validates the scripts automatically, and `init` and
  `migrate` pick up the new head version. `migrations/v001.py` is the
  hand-written reference implementation; reuse the helpers in
  `migrations/common.py` (`materialize_config()` folds config defaults,
  `sync_templates()` refreshes templates, and `standard_step()` is the
  ready-made combination that newer versions delegate to).
- `sync_templates()` already copies added templates and refreshes drifted
  ones during migrate; the new migration is still required by this rule, and
  it is also the place for anything that helper can't do (renames, removals,
  config-side counterparts).
- A change that alters only the *resolution rule* for existing keys — none
  added, removed, renamed or re-keyed — does not literally trigger this rule,
  but `v009` set the precedent of shipping one anyway to leave an upgrade
  record. `v012` did the same and is deliberately **report-only**: a resolution
  change has no old-default sentinel to match on, so it rewrites nothing and
  only explains the new semantics and its packing consequence.
- The normative contract is `references/file-formats.md` § "Migrations"; if
  the contract itself changes, update that section too (see the rule above).
