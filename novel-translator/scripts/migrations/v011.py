"""v11: the two-tier log layout -- three new keys, retention 5 -> 10.

  log_orchestration        (new, true)  tier-1 structural events
  log_prompt_bodies        (new, true)  the prompt/response text itself
  log_chapter_keep_runs    (new, 3)     per-chapter tier-2 retention
  log_llm_keep_runs        5      -> 10 tier-1 + project-bucket retention

The three new keys are added by folding config.DEFAULTS in, the ordinary way.

log_llm_keep_runs is a different job and is rewritten in the raw file, on
v009's "untouched default" rule: a project sitting on a number other than 5
chose it on purpose and is left alone. This rewrite is not optional polish --
materialize_config folds ALL of DEFAULTS into config.json on every init and
migrate, so with the old default at 5 essentially every existing project
already carries an explicit 5, and a bare standard_step would preserve it and
deliver this bump to new projects only.

The same ambiguity v009 accepted applies to this number: a user who genuinely
wanted exactly 5 cannot be distinguished from one who never touched it, and
their 5 is bumped. The choice is recoverable by setting it back, and the next
migrate is a no-op.

Permanent-baking consequence, stated for the record: because materialize_config
writes the entire merged form, after this step every project carries
"log_llm_keep_runs": 10 as an explicit literal that no longer tracks DEFAULTS.
That is the same trade-off v009 makes and documents.

No templates change, so sync_templates stays delegated to the shared helper
and is silent on an up-to-date project. Idempotent: the second call matches
the new default, the rewrite matches nothing, and migrate() reports [].
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 11
DESCRIPTION = ("two-tier log layout (new log_orchestration, log_prompt_bodies, "
                "log_chapter_keep_runs; log_llm_keep_runs 5->10)")

OLD_KEEP_RUNS = 5
NEW_KEEP_RUNS = 10


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    cfg_path = Path(project_dir) / "config.json"
    raw = _read(cfg_path)
    lines: list[str] = []

    if raw.get("log_llm_keep_runs") == OLD_KEEP_RUNS:
        raw["log_llm_keep_runs"] = NEW_KEEP_RUNS
        lines.append("[ok] config: log_llm_keep_runs "
                     f"{OLD_KEEP_RUNS} -> {NEW_KEEP_RUNS} (orchestration "
                     "runs; each chapter keeps its own "
                     "log_chapter_keep_runs)")
        if not dry_run:
            _save(cfg_path, raw)
    elif raw.get("log_llm_keep_runs") == NEW_KEEP_RUNS:
        pass

    lines += common.materialize_config(project_dir, dry_run)
    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)


def _read(path: Path) -> dict:
    import json

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _save(path: Path, raw: dict) -> None:
    import json

    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
