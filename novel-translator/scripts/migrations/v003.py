"""v3: backfill existing projects with the git-history feature.

Projects stamped by v002 predate project git history: they carry no
`git_commits` config key and usually no repository. materialize_config folds
the new default in strictly add-only (load_config deep-merges DEFAULTS
underneath the file's own contents, so a user-set value -- even one already
sitting under the new key's name -- always wins), and ensure_repo turns the
project into a git repository: `git init`, a .gitignore for transient
pipeline state, and local-only identity/autocrlf config (fresh projects get
the same repo from init). Like v001/v002 this is idempotent -- ensure_repo
returns [] when .git already exists -- and the template sync behaves
identically to v002's: missing copies are always added, drifted ones only
refreshed with consent (--force silently, or the caller's confirm callback;
cmd_migrate passes the interactive y/N prompt only when stdin is a TTY).

The repo is created without committing: cmd_migrate owns the per-step commit
it fires after stamping the config version, so a project migrated into git
gets its first commit as one snapshot of the fully migrated state.
--dry-run leaves the project untouched: no .git, no .gitignore, no config
write -- it only reports what a real run would do.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Relative import: the module is only ever reached through the package
# (chain() imports it as migrations.v003, and translate.py / the tests both
# import `migrations` with scripts/ on sys.path), so binding the sibling
# through the package works regardless of which sys.path root made the
# package importable.
from . import common
# Same reason `common` can import `from lib import config, project`: scripts/
# is on sys.path in every context that can import `migrations` at all.
from lib import vcs

VERSION = 3
DESCRIPTION = ("git history: materialize git_commits default, "
               "init the project repo")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    # confirm passes straight through to sync_templates: interactivity was
    # decided once, upstream in cmd_migrate (TTY -> prompt, else None), and
    # this step never touches stdin itself.
    lines = (
        common.materialize_config(project_dir, dry_run)
        + common.sync_templates(project_dir, templates_src, dry_run, force, confirm)
    )
    # The git backfill mirrors the helpers' dry-run discipline: a dry run
    # writes nothing (no .git, no .gitignore) and only reports the intent.
    if dry_run:
        if not vcs.is_repo(project_dir):
            lines.append("[git] would initialize the repository "
                         "(a real run commits after each migrate step)")
    else:
        lines += vcs.ensure_repo(project_dir)
    return lines
