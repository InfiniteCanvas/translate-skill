"""v13: a provider HARD output ceiling (`max_tokens_limit`) on a block.

No config key is added, removed, renamed or re-keyed at the top level, no
prompt template changes -- so by the letter of AGENTS.md's rule this step is
not required. It ships anyway, on v009's precedent of leaving an upgrade record
for a change in MEANING, and because this one fixes a live failure that the
user cannot see without knowing the number the pipeline sends.

What changed in behavior:

    Before: the consensus synthesis is the one call site allowed to exceed its
    block's own max_tokens (consensus.chat computes max(task cap, block cap),
    because an arbitrator set to 65536 must still be able to merge two
    256000-token chapters). That is a FLOOR, and it was the whole clamp. A
    block whose max_tokens was therefore its PROVIDER's real limit -- Z.AI
    refuses anything above 131072 -- got sent the task cap instead, answered
    HTTP 400 code 1210, and the merge silently degraded to candidate 1:

        job=consensus consensus_for=translator model=glm-5.3 max_tokens=256000
          -> HTTP 400 "The max_tokens parameter is illegal.：[1,131072]"

    After: a block may declare `max_tokens_limit`, its provider's hard
    rejection threshold, which bounds EVERY call to that block including that
    synthesis. `max_tokens` keeps meaning "the budget this block asks for".

Deliberately NOT rewritten, for v012's reason extended: there is no old-default
sentinel. No value on disk is wrong that was right before, and the migration
cannot know any provider's real limit -- that number lives in the vendor's
docs, not in this project. So this step READS, REPORTS, and changes nothing.

What it reports is the one condition observable from the file alone: the
synthesis for a multi-model job would send MORE than the consensus block's own
declared max_tokens, and that block declares no limit. That is precisely the
shape that 400s when the declared number is the provider's ceiling.

Note the polarity, which an earlier draft of this step got backwards. The
report is NOT "translate_max_output_tokens exceeds providers.consensus
.max_tokens": since v012 a block below the ceiling is an explicitly SUPPORTED
configuration (references/file-formats.md), and warning about it would fire on
healthy projects. The condition is on the SYNTHESIS CAP
(max(task cap, block cap)), not on the relationship between two settings.

The line is an [info] and says the shape is supported -- the maintainer is told
what number goes on the wire and decides what their provider accepts:

    [info] config: providers.consensus will be sent up to 256000 tokens to
    merge translator candidates, above its own declared max_tokens (128000),
    and declares no max_tokens_limit. That is supported as long as your
    provider accepts 256000; if 128000 is your provider's real limit, add
    "max_tokens_limit": <the provider's limit> to the consensus block so the
    merge is clamped there instead of failing. Nothing was rewritten.

Idempotent by construction: it reads, reports, writes nothing.

No templates change, so sync_templates stays delegated to common and is silent
on an up-to-date project.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 13
DESCRIPTION = ("provider max_tokens_limit: a block may declare its provider's "
               "hard output ceiling, which binds the consensus synthesis too; "
               "report a consensus block the merge would exceed")

_LIMIT_KEY = "max_tokens_limit"


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    cfg_path = Path(project_dir) / "config.json"
    lines: list[str] = []

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError(f"{cfg_path} must contain a JSON object")
    except (OSError, ValueError) as exc:
        lines.append(f"[warn] config: config.json not readable ({exc}) - "
                     "skipped the consensus cap check")
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    cblock = _one_block(providers.get("consensus"))
    if cblock is None:
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    declared = _as_int(cblock.get("max_tokens"))
    if declared is None:
        declared = 65536

    if cblock.get(_LIMIT_KEY):
        lines.append(
            f"[ok] config: providers.consensus declares {_LIMIT_KEY} "
            f"({cblock[_LIMIT_KEY]}) - the merge is clamped there and can "
            f"never exceed it")
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    exceeded = [(job, max(cap, declared))
                for job, cap in _task_caps(raw, providers)
                if cap > declared]

    if not exceeded:
        lines.append(
            f"[ok] config: providers.consensus merges at or below its own "
            f"max_tokens ({declared}) - no {_LIMIT_KEY} needed")
    else:
        top = max(merged for _job, merged in exceeded)
        jobs = ", ".join(job for job, _merged in exceeded)
        lines.append(
            f"[info] config: providers.consensus will be sent up to {top} "
            f"tokens to merge {jobs} - above its own declared max_tokens "
            f"({declared}) - and declares no {_LIMIT_KEY}. That is supported "
            f"as long as your provider accepts {top}; if {declared} is your "
            f"provider's real limit, add \"{_LIMIT_KEY}\": <that limit> to the "
            f"consensus block so the merge is clamped there instead of "
            f"failing. Nothing was rewritten.")

    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)


def _one_block(value: object) -> dict | None:
    """The consensus job's single block, or None when it cannot be read.

    Exactly one block is the loaded-config invariant (config._normalize_
    providers rejects more), but this reads the RAW file, so tolerate the
    legacy shapes rather than raising: a bare dict, and absent."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    return None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _task_caps(raw: dict, providers: dict) -> list[tuple[str, int]]:
    """(job, task cap) for every job that fans out to more than one model.

    The ceiling a job passes down is translate_max_output_tokens for the
    translator and None for everything else, so for a non-translator job the
    synthesis cap is the consensus block's own declared max_tokens -- which
    can never exceed `declared` and so never needs reporting. Only the
    translator can raise it. Reported as the pairs so the line names the jobs
    that actually matter rather than every provider job in the file."""
    translator = _one_block_list(providers.get("translator"))
    if translator is None or len(translator) < 2:
        return []
    cap = _as_int(raw.get("translate_max_output_tokens"))
    return [("translator", cap)] if cap is not None else []


def _one_block_list(value: object) -> list | None:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(b, dict) for b in value):
        return value
    return None
