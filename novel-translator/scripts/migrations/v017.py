"""v17: the v2 template layering -- give a project back only the prompts it owns.

Under the layering model (references/file-formats.md, "Overlay layers") a
project's templates/ holds only what a human put there; the skill's shipped
assets/templates/ is the base layer, consulted at READ time and never copied
into the project. Every project written before this step was written the other
way: init copied every shipped template into the project, so each templates/
dir carries a snapshot of the skill's prompts as they stood on the day the
project was scaffolded -- or as they stood the last time a migration refreshed
them.

Those copies are exactly what the new resolution rule already ignores. A file
whose bytes are byte-identical to a revision the skill shipped (recorded in
assets/templates/.history.json, seeded from git history) is an unmodified
leftover, so the skill's current copy is what runs. Deleting it here removes the
dead weight and makes the project's real state visible: whatever remains in
templates/ is a customization the author wrote, and it keeps winning.

A file whose hash is NOT in the history is a customization, and this step never
touches it, never reports it, and never asks about it -- even under --force.
The distinction is not a judgement about content, only about byte identity, so
there is nothing to confirm: a deletion here is never the loss of an edit.

style.md is out of scope. It is a chosen project data shape copied from
assets/styles/, not a template override; the preset name is recorded in
novel_info.json, and there is no honest signal separating "the user edited this
guide" from "this is the unmodified preset".

Idempotent: the second run finds only customizations left, matches nothing, and
reports nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from lib import project

VERSION = 17
DESCRIPTION = ("v2 template layering: delete project template files that are "
               "unmodified shipped copies; keep every customization")

HISTORY_FILE = ".history.json"


def _history(templates_src: Path) -> dict[str, list[str]]:
    """assets/templates/.history.json as {name: [sha256, ...]}.

    An unreadable or malformed file yields an empty map, which classifies
    every project file as a customization -- the conservative direction, since
    the alternative would delete prompts whose history is unknown."""
    path = Path(templates_src) / HISTORY_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(name): {str(rev) for rev in revisions}
        for name, revisions in data.items()
        if isinstance(revisions, list)
    }


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    """Delete every project template whose bytes match a known-shipped
    revision. Customizations are left alone and silent. `force` and `confirm`
    are accepted and unused: this step never overwrites an edit, so there is
    nothing for consent to decide."""
    history = _history(templates_src)
    if not history:
        return [f"[warn] templates: no {HISTORY_FILE} in {templates_src} - "
                "kept every project template (none can be proven unmodified)"]

    templates_dir = project.paths(Path(project_dir))["templates"]
    if not templates_dir.is_dir():
        return []

    lines: list[str] = []
    for tpl in sorted(templates_dir.glob("*.md")):
        try:
            digest = hashlib.sha256(tpl.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest not in history.get(tpl.name, set()):
            continue
        if not dry_run:
            tpl.unlink()
        lines.append(f"[ok] templates - {tpl.name} (unmodified shipped copy; "
                     "the skill's template is used from now on)")
    return lines