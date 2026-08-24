"""Assemble the final translated chapter markdown (epub-builder contract).

Output format: YAML frontmatter (source frontmatter plus the translated
title), one body line per translated line with Markdown footnote markers for
translator's notes, and a "## Translator's Notes" section at the end.
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
    notes: list[dict],
) -> None:
    """Write the final translated chapter markdown to out_path.

    - Frontmatter: source frontmatter + "title" = translated_title.
    - Body: one line per translated line; note n (1-based position in notes)
      appends "[^n]" to the end of lines[note["line"]] (after rstripping that
      line). Multiple notes on one line stack as "[^1][^2]".
    - If notes: blank line, "## Translator's Notes", blank line, then one
      "[^n]: **{term}** — {note}" definition per note.
    """
    fm: dict[str, Any] = dict(frontmatter)
    fm["title"] = translated_title

    # Keep only notes whose line index is valid for this chapter.
    valid_notes: list[dict] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        idx = note.get("line")
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if 0 <= idx < len(lines):
            valid_notes.append(note)

    body_lines = list(lines)
    for pos, note in enumerate(valid_notes, start=1):
        idx = note["line"]
        if not body_lines[idx].strip():
            # A marker alone on an empty line reads as a defect: roll it to
            # the nearest following non-empty line (falling back to the
            # nearest preceding one) so it attaches to real text.
            target = next((j for j in range(idx + 1, len(body_lines)) if body_lines[j].strip()),
                          None)
            if target is None:
                target = next((j for j in range(idx - 1, -1, -1) if body_lines[j].strip()), idx)
            idx = target
        body_lines[idx] = body_lines[idx].rstrip() + f"[^{pos}]"

    sections = ["\n".join(body_lines)]
    if valid_notes:
        note_lines = []
        for pos, note in enumerate(valid_notes, start=1):
            term = str(note.get("term", "")).strip()
            text = str(note.get("note", ""))
            note_lines.append(f"[^{pos}]: **{term}** \u2014 {text}")
        sections.append("## Translator's Notes")
        sections.append("\n".join(note_lines))

    body = "\n\n".join(sections)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    project.write_chapter(out_path, fm, body)
