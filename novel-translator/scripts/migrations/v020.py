"""v20: a provider block can declare its wire protocol (`protocol`).

No config key is added, removed, renamed or re-keyed at the top level, no
DEFAULTS or PROVIDER_DEFAULTS entry changes, and no prompt template changes --
so by the letter of AGENTS.md's rule this step is not required. It ships on
v015's precedent of leaving an upgrade record for a change in MEANING: the
client now speaks two wire protocols, and a block's `protocol` field -- when
present -- picks which one. Absent still means "openai", so every existing
project loads and behaves byte-identically; nothing on disk is wrong that was
right before.

It is deliberately NOT materializing the key (v013/v015 precedent): a fresh
config would carry eight dead `"protocol": "openai"` blocks duplicating what
absence already means, and the anthropic decision belongs to whoever points a
block at an Anthropic-compatible endpoint.

What it DOES report is which blocks in the PROJECT's config.json DECLARE
which protocol -- a raw-file scan, like every migration's read (layers 0 and
2 only, so a protocol set only in the skill's config.local.json is layer 1
and not visible here; no CLI command prints a block's resolved protocol) --
because that is the one fact the operator cannot see without reading the
config, and it is the fact this step introduces. Note `migrate` loads the
config BEFORE any step runs, so a config the load refuses (an unknown or a
present-null protocol) never reaches this step through the CLI: the refusal
names the block itself. The [FAIL] branch below is the step's own belt for
direct module use and a chain run without the pre-flight:

    [ok] config: no provider block in config.json declares a protocol
    (5 block(s) authored). Nothing was rewritten. (A protocol set in the
    skill's config.local.json is layer 1 and not visible to this report.)

or, where blocks declare it:

    [ok] config: 2 provider block(s) in config.json declare a protocol -
    translator[1], consensus[0] declare "anthropic"; the other 5 block(s)
    declare none. Nothing was rewritten.

and, where a block declares something the client cannot speak (the got
value is the JSON literal the config carries):

    [FAIL] config: providers.translator[0] protocol must be "openai" or
    "anthropic" (got "gRPC"). Nothing was rewritten.

Idempotent by construction: it reads, reports, writes nothing.

No templates change, so sync_templates stays delegated to common and is
silent on an up-to-date project.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from lib import config

from . import common

VERSION = 20
DESCRIPTION = ("a provider block can declare its wire protocol: `protocol` "
               "picks the openai (default) or anthropic SDK per block; "
               "report which protocol each project block declares")


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
                     "skipped the protocol report")
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    CAVEAT_LINE = ("(A protocol set in the skill's config.local.json is layer "
                   "1 and not visible to this report.)")
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        # a non-dict providers is refused by a normal load (migrate's own
        # pre-flight with require_base_url=False tolerates it); the report
        # simply has nothing to scan
        lines.append(
            f"[ok] config: config.json's providers is {type(providers).__name__}, "
            f"not a JSON object - no protocol declarations to report (a normal "
            f"run refuses this config). Nothing was rewritten.")
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    declared: list[str] = []
    invalid: list[str] = []
    total = 0
    for job in config.PROVIDER_JOBS:
        blocks = _blocks(providers.get(job))
        if blocks is None:
            continue
        total += len(blocks)
        _collect(job, blocks, declared, invalid)
    for key in providers:
        if not key.startswith(config.CONSENSUS_PREFIX) or key == config.CONSENSUS_PREFIX:
            continue
        blocks = _blocks(providers.get(key))
        if blocks is None:
            continue
        total += len(blocks)
        _collect(key, blocks, declared, invalid)

    if invalid:
        for name, got in invalid:
            literal = json.dumps(got)
            lines.append(
                f"[FAIL] config: providers.{name} protocol must be \"openai\" or "
                f"\"anthropic\" (got {literal}). A run will refuse to start until "
                f"the field is fixed or removed. Nothing was rewritten.")
    elif declared:
        names = ", ".join(name for name, _p in declared)
        protocols = sorted({p for _n, p in declared})
        others = total - len(declared)
        quoted = " and ".join(f'"{p}"' for p in protocols)
        lines.append(
            f"[ok] config: {len(declared)} provider block(s) in config.json "
            f"declare a protocol - {names} declare {quoted}; the other {others} "
            f"block(s) declare none. Nothing was rewritten. "
            + CAVEAT_LINE)
    else:
        lines.append(
            f"[ok] config: no provider block in config.json declares a protocol "
            f"({total} block(s) authored). Nothing was rewritten. "
            + CAVEAT_LINE)

    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)


def _collect(job: str, blocks: list, declared: list, invalid: list) -> None:
    for i, block in enumerate(blocks):
        name = f"{job}[{i}]" if len(blocks) > 1 else f"{job}[0]"
        if "protocol" not in block:
            continue
        protocol = block["protocol"]
        if protocol in config.PROTOCOLS:
            declared.append((name, protocol))
        else:
            invalid.append((name, protocol))


def _blocks(value: object) -> list | None:
    """A job's blocks as a list, or None when it is absent/unsupported.

    Tolerates the legacy bare-dict shape, because this reads the RAW file.
    """
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(b, dict) for b in value):
        return value
    return None
