"""One-time style profile generation: sample random source chapters and ask
the model for a translation style summary plus novel background, stored in
novel_info.json as style_profile and injected into later prompts."""

from __future__ import annotations

import random
from pathlib import Path

from lib import client, config, logger, project
from lib.pipeline import LANG_NAMES, fill

PROFILE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "style_summary": {"type": "string"},
        "background": {"type": "string"},
    },
    "required": ["style_summary", "background"],
    "additionalProperties": False,
}


class ProfileError(Exception):
    """Style profile generation failed."""


def _lang_name(code: object) -> str:
    return LANG_NAMES.get(str(code).strip().lower(), str(code))


def generate_profile(
    project_dir: Path, cfg: dict, sample_chapters: int, sample_chars: int
) -> dict:
    """Return {"style_summary": str, "background": str} for this project.

    Samples up to sample_chapters random source chapters, concatenates their
    bodies until roughly sample_chars characters are collected (truncating the
    last one at a line boundary), fills templates/style_profile.md, and asks
    the "profile" provider. Raises ProfileError on any failure.
    """
    chapters = project.discover(project_dir)
    if not chapters:
        raise ProfileError("no source chapters found in source/")
    picked = random.sample(chapters, min(max(1, sample_chapters), len(chapters)))
    parts: list[str] = []
    total = 0
    for chapter in picked:
        _fm, body = project.read_chapter(chapter.path)
        text = body.strip()
        if not text:
            continue
        remaining = sample_chars - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            cut = text[:remaining]
            newline = cut.rfind("\n")
            if newline > remaining // 2:
                cut = cut[:newline]
            text = cut
        parts.append(text)
        total += len(text)
    sample_text = "\n\n".join(parts).strip()
    if not sample_text:
        raise ProfileError("sampled chapters contained no text")

    tpl_path = project.paths(project_dir)["templates"] / "style_profile.md"
    if not tpl_path.is_file():
        raise ProfileError(f"missing template: {tpl_path}")
    prompt = fill(
        tpl_path.read_text(encoding="utf-8"),
        {
            "source_lang": _lang_name(cfg.get("source_lang")),
            "target_lang": _lang_name(cfg.get("target_lang")),
            "sample_text": sample_text,
        },
        "style_profile.md",
    )
    def hook(meta: dict) -> None:
        if bool(cfg.get("log_llm", True)):
            logger.log_event(project_dir, {"job": "profile", **meta})

    resp = client.chat(
        config.provider(cfg, "profile"), prompt, json_schema=PROFILE_SCHEMA,
        meta_hook=hook,
    )
    data = client.extract_json(resp)
    if not isinstance(data, dict):
        raise ProfileError("profile response is not a JSON object")
    summary = data.get("style_summary")
    background = data.get("background")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(background, str)
        or not background.strip()
    ):
        raise ProfileError("profile response missing style_summary/background strings")
    return {"style_summary": summary.strip(), "background": background.strip()}
