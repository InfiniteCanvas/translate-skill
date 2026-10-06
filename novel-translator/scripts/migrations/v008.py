"""v8: provider arrays, the consensus job, and the consensus.md template.

Three skill changes land here at once. (1) `providers.<job>` becomes an
ARRAY of provider blocks -- the multi-model shape the consensus fan-out
(lib/consensus.py) iterates; a legacy single-block dict keeps loading and
normalizes to a one-element array, so no user value moves or renames.
(2) A new `consensus` provider job: when a job's array carries more than
one model, the task prompt fans out to every model in parallel and one
consensus-provider call synthesizes the final response from the candidates
(under the task's own JSON schema); the job is exactly one block -- when
omitted it inherits the project's translator block at the consensus
sampling defaults, and an explicitly authored multi-block consensus array
is rejected at load. (3) The newly shipped consensus.md template (the
synthesis prompt: original task prompt + labeled candidates).

No config.json DEFAULTS key is added, removed, or renamed -- the schema
change is confined to the "providers" section -- so materialize_config
performs the whole conversion through the established provider
normalization: load_config normalizes (legacy dicts wrap into one-element
arrays, every user-set key preserved verbatim inside its block, an omitted
job inherits the project's translator list, and the consensus entry
materializes) and save_config writes the normalized array form back,
reporting "provider blocks normalized (no new top-level keys)", like the
no-new-keys case of v007. The template side is sync_templates as in every
step since v001: missing copies (consensus.md) are always added, drifted
ones only refreshed with consent (--force silently, or the caller's
confirm callback; confirm=None non-interactively keeps the user's copy
with a [warn]). Idempotent like v001-v007 -- a project whose config is
the merged form and whose templates already match the shipped copies
reports [].
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Relative import: the module is only ever reached through the package
# (chain() imports it as migrations.v008, and translate.py / the tests both
# import `migrations` with scripts/ on sys.path), so binding the sibling
# through the package works regardless of which sys.path root made the
# package importable.
from . import common

VERSION = 8
DESCRIPTION = ("provider arrays + the consensus job (multi-model "
               "consensus); ship consensus.md")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    # confirm passes straight through to standard_step (which hands it to
    # sync_templates): interactivity was decided once, upstream in
    # cmd_migrate (TTY -> prompt, else None), and this step never touches
    # stdin itself.
    return common.standard_step(project_dir, templates_src, dry_run, force, confirm)
