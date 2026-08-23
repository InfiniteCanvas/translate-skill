"""Translator's-note deduplication and cross-chapter history.

The pipeline generates candidate notes per chapter; this module first drops
the model's self-assessed low-comprehension notes (threshold "low"), then
drops invalid entries, collapses within-chapter duplicates, and suppresses
notes for terms that were already explained recently (the gap rule),
maintaining a persistent tn_history.json at the project root.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import project


def _history_path(project_dir: Path) -> Path:
    return Path(project.paths(project_dir)["tn_history"])


def load_history(project_dir: Path) -> dict:
    """Load tn_history.json ({} when missing or malformed)."""
    path = _history_path(project_dir)
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_history(project_dir: Path, h: dict) -> None:
    project.atomic_write_text(
        _history_path(project_dir), json.dumps(h, ensure_ascii=False, indent=2)
    )


def process(
    notes: list[dict],
    line_count: int,
    chapter_order: int,
    history: dict,
    gap: int,
) -> tuple[list[dict], dict, list[str]]:
    """Filter generated notes for one chapter.

    notes: [{"line": int (0-based), "term": str, "note": str,
    "threshold": str (optional)}] from the model.

    - Comprehension gate (first, before any other validation or dedup): drop
      every note whose "threshold" is "low" (case-insensitive), silently.
      This is the model's self-assessed comprehension-threshold gate --
      low-threshold notes are discarded by design. Notes missing the
      "threshold" key or carrying any other value are kept.
    - Drop invalid entries (line outside [0, line_count), empty term/note,
      wrong types) with a warning string.
    - Key = term.strip(). Within-chapter duplicates by key: keep the first
      (warning for the rest).
    - Gap rule: if key in history, was annotated in a DIFFERENT chapter, and
      0 <= chapter_order - last_order <= gap (an earlier chapter, at most
      `gap` chapters before this one) -> drop silently (expected behavior)
      and leave history unchanged. A previous annotation in this same chapter
      (retranslation/retry) or in a LATER chapter (retranslating an earlier
      chapter after a later one already annotated the term — negative
      distance; the reader hits the earlier chapter first) never suppresses
      the note. Otherwise keep the note and set history[key] = {"note",
      "last_order", "times": previous times + 1 or 1}.

    Returns (kept_notes, updated_history, warnings). The input history dict is
    not mutated; a shallow copy is returned.
    """
    warnings: list[str] = []
    kept: list[dict] = []
    seen: set[str] = set()
    updated = dict(history)

    # Comprehension gate (Hy-MT2 convention), before any other validation or
    # dedup: notes the model marked threshold="low" (case-insensitive) are
    # discarded silently, by design. Notes missing "threshold" or with any
    # other value are kept.
    notes = [
        entry for entry in notes
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("threshold"), str)
            and entry["threshold"].lower() == "low"
        )
    ]

    for idx, entry in enumerate(notes):
        position = f"note #{idx + 1}"
        if not isinstance(entry, dict):
            warnings.append(f"dropped {position}: not an object")
            continue
        line = entry.get("line")
        term = entry.get("term")
        note = entry.get("note")
        if isinstance(line, bool) or not isinstance(line, int):
            warnings.append(f"dropped {position}: 'line' must be an integer")
            continue
        if not isinstance(term, str) or not isinstance(note, str):
            warnings.append(f"dropped {position}: 'term' and 'note' must be strings")
            continue
        if line < 0 or line >= line_count:
            warnings.append(
                f"dropped {position} ('{term.strip() or '?'}'): "
                f"line {line} out of range [0, {line_count})"
            )
            continue
        if not term.strip() or not note.strip():
            warnings.append(f"dropped {position}: empty term or note")
            continue

        key = term.strip()
        if key in seen:
            warnings.append(f"dropped duplicate note for '{key}' within chapter")
            continue
        seen.add(key)

        prev = updated.get(key)
        last_order = prev.get("last_order") if isinstance(prev, dict) else None
        if (
            isinstance(last_order, int)
            and last_order != chapter_order
            and 0 <= chapter_order - last_order <= gap
        ):
            # Recently explained in an earlier chapter (positive distance at
            # most `gap`): drop without warning. last_order == chapter_order
            # means this very chapter is being retranslated — its own note
            # must be restored, not suppressed; last_order > chapter_order
            # (negative distance) means an earlier chapter is being
            # retranslated after a later one already annotated the term —
            # the reader hits the earlier chapter first, so keep the note.
            continue

        times = prev.get("times", 0) if isinstance(prev, dict) else 0
        updated[key] = {
            "note": note,
            "last_order": chapter_order,
            "times": (times if isinstance(times, int) else 0) + 1,
        }
        kept.append(entry)

    return kept, updated, warnings
