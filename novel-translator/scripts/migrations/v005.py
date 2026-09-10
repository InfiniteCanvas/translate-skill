"""v5: reword the glossary templates to named entities, named actions, and
name-bound titles.

Projects stamped by v004 carry glossary templates that admitted "titled
positions ... plus honorifics and fixed forms of address", so
speaker-dependent kinship and address terms -- "great grandmother", a bare
"Empress Dowager" -- landed in the glossary even though the referent is
designated differently elsewhere ("Empress Dowager Zhao"). The shipped
templates now qualify only named entities, named actions, and titles bound
to a name.

No config.json key is added, removed, renamed, or re-keyed, so unlike
v001-v004 this step is sync_templates only: missing copies are always
added, drifted ones only refreshed with consent (--force silently, or the
caller's confirm callback; confirm=None non-interactively keeps the user's
copy with a [warn]). Like v001-v004 this is idempotent -- a project whose
templates already match the shipped copies reports [].
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Relative import: the module is only ever reached through the package
# (chain() imports it as migrations.v005, and translate.py / the tests both
# import `migrations` with scripts/ on sys.path), so binding the sibling
# through the package works regardless of which sys.path root made the
# package importable.
from . import common

VERSION = 5
DESCRIPTION = ("restrict glossary terms to named entities, named actions, "
               "and name-bound titles")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    # confirm passes straight through to sync_templates: interactivity was
    # decided once, upstream in cmd_migrate (TTY -> prompt, else None), and
    # this step never touches stdin itself.
    return common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)
