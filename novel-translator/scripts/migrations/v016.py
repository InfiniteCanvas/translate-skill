"""v16: the v2 config layering -- hand a project back its authored keys.

Under the layering model (references/file-formats.md, "Overlay layers") a
project's config.json holds only what a human put there; the code defaults and
the machine's config.local.json are consulted at READ time and are never
written into the project. Every project written before this step was written
the other way: init copied DEFAULTS into config.json and apply_overlay copied
the overlay's endpoint, model and auth into it, so each file carries a frozen
snapshot of both. This step unwrites that snapshot, so a shipped default
changes and an overlay edit reaches the project again.

Three classes of key are handled, and the difference between them is the whole
point of the step:

  1. A provider block whose `base_url` byte-equals the RETIRED default
     (config.RETIRED_BASE_URL, the private LAN address PROVIDER_DEFAULTS used
     to ship) is stripped and reported as [warn]. This is the one key that
     cannot follow the v009 value-identity rule, which is what the other two
     classes use. v009's sentinel is safe because the value it strips still
     resolves through layer 0; base_url is the one field layer 0 no longer
     has, so stripping it on value-identity would leave the job with NO
     endpoint at all and fail at the first call. It is stripped only where it
     still carries the retired address -- a project whose base_url is its own
     (or its overlay's) keeps it, because that value was authored and layer 0
     cannot supply a replacement.

  2. Every other provider key equal to THAT JOB's PROVIDER_DEFAULTS value is
     stripped, per job and not against one flat dict: _with_defaults fills a
     job-specific key set (translator has top_p, the rest do not), so a
     sentinel compared against the wrong job's defaults would either miss a
     key or strip one the job never inherited.

  3. Every top-level key equal to a DEFAULTS value is stripped.

Classes 2 and 3 can only lose a value that resolves identically through
layer 0, so they are safe -- with one consequence worth stating: when the
project's frozen default and the machine's overlay disagree, stripping hands
the key to the overlay, which is the point. A project that had never touched
the key should follow the machine, not freeze layer 1's old snapshot.

Every stripped key is named in the report. Idempotent: the second run finds
the defaults gone and the endpoint gone, matches nothing, and reports only the
template sync.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from lib import config

from . import common

VERSION = 16
DESCRIPTION = ("v2 config layering: strip the defaults and overlay values "
               "materialized into config.json, and the retired default "
               "endpoint")


def _is_default(value: object, default: object) -> bool:
    """True when value is indistinguishable from the shipped default.

    The bool guard matters because True == 1 and False == 0 in Python: a
    project that authored the number 1 for a boolean key did not leave that
    key on its default, and must not have it stripped."""
    return (isinstance(value, bool) == isinstance(default, bool)
            and value == default)


def _job_defaults(job: str) -> dict:
    """The defaults a job's block is compared against. A per-job arbitrator
    (`consensus_<job>`) resolves onto the consensus defaults at read time, so
    it is compared against those; an unknown job is compared against nothing,
    because nothing fills it and stripping one of its keys could lose a value
    that has no other source."""
    if job in config.PROVIDER_DEFAULTS:
        return config.PROVIDER_DEFAULTS[job]
    if job.startswith(config.CONSENSUS_PREFIX):
        name = job[len(config.CONSENSUS_PREFIX):]
        if name in config.CONSENSUS_JOBS:
            return config.PROVIDER_DEFAULTS["consensus"]
    return {}


def _strip_defaults(raw: dict) -> list[str]:
    """Drop every top-level key that still holds its DEFAULTS value."""
    stripped: list[str] = []
    for key in list(raw):
        if key in config.DEFAULTS and _is_default(raw[key], config.DEFAULTS[key]):
            del raw[key]
            stripped.append(key)
    return stripped


def _strip_providers(providers: dict) -> tuple[list[str], list[str]]:
    """Drop default-valued provider keys and the retired endpoint, in place.
    Returns ([warn] lines for stripped endpoints, [ok] lines for the rest)."""
    warns: list[str] = []
    oks: list[str] = []
    for job in sorted(providers):
        value = providers[job]
        blocks = value if isinstance(value, list) else [value]
        if not all(isinstance(block, dict) for block in blocks):
            continue
        defaults = _job_defaults(job)
        retired = [i for i, block in enumerate(blocks)
                   if block.get("base_url") == config.RETIRED_BASE_URL]
        for index in retired:
            del blocks[index]["base_url"]
        if retired:
            if len(blocks) > 1:
                named = ", ".join(f"providers.{job}[{index}].base_url"
                                  for index in retired)
            else:
                named = f"providers.{job}.base_url"
            warns.append(
                f"[warn] {named} was the retired default "
                f"{config.RETIRED_BASE_URL}; set it in config.local.json or "
                "the project")
        stripped: list[str] = []
        for block in blocks:
            for key in list(block):
                if key == "base_url":
                    continue
                if key in defaults and _is_default(block[key], defaults[key]):
                    del block[key]
                    stripped.append(key)
        if stripped:
            oks.append(
                f"[ok] config: providers.{job}: stripped {len(stripped)} "
                "key(s) that resolve through the shipped defaults: "
                + ", ".join(sorted(set(stripped))))
    return warns, oks


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    cfg_path = Path(project_dir) / "config.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path} must contain a JSON object")

    lines: list[str] = []
    stripped_top = _strip_defaults(raw)
    if stripped_top:
        lines.append(
            f"[ok] config: stripped {len(stripped_top)} key(s) that resolve "
            "through the shipped defaults: " + ", ".join(stripped_top))
    providers = raw.get("providers")
    if isinstance(providers, dict):
        warns, oks = _strip_providers(providers)
        lines.extend(warns)
        lines.extend(oks)

    if lines and not dry_run:
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")

    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)