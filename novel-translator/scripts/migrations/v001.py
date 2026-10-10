"""v1: materialize config defaults; copy templates missing from the project.

Pre-versioning projects were written by an older init: config.json without
today's DEFAULTS keys / full provider blocks, and templates/ that may
predate newly shipped prompt templates. Both fixes are idempotent, so this
step is also a no-op for anything already current. A drifted template copy
is only refreshed with consent: --force silently, or the caller's confirm
callback (cmd_migrate passes the interactive y/N prompt only when stdin
is a TTY).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 1
DESCRIPTION = ("materialize config defaults and provider blocks; "
               "copy templates missing from the project")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    return (
        common.materialize_config(project_dir, dry_run)
        + common.sync_templates(project_dir, templates_src, dry_run, force, confirm)
    )
