"""v18: record the `verify` provider job.

`translate verify` (lib/verify.py) spends model calls on a project's `verify`
job, so `verify` joins `config.PROVIDER_JOBS` and `PROVIDER_DEFAULTS`. A job
named nowhere inherits the translator's whole array (config._normalize_providers),
so no project needs a `providers.verify` key to run it: an unconfigured verify
resolves to the same blocks, endpoint and auth the translator already uses, and
an explicitly authored one overrides.

That inheritance is exactly why this step rewrites nothing. Materializing the
job into a project file would be the pre-v016 behaviour the overlay chain
removed -- it would pin one machine's endpoint into every project it ever
touched, and that project's key would then out-rank a later edit to
config.local.json. So this step follows v012's precedent rather than v007's:
a new job is a schema addition that the read-time layering resolves on its own,
and the only thing worth recording is that the schema moved.

What a maintainer gets from the report line is the two facts a project needs to
change the behaviour:

    [ok] config: providers.verify is a new job (v018) - it inherits the
    translator's provider blocks until you author it; set providers.verify to
    point the whole-book drift scan at its own models, or
    providers.consensus_verify to arbitrate its fan-out differently.

Deliberately NOT rewritten: config.json is untouched. A resolution change with
no old-default sentinel to match on has nothing to migrate (v012), and unlike
that step there is not even a threshold whose MEANING shifted -- verify simply
resolves an array it did not resolve before.

Idempotent by construction: it reads, reports, and writes nothing. A second
call reports the same line.

No template changes for a project to pick up, so this step only calls
sync_templates and is silent on an up-to-date project.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from lib import config

from . import common

VERSION = 18
DESCRIPTION = ("record the `verify` provider job added for `translate verify` "
               "(report-only: it inherits the translator's blocks until "
               "authored; nothing is rewritten)")

_JOB = "verify"


def _resolution_note(project_dir: Path) -> list[str]:
    """One report line naming what the new job resolves to, or why that cannot
    be read here.

    The project is read RAW, on purpose. materialize_config's rule is that a
    migration reads layers 0 and 2 only and never sees config.local.json, so
    any line this step printed about "the endpoint verify will use" would
    describe one machine's layering, not the project's. The raw read answers
    the only question worth asking under that constraint: did this project
    author the job itself, and if so with what."""
    raw_providers = _raw_providers(Path(project_dir))
    if raw_providers is None:
        return []
    authored = raw_providers.get(_JOB)
    if authored is None:
        return [
            f"[ok] config: providers.{_JOB} is a new job (v018) - it inherits "
            f"the translator's provider blocks until you author it; set "
            f"providers.{_JOB} to point the whole-book drift scan at its own "
            f"models, or providers.consensus_{_JOB} to arbitrate its fan-out "
            f"differently."
        ]
    blocks = authored if isinstance(authored, list) else [authored]
    models = [str(b.get("model")) for b in blocks
              if isinstance(b, dict) and b.get("model")]
    detail = ", ".join(models) if models else f"{len(blocks)} block(s), no model set"
    return [
        f"[ok] config: providers.{_JOB} was already authored ({detail}) and is "
        f"unchanged by v018; the new job already resolves to it."
    ]


def _raw_providers(project_dir: Path) -> dict | None:
    """The project's own providers object, or None when it cannot be read.

    An unreadable or non-object config.json yields None and no line at all:
    `migrate` reaches this step only through the versions before it, which
    already read the file, so a failure here is a file removed mid-run, and
    inventing a report line about it would say nothing useful."""
    import json

    try:
        raw = json.loads((project_dir / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    providers = raw.get("providers")
    return providers if isinstance(providers, dict) else {}


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    """Report that the `verify` job exists and how it resolves. Writes nothing:
    `force` and `confirm` are accepted and unused, because there is no
    overwrite for consent to decide."""
    if _JOB not in config.PROVIDER_JOBS:
        return [f"[warn] config: providers.{_JOB} is not a known job in this "
                f"skill - nothing to record"]
    lines = _resolution_note(project_dir)
    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)