"""v9: raise the translation output budget to 64k.

Both numbers move together, and they have to:

  DEFAULTS["translate_max_output_tokens"]   8192  -> 65536
  config.DEFAULT_MAX_TOKENS                 16384 -> 65536   (every job's
                                                             max_tokens default)

A project that carried the OLD pair is rewritten to the NEW pair. Moving only
one would be actively harmful: pipeline._escalated_cap clamps the corrective
retry for a truncated chunk to the smallest translator max_tokens, so a
project left at max_tokens=16384 with translate_max_output_tokens=65536 would
re-send the retry SMALLER than the attempt it is retrying -- guaranteeing the
same truncation and burning a full attempt for nothing.

Values are rewritten only when they EXACTLY equal the old default, the same
"untouched default" rule the rest of this chain uses: a project sitting on
some other number chose that number on purpose and is left alone. That means
a user who genuinely wanted 8192 cannot be distinguished from one who never
touched it, and their 8192 is bumped -- the deliberate choice is recoverable
by setting it back, and the next migrate is a no-op.

No templates change, so sync_templates stays delegated to standard_step and
is silent on an up-to-date project. Idempotent: the second call matches the
NEW default, the rewrite matches nothing, and migrate() reports [].
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import common

VERSION = 9
DESCRIPTION = ("raise the translation output budget to 64k "
                "(translate_max_output_tokens 8192->65536 and provider "
                "max_tokens 16384->65536, together)")

OLD_TRANSLATE_CAP = 8192
OLD_MAX_TOKENS = 16384


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    cfg_path = Path(project_dir) / "config.json"
    raw = _read(cfg_path)
    lines: list[str] = []

    if raw.get("translate_max_output_tokens") == OLD_TRANSLATE_CAP:
        raw["translate_max_output_tokens"] = 65536
        lines.append("[ok] config: translate_max_output_tokens "
                     f"{OLD_TRANSLATE_CAP} -> 65536 (64k headroom for "
                     "reasoning models; raise your own max_tokens to match)")
    elif raw.get("translate_max_output_tokens") == 65536:
        pass

    blocks_changed: list[str] = []
    providers = raw.get("providers")
    if isinstance(providers, dict):
        for job, blocks in sorted(providers.items()):
            items = blocks if isinstance(blocks, list) else [blocks]
            if not all(isinstance(b, dict) for b in items):
                continue
            if any(b.get("max_tokens") == OLD_MAX_TOKENS for b in items):
                blocks_changed.append(job)
                for b in items:
                    if b.get("max_tokens") == OLD_MAX_TOKENS:
                        b["max_tokens"] = 65536
    if blocks_changed:
        lines.append("[ok] config: provider max_tokens "
                     f"{OLD_MAX_TOKENS} -> 65536 on "
                     + ", ".join(blocks_changed)
                     + " (keeps the truncation retry above the first "
                       "attempt's cap)")

    if lines and not dry_run:
        _save(cfg_path, raw)

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
