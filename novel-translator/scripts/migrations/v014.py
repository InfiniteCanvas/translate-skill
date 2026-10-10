"""v14: a provider failure that survives its retries is now FATAL, and Z.AI's
irrecoverable error codes skip the retry ladder entirely.

No config key is added, removed, renamed or re-keyed, and no prompt template
changes -- so by the letter of AGENTS.md's rule this step is not required. It
ships anyway on v009's precedent of leaving an upgrade record for a change in
MEANING, because this one changes what a run does when the provider is wrong.

    Before: four consensus paths absorbed a failed call. A candidate that failed
    was a [warn] and the run continued on the survivor; a single survivor was
    logged as `degraded`; a failed synthesis was logged as `degraded` and
    answered with candidate 1's text verbatim. All of it quiet -- one [warn]
    line and a tier-1 event nobody reads during a batch. The chapter looked
    translated and the multi-model merge the project configured had never run.

    After: every one of those paths stops the run. client.LLMFatal (a member of
    ZAI_FATAL_CODES -- auth, balance, invalid parameter, filtered content,
    expired plan) is raised on the FIRST response, with no retry and no
    backoff sleep. Anything else that exhausts client._MAX_ATTEMPTS raises
    client.LLMError, which is equally fatal. Both reach translate.main() as one
    `[FAIL]` line and exit 3.

Nothing is rewritten. There is no old-default sentinel -- no value on disk is
wrong that was right before -- and no config edit can express "this provider
will be down", which is the failure this policy is about.

What it DOES report is the one thing observable from the file and the
environment that will now stop a run at the first call instead of degrading:
a provider block with no usable credential.

    [warn] config: providers.translator[0] (glm-5.3 at api.z.ai) has no usable
    credential - set "api_key_env" to an environment variable that is set, or
    "api_key" inline. As of v14 a provider call that fails after its retries
    stops the run (exit 3) instead of degrading quietly. Nothing was rewritten.

That is a [warn], not a [FAIL]: an unset environment variable in a shell that
is not the one that will run the translate is common and harmless until it is
not, and the step must not turn a recoverable setup mistake into a refusal to
migrate. The `[FAIL]` is reserved for a block that names NO credential source
at all, which cannot become valid without editing the file.

Idempotent by construction: it reads, reports, writes nothing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 14
DESCRIPTION = ("a provider failure that exhausts its retries is now fatal (exit 3) "
               "instead of degrading quietly, and Z.AI irrecoverable codes skip the "
               "retry ladder; report provider blocks with no usable credential")

_JOBS = ("translator", "glossary", "reviewer", "annotator", "recap",
         "profile", "consensus")


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
                     "skipped the credential check")
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return lines + common.sync_templates(
            project_dir, templates_src, dry_run, force, confirm)

    unusable: list[str] = []
    absent: list[str] = []
    for job in _JOBS:
        blocks = _blocks(providers.get(job))
        if blocks is None:
            continue
        for i, block in enumerate(blocks):
            label = f"providers.{job}" + (f"[{i}]" if len(blocks) > 1 else "")
            model = str(block.get("model") or "?")
            base = str(block.get("base_url") or "?")
            where = f"{label} ({model} at {base})"
            if block.get("api_key"):
                continue
            env = block.get("api_key_env")
            if isinstance(env, str) and env.strip():
                if not os.environ.get(env.strip()):
                    absent.append(f"{where}: {env.strip()} is not set")
            else:
                unusable.append(where)

    if unusable:
        lines.append(
            f"[FAIL] config: provider blocks with no credential at all - "
            f"{'; '.join(unusable)}. As of v14 a provider call that fails after "
            f"its retries stops the run (exit 3) rather than degrading quietly, "
            f"so an unusable key fails the whole run instead of one chapter. "
            f"Nothing was rewritten - set \"api_key_env\" or \"api_key\".")
    if absent:
        lines.append(
            f"[warn] config: {len(absent)} provider block(s) name an "
            f"api_key_env that is not set in this shell - "
            f"{'; '.join(absent)}. Fine if you run translate from a shell that "
            f"has it; as of v14 an unusable credential stops the run (exit 3) "
            f"rather than degrading quietly. Nothing was rewritten.")

    if not unusable and not absent:
        lines.append(
            f"[ok] config: every provider block resolves a credential - as of "
            f"v14 a failed call is fatal, so this is what a run depends on")

    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)


def _blocks(value: object) -> list | None:
    """A job's blocks as a list, or None when it is absent/unsupported."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(b, dict) for b in value):
        return value
    return None
