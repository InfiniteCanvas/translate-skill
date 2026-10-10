"""v12: decouple translate_max_output_tokens from provider max_tokens.

No config key is added, removed, renamed or re-keyed, and no prompt template
changes -- so by the letter of AGENTS.md's rule this step is not required. It
ships anyway, because the rule's own precedent (v009) covers value REWRITES,
and because the maintainer wants the upgrade to leave an explicit record.

What changed in behavior:

    Before: client.chat sent `max_tokens or provider_cfg["max_tokens"]`, so
    translate_max_output_tokens silently OVERRODE every translator block. A
    project whose ceiling sat above a block's real limit packed chapters to
    the ceiling and sent them to a provider that returns less -- silent
    truncation, then a corrective retry at the same oversized cap.

    After: translate_max_output_tokens is a CEILING. client.chat clamps the
    sent cap down to each block's own max_tokens (per block), and pipeline
    packs to min(ceiling, tightest block) so no model in the array truncates
    its part.

A project left with a mismatched pair is no longer broken -- the block wins
and the chapter still translates -- but its CHAPTER PACKING changes the moment
this lands: it re-packs to the tightest block instead of the ceiling. A
chapter that was mid-translation fails its crash-resume line-count check and
retranslates from scratch (pipeline's packing-drift warn). This step exists so
that is a planned, logged event with an explanation attached, not a surprise
the first time someone resumes a batch.

The report line names what changed and what to do about it, because that is
what the maintainer runs the migration for:

    [warn] config: translate_max_output_tokens (N) exceeds the tightest
    providers.translator block (M) - the block now wins as a hard ceiling and
    chapters pack to M instead of N; per-chunk call caps drop to
    floor(0.8 * M) - 256 characters. Raise the block's max_tokens if M was
    below what you need, or set translate_max_output_tokens to M to make the
    effective cap explicit. Nothing is rewritten on disk.

Deliberately NOT rewritten: unlike v009 this step has no old-default sentinel.
There is no value that is "wrong" now that was "right" before -- the pair is
merely no longer equivalent in meaning, and either side is a legitimate
configuration. Rewriting a user's chosen numbers because their RELATIONSHIP
changed would be exactly the kind of silent edit v009's docstring warns
against. So this step is read-only against config.json and reports; it does
not touch values.

Idempotent by construction: it reads, reports, and writes nothing. A second
call reports the same lines.

No templates change, so sync_templates stays delegated to standard_step and
is silent on an up-to-date project.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 12
DESCRIPTION = ("decouple translate_max_output_tokens from provider "
               "max_tokens (the key is now a ceiling a block can lower, not "
               "an override; report mismatched translator pairs)")

_MIN_USABLE = 8192


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    cfg_path = Path(project_dir) / "config.json"
    lines: list[str] = []

    try:
        raw = _read(cfg_path)
    except (OSError, ValueError) as exc:
        lines.append(f"[warn] config: config.json not readable ({exc}) - "
                     "skipped the translator cap check")
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    max_out = raw.get("translate_max_output_tokens")
    providers = raw.get("providers")
    blocks = providers.get("translator") if isinstance(providers, dict) else None
    items = blocks if isinstance(blocks, list) else ([blocks] if isinstance(blocks, dict) else [])
    caps = [int(b["max_tokens"]) for b in items
            if isinstance(b, dict) and isinstance(b.get("max_tokens"), int)]

    if not isinstance(max_out, int) or not caps:
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    tightest = min(caps)
    if tightest < _MIN_USABLE:
        lines.append(
            f"[warn] config: the tightest providers.translator block "
            f"({tightest}) is below {_MIN_USABLE} - chapters cannot be packed "
            f"into it at all, so this project cannot translate until you raise "
            f"it to at least {_MIN_USABLE}.")
    elif tightest < max_out:
        room = max(tightest * 4 // 5 - 256, 0)
        lines.append(
            f"[warn] config: translate_max_output_tokens ({max_out}) exceeds "
            f"the tightest providers.translator block ({tightest}) - the "
            f"block now wins as a hard ceiling and chapters pack to "
            f"{tightest} instead of {max_out}; each part targets about {room} "
            f"characters. Raise that block's max_tokens if {tightest} is below "
            f"what you need, or set translate_max_output_tokens to {tightest} "
            f"to make the effective cap explicit. Nothing was rewritten; a "
            f"chapter already mid-translation will re-pack and restart.")
    else:
        lines.append(
            f"[ok] config: translator caps agree - translate_max_output_tokens "
            f"({max_out}) is at or below the tightest block ({tightest}); the "
            f"ceiling is the effective cap.")

    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)


def _read(path: Path) -> dict:
    import json

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data
