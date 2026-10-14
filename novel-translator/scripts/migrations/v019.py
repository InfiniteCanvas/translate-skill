"""v19: `translate verify` stage 2 judges merged evidence blocks.

Stage 2 (lib/verify.py) used to hand the model one CONTEXT_WINDOW window per hit
line, so a term whose occurrences fell close together paid for -- and the model
re-read -- the same sentence up to eight times. It now coalesces a term's
overlapping-or-touching windows, per chapter, into BLOCKS before packing them
into batches, and `glossary_verify.md` describes what it is now being handed: a
context is "a span of lines around one or more places the term occurs", and the
batch line reports the block count AND the occurrence count, because the
rubric's rules reason about occurrences.

The six numbered rules in that rubric are unchanged, word for word. What moved is
the evidence-unit vocabulary and the two counts: this is a change to what the
model is shown, not to what it is asked.

Why a version script exists at all for a template edit: AGENTS.md makes an edit to
assets/templates/.history.json migration-grade, and this change edits it. The new
revision's sha256 is APPENDED beside the old ones, never replacing them, because
both readers match on sha256 alone -- pipeline._load_template falls a project
copy through to the skill's when its bytes are any revision the skill shipped,
and v017 deletes exactly those copies. Appending is what keeps an old project's
unmodified copy classified as a leftover instead of silently becoming a
customization that wins forever.

What the step DOES write is the template: sync_templates copies the current
rubric into a project that has none and offers to refresh one that has drifted --
the same helper and the same consent rules as every other step. What the
maintainer reads is the behaviour change:

    [ok] templates: `translate verify` stage 2 now presents one merged block per
    run of a term's nearby occurrences instead of one window per hit (v019) - the
    same lines in fewer spans, with the prompt stating both the block count and
    the occurrence count; all six rubric rules are unchanged, and a copy you
    customized is left yours.

Deliberately NOT rewritten: config.json. Nothing configurable moved with this --
the batch cap is BATCH_CHARS in lib/verify.py rather than a key, precisely because
the config carries no input-context key to derive one from -- and no project data
shape changed: nothing is moved, renamed or removed, so there is nothing for this
step to express that sync_templates cannot. A project that never migrated needs
nothing either: the shipped base layer already resolves to this rubric on its
next run, which is what makes this step belt-and-braces rather than the delivery
mechanism.

Idempotent by construction: the note is printed on every run and sync_templates is
silent on a project already carrying the current rubric.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 19
DESCRIPTION = ("verify stage 2 presents merged evidence blocks instead of one "
               "window per hit (templates only: config.json is not rewritten, and "
               "the shipped rubric reaches unmodified project copies through the "
               "base layer without this step)")

_NOTE = ("[ok] templates: `translate verify` stage 2 now presents one merged "
         "block per run of a term's nearby occurrences instead of one window "
         "per hit (v019) - the same lines in fewer spans, with the prompt "
         "stating both the block count and the occurrence count; all six rubric "
         "rules are unchanged, and a copy you customized is left yours.")


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    """Report the evidence-block change and sync the rubric it ships in.

    config.json is never written -- `force` and `confirm` are accepted and unused
    there, because there is no overwrite for consent to decide. They are passed
    through to sync_templates, which does have an overwrite: a template that
    exists and differs from the shipped one is asked about."""
    return [_NOTE] + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)