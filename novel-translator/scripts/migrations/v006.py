"""v6: transliterated measurement units -- translator-note guidance and the
guide-only unit category.

Units like 里, 斤, and 时辰 now ship as catalogue seeds (category "unit"):
kept transliterated in the translation ("li", "catty", "shichen") and
injected into the translation prompt as rendering guides, while the balance
checker ignores them entirely -- their source strings are short and
polysemous (里 in 这里/里面, 寸 in idioms), so drift/usage counting for them
is noise. The shipped templates back this up: tn_generate.md gains a
standing exception requiring a conversion note at a transliterated unit's
first chapter occurrence, and glossary_review.md exempts category "unit"
entries from the mundane judgment (guide-only reference vocabulary, not
named entities).

No config.json key is added, removed, renamed, or re-keyed, so like v005
this step is sync_templates only: missing copies are always added, drifted
ones only refreshed with consent (--force silently, or the caller's confirm
callback; confirm=None non-interactively keeps the user's copy with a
[warn]). Like v001-v005 this is idempotent -- a project whose templates
already match the shipped copies reports [].
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 6
DESCRIPTION = ("transliterated measurement units: conversion-note guidance "
               "and the guide-only unit category")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    return common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)
