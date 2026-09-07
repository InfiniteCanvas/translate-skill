"""Shared building blocks for version scripts.

Both helpers RETURN complete printable report lines (with their own
[ok]/[warn] prefixes) and never print: cmd_migrate owns the console, and
pure return values keep the steps unit-testable. Both are safe to re-run:
materialize_config writes only when the merged form actually differs from
what is on disk, and sync_templates only touches missing templates (or,
with user consent -- --force or an interactive confirm callback -- ones
whose text drifted from the shipped copy).

`lib` is importable here because scripts/ -- the parent of this package --
is on sys.path in every context that can import `migrations` (translate.py
inserts SCRIPT_DIR itself; the test suite inserts the same directory).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from lib import config, project


def materialize_config(project_dir: Path, dry_run: bool) -> list[str]:
    """Fold config.DEFAULTS and normalized provider blocks into the project's
    config.json, preserving every user-set value (load_config deep-merges
    DEFAULTS underneath the file's own contents). Returns [] when the file is
    already the merged form, so no-op runs stay quiet."""
    project_dir = Path(project_dir)
    raw = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
    merged = config.load_config(project_dir)
    if raw == merged:
        return []
    if not dry_run:
        config.save_config(project_dir, merged)
    new_keys = [key for key in merged if key not in raw]
    if new_keys:
        return [
            f"[ok] config: materialized {len(new_keys)} new key(s): "
            + ", ".join(new_keys)
        ]
    # No new top-level keys: the only change was nested provider defaults.
    return ["[ok] config: provider blocks normalized (no new top-level keys)"]


def sync_templates(project_dir: Path, templates_src: Path,
                   dry_run: bool, force: bool,
                   confirm: Callable[[str], bool] | None = None) -> list[str]:
    """Copy shipped prompt templates the project is missing, and manage the
    ones whose text drifted from the shipped copy. Missing templates are
    always copied, never prompted; identical ones stay silent. A differing
    template may be the user's own edit, so overwriting one needs consent:
    --force refreshes silently; otherwise confirm(question) decides when a
    callable was provided, and confirm=None (non-interactive run) keeps the
    user's version with a warning. Interactivity is decided ONCE by the
    caller -- cmd_migrate checks sys.stdin.isatty() -- because this
    function never touches stdin itself, which keeps it unit-testable. The
    templates/ dir is created lazily, right before the first real copy, so
    --dry-run leaves nothing behind."""
    paths = project.paths(project_dir)
    lines: list[str] = []
    for tpl in sorted(Path(templates_src).glob("*.md")):
        dest = paths["templates"] / tpl.name
        if not dest.exists():
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tpl, dest)
            lines.append(f"[ok] templates + {tpl.name} (new)")
            continue
        # Text comparison, not bytes: line-ending drift alone is not a
        # meaningful "user edit" worth a prompt.
        if dest.read_text(encoding="utf-8") == tpl.read_text(encoding="utf-8"):
            continue
        if dry_run:
            fate = "--force would overwrite it" if force else "y/N choice in a real run"
            lines.append(
                f"[warn] templates ~ {tpl.name} differs from shipped ({fate})"
            )
            continue
        if force:
            shutil.copy2(tpl, dest)
            lines.append(f"[ok] templates ~ {tpl.name} refreshed (--force)")
            continue
        if confirm is not None:
            if confirm(
                f"templates ~ {tpl.name} differs from the shipped copy - "
                "overwrite it?"
            ):
                shutil.copy2(tpl, dest)
                lines.append(f"[ok] templates ~ {tpl.name} refreshed")
            else:
                lines.append(f"[ok] templates ~ {tpl.name} kept (your version)")
            continue
        lines.append(
            f"[warn] templates ~ {tpl.name} differs from shipped - kept "
            "yours (non-interactive; --force to overwrite)"
        )
    return lines
