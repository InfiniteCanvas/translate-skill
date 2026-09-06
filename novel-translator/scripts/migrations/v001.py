"""v1: materialize config defaults; copy templates missing from the project.

Pre-versioning projects were written by an older init: config.json without
today's DEFAULTS keys / full provider blocks, and templates/ that may
predate newly shipped prompt templates. Both fixes are idempotent, so this
step is also a no-op for anything already current.
"""

from __future__ import annotations

from pathlib import Path

# Relative import: the module is only ever reached through the package
# (chain() imports it as migrations.v001, and translate.py / the tests both
# import `migrations` with scripts/ on sys.path), so binding the sibling
# through the package works regardless of which sys.path root made the
# package importable.
from . import common

VERSION = 1
DESCRIPTION = ("materialize config defaults and provider blocks; "
               "copy templates missing from the project")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False) -> list[str]:
    return (
        common.materialize_config(project_dir, dry_run)
        + common.sync_templates(project_dir, templates_src, dry_run, force)
    )
