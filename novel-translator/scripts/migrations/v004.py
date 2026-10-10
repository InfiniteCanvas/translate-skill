"""v4: materialize the min_term_occurrences config default.

Projects stamped by v003 predate the novel-wide significance gate for
GLOSSARY_EXPAND: they carry no `min_term_occurrences` config key.
materialize_config folds the new default in strictly add-only (load_config
deep-merges DEFAULTS underneath the file's own contents, so a user-set value
-- even one already sitting under the new key's name -- always wins).
Like v001-v003 this is idempotent -- materialize_config returns [] once the
file is already the merged form -- and the template sync behaves identically
to v003's: missing copies are always added, drifted ones only refreshed with
consent (--force silently, or the caller's confirm callback; cmd_migrate
passes the interactive y/N prompt only when stdin is a TTY).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 4
DESCRIPTION = ("add min_term_occurrences "
               "(novel-wide significance gate for glossary expansion)")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    return common.standard_step(project_dir, templates_src, dry_run, force, confirm)
