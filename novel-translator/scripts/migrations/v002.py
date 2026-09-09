"""v2: materialize the review command defaults into existing projects.

Projects stamped by v001 predate the `review` command's config keys
(review_batch_size, review_report_path). materialize_config folds them in
strictly add-only: load_config deep-merges DEFAULTS underneath the file's
own contents, so a user-set value -- even one already sitting under a new
key's name -- always wins. Like v001's fixes this is idempotent (a no-op
for anything already current), and the template sync behaves identically:
missing copies are always added, drifted ones only refreshed with consent
(--force silently, or the caller's confirm callback; cmd_migrate passes
the interactive y/N prompt only when stdin is a TTY).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Relative import: the module is only ever reached through the package
# (chain() imports it as migrations.v002, and translate.py / the tests both
# import `migrations` with scripts/ on sys.path), so binding the sibling
# through the package works regardless of which sys.path root made the
# package importable.
from . import common

VERSION = 2
DESCRIPTION = ("materialize review command defaults "
               "(review_batch_size, review_report_path)")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    # confirm passes straight through to sync_templates: interactivity was
    # decided once, upstream in cmd_migrate (TTY -> prompt, else None), and
    # this step never touches stdin itself.
    return (
        common.materialize_config(project_dir, dry_run)
        + common.sync_templates(project_dir, templates_src, dry_run, force, confirm)
    )
