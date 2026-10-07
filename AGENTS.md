# AGENTS.md

Additional instructions for agents working in this repository, on top of the
behavior docs in `novel-translator/` (`SKILL.md`, `README.md`,
`references/*.md`).

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

- Name it `vNNN.py`, with NNN = highest existing version + 1 (currently
  `v001.py` … `v012.py`, so the next is `v013.py`).
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
