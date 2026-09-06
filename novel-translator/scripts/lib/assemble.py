"""Assemble the final translated chapter markdown (epub-builder contract).

Output format: YAML frontmatter (source frontmatter plus the translated
title) and one body line per translated line -- clean markdown only.
Translator's notes are NOT baked into the file: they live in the
per-chapter sidecar notes/<stem>.json (written by the caller via
tn.save_notes), which the epub builder renders as epub3 footnotes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lib import project


def assemble(
    out_path: Path,
    frontmatter: dict,
    translated_title: str,
    lines: list[str],
) -> None:
    """Write the final translated chapter markdown to out_path.

    - Frontmatter: source frontmatter + "title" = translated_title.
    - Body: one line per translated line, nothing else. No footnote
      markers, no notes section -- the notes sidecar (tn.save_notes)
      carries them alongside the file.
    """
    fm: dict[str, Any] = dict(frontmatter)
    fm["title"] = translated_title
    out_path.parent.mkdir(parents=True, exist_ok=True)
    project.write_chapter(out_path, fm, "\n".join(lines))
