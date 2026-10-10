"""v15: a fan-out job can name its OWN arbitrator (`providers.consensus_<job>`).

No config key is added, removed, renamed or re-keyed at the top level, no
DEFAULTS or PROVIDER_DEFAULTS entry changes, and no prompt template changes --
so by the letter of AGENTS.md's rule this step is not required. It ships anyway
on v009's precedent of leaving an upgrade record for a change in MEANING, and
on v008's: a new authorable key INSIDE `providers` is a schema change even
though nothing above `providers` moved.

    Before: one arbitrator for the whole pipeline. Every merge -- translator,
    annotator, glossary, recap, profile, reviewer -- went to
    `providers.consensus`. So the merge model had to be a single compromise:
    strong enough for the translation merge and cheap enough for the notes
    merge, which is neither.

    After: `providers.consensus_<job>` overrides the global arbitrator for that
    one job's merge. An ABSENT key resolves to `providers.consensus` exactly as
    before, so every existing project is unaffected. A partial block inherits
    the global arbitrator's endpoint and auth and overrides only the keys it
    names, so `{"model": "x"}` keeps a working provider pointed at itself.

Nothing is rewritten. There is no old-default sentinel -- no value on disk is
wrong that was right before -- and the migration cannot know which model anyone
wants to merge with, which is the whole decision this step enables. It is also
deliberately NOT materializing the keys: a fresh config would carry six dead
blocks duplicating the global's base_url and auth, and changing the global
later would leave stale copies silently winning for the jobs that had them.

What it DOES report is the resolution map -- which arbitrator each fan-out job
will actually merge through -- because that is the one fact the operator cannot
see without reading the config, and it is the fact this step introduces:

    [ok] config: 2 fan-out job(s) merge through providers.consensus -
    translator, annotator. Set providers.consensus_<job> to arbitrate one
    merge with its own model (v015).

and, only where it can be seen from the file alone, v013's check applied to the
arbitrator that will ACTUALLY be used rather than to the global one:

    [info] config: providers.consensus_translator will be sent up to 256000
    tokens to merge translator - above its own declared max_tokens (128000) -
    and declares no max_tokens_limit. ...

Idempotent by construction: it reads, reports, writes nothing.

No templates change, so sync_templates stays delegated to common and is silent
on an up-to-date project.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 15
DESCRIPTION = ("a fan-out job can name its own arbitrator: providers.consensus_<job> "
               "overrides providers.consensus for that job's merge; report which "
               "arbitrator each fan-out job resolves to")

# The jobs that can actually fan out. `consensus` is excluded because it is the
# arbitrator itself: it is always exactly one block, so it never merges
# candidates and never has an arbitrator of its own.
_JOBS = ("translator", "glossary", "reviewer", "annotator", "recap", "profile")

_LIMIT_KEY = "max_tokens_limit"


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    # Read the RAW file, as v012/v013/v014 do: load_config fills the inherited
    # blocks in, and the question here is what the author actually wrote.
    cfg_path = Path(project_dir) / "config.json"
    lines: list[str] = []

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError(f"{cfg_path} must contain a JSON object")
    except (OSError, ValueError) as exc:
        lines.append(f"[warn] config: config.json not readable ({exc}) - "
                     "skipped the consensus routing check")
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    translator = _blocks(providers.get("translator"))
    fanout: list[tuple[str, str]] = []
    for job in _JOBS:
        # The real inheritance: an omitted non-translator job inherits the
        # translator's WHOLE array, so it fans out too even though the file
        # never names it. Mirroring that here is what keeps the report honest.
        blocks = translator if job == "translator" else (
            _blocks(providers.get(job)) if providers.get(job) is not None
            else translator)
        if blocks is None or len(blocks) < 2:
            continue
        fanout.append((job, _arbitrator_key(providers, job)))

    if not fanout:
        lines.append("[ok] config: no job fans out to more than one model, so no "
                     "consensus merge runs and no arbitrator is needed")
    elif "consensus" not in providers:
        # The blocking condition. A fanning-out config with no authored
        # arbitrator is now REFUSED at load, so this migration is predicting a
        # run that will not start -- which is why it is a [FAIL], not the
        # [info]/[ok] the pure routing map below uses. It cannot repair this:
        # writing `consensus` from translator[0] would re-create on disk the
        # exact inference this rule forbids, silently and with nothing to
        # explain it.
        jobs = ", ".join(job for job, _key in fanout)
        lines.append(
            f"[FAIL] config: providers.consensus is not set but {len(fanout)} "
            f"job(s) fan out to more than one model ({jobs}). As of v015 a run "
            f"refuses to start until you add providers.consensus explicitly, or "
            f"give those jobs a single provider block. Nothing was rewritten.")
    else:
        dedicated = [job for job, key in fanout if key != "consensus"]
        via_global = sorted(job for job, key in fanout if key == "consensus")
        if dedicated:
            lines.append(
                f"[ok] config: {len(fanout)} fan-out job(s) merge through a "
                f"dedicated arbitrator as of v015 - " +
                ", ".join(f"{job} via providers.{_arbitrator_key(providers, job)}"
                          for job in dedicated) +
                f". Other fan-out job(s) ({', '.join(via_global) or 'none'}) "
                f"still merge through providers.consensus. Nothing was rewritten.")
        else:
            lines.append(
                f"[ok] config: {len(fanout)} fan-out job(s) - "
                f"{', '.join(via_global)} - merge through providers.consensus. "
                f"As of v015 each can be given its own arbitrator by adding "
                f"providers.consensus_<job> (a partial block keeps the global "
                f"one's endpoint and auth). Nothing was rewritten.")

    lines.extend(_cap_notes(raw, providers, fanout))
    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)


def _arbitrator_key(providers: dict, job: str) -> str:
    """The providers key whose block will arbitrate `job`'s merge: its own
    `consensus_<job>` when authored, else the global `consensus`."""
    own = f"consensus_{job}"
    return own if providers.get(own) is not None else "consensus"


def _cap_notes(raw: dict, providers: dict,
               fanout: list[tuple[str, str]]) -> list[str]:
    """v013's check, aimed at the arbitrator each job will ACTUALLY use.

    Only the translator can push a synthesis cap above the arbitrator's own
    declared max_tokens: the pipeline hands it translate_max_output_tokens,
    while every other job passes no task cap, so their merge cap IS the
    arbitrator's declared number and can never exceed it. So this can produce at
    most one note, and only about the translator's arbitrator.
    """
    out: list[str] = []
    translator = next((key for job, key in fanout if job == "translator"), None)
    if translator is None:
        return out

    block = _one_block(providers.get(translator))
    if block is None:
        return out
    # An unset max_tokens fills in as DEFAULT_MAX_TOKENS at load time, which the
    # comparison below still evaluates correctly -- report it as that number
    # rather than staying silent about a limit the runtime will apply.
    declared = _as_int(block.get("max_tokens"))
    if declared is None:
        declared = 65536
    if block.get(_LIMIT_KEY):
        out.append(
            f"[ok] config: providers.{translator} declares {_LIMIT_KEY} "
            f"({block[_LIMIT_KEY]}) - the translator merge is clamped there and "
            f"can never exceed it")
        return out

    task_cap = _as_int(raw.get("translate_max_output_tokens"))
    if task_cap is None or task_cap <= declared:
        out.append(
            f"[ok] config: providers.{translator} merges at or below its own "
            f"max_tokens ({declared}) - no {_LIMIT_KEY} needed")
        return out

    out.append(
        f"[info] config: providers.{translator} will be sent up to {task_cap} "
        f"tokens to merge translator candidates - above its own declared "
        f"max_tokens ({declared}) - and declares no {_LIMIT_KEY}. That is "
        f"supported as long as your provider accepts {task_cap}; if {declared} "
        f"is your provider's real limit, add \"{_LIMIT_KEY}\": <that limit> to "
        f"the block so the merge is clamped there instead of failing (exit 3). "
        f"Nothing was rewritten.")
    return out


def _blocks(value: object) -> list | None:
    """A job's blocks as a list, or None when it is absent/unsupported.

    Tolerates the legacy bare-dict shape, because this reads the RAW file.
    """
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(b, dict) for b in value):
        return value
    return None


def _one_block(value: object) -> dict | None:
    """The arbitrator's single block, or None when it cannot be read."""
    blocks = _blocks(value)
    return blocks[0] if blocks and len(blocks) == 1 else None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None