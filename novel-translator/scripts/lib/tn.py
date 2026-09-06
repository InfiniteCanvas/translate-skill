"""Translator's-note deduplication, cross-chapter history, and sidecar IO.

The pipeline generates candidate notes per chapter; this module drops the
model's self-assessed low-comprehension notes (threshold "low") unless
cfg tn_keep_low_confidence is set, then drops invalid entries, collapses
within-chapter duplicates, and suppresses notes for terms that were
already explained recently (the gap rule), maintaining a persistent
tn_history.json at the project root.

Notes are no longer baked into chapter markdown: they live in a
per-chapter sidecar notes/<stem>.json (load_notes/save_notes), whose line
indexes refer to the translated body lines and whose anchors let the epub
builder re-resolve lines after hand-edits. strip_marked_notes migrates
legacy chapters whose notes were baked in as "[^N]" markers plus a
"## Translator's Notes" section.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

try:  # package-style import when scripts/lib is imported as a package
    from . import project
except ImportError:  # flat import when scripts/lib is on sys.path
    import project

# Legacy baked-in format (epub.py's chapter parser uses the same rules):
# markers "[^N]" appended to body lines, definitions "[^N]: **term** — note"
# under a "## Translator's Notes" heading.
_TN_HEADING = "## Translator's Notes"
_MARKER_RE = re.compile(r"\[\^\d+\]")
_MARKER_STRIP_RE = re.compile(r"\s*\[\^\d+\]")
_DEFINITION_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
_TERM_NOTE_RE = re.compile(r"^\*\*(.+?)\*\*\s*\u2014\s*(.*)$")


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


def notes_path(project_dir: Path, file: str) -> Path:
    """Sidecar path for a chapter's notes: notes/<stem>.json."""
    return project.paths(project_dir)["notes"] / (Path(file).stem + ".json")


def load_notes(project_dir: Path, file: str) -> list[dict]:
    """Read the notes sidecar for a chapter; [] when missing or malformed
    (load_history's leniency: a broken sidecar means "no notes", never a
    crashed epub build)."""
    path = notes_path(project_dir, file)
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    if isinstance(notes, list) and all(isinstance(note, dict) for note in notes):
        return notes
    return []


def save_notes(project_dir: Path, file: str, lines: list[str], notes: list) -> list[dict]:
    """Validate notes against the chapter body and write the sidecar.

    lines are the exact body lines the indexes refer to (the list assemble
    joins; read_chapter's normalization means there is no phantom trailing
    "" element). Each kept note is normalized to {line, term, note, anchor},
    where anchor snapshots the line's first 80 characters so the epub
    builder can re-resolve the line after hand-edits shift them. Invalid
    entries are dropped defensively with a warning. An empty kept list
    DELETES the sidecar (absent = no notes). Returns the kept notes, i.e.
    the sidecar content.
    """
    kept: list[dict] = []
    for entry in notes:
        if not isinstance(entry, dict):
            print("[warn] dropped invalid note entry: not an object")
            continue
        line = entry.get("line")
        term = entry.get("term")
        note = entry.get("note")
        if isinstance(line, bool) or not isinstance(line, int) or not 0 <= line < len(lines):
            print(f"[warn] dropped invalid note entry: 'line' must be an integer in [0, {len(lines)})")
            continue
        if (not isinstance(term, str) or not isinstance(note, str)
                or not term.strip() or not note.strip()):
            print("[warn] dropped invalid note entry: 'term' and 'note' must be non-empty strings")
            continue
        kept.append({
            "line": line,
            "term": term,
            "note": note,
            "anchor": lines[line].strip()[:80],
        })

    path = notes_path(project_dir, file)
    if not kept:
        path.unlink(missing_ok=True)
        return kept
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "chapter": Path(file).name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": kept,
    }
    project.atomic_write_text(
        path, json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )
    return kept


def strip_marked_notes(body: str) -> tuple[str, list[dict]]:
    """Split legacy baked-in notes out of a chapter body.

    Pure text function (no project IO) for migrating chapters assembled by
    the old pipeline: a trailing "## Translator's Notes" section (FIRST body
    line whose stripped text equals the heading; everything from it onward)
    is split off, all "[^N]" footnote markers are removed from the remaining
    lines (stacked "[^1][^2]" included), and trailing blank lines are
    stripped. Definition lines ("[^N]: **term** — note") parse into
    [{"term", "note"}] in definition order; unparseable lines are skipped
    silently. A body with no section and no markers is returned unchanged.
    """
    lines = body.split("\n")

    tn_start = None
    for i, line in enumerate(lines):
        if line.strip() == _TN_HEADING:
            tn_start = i
            break
    body_lines = lines if tn_start is None else lines[:tn_start]

    if tn_start is None and not any(_MARKER_RE.search(line) for line in lines):
        return body, []

    existing: list[dict] = []
    if tn_start is not None:
        for line in lines[tn_start + 1:]:
            match = _DEFINITION_RE.match(line.strip())
            if not match:
                continue
            term_note = _TERM_NOTE_RE.match(match.group(2).strip())
            if not term_note:
                continue
            existing.append({
                "term": term_note.group(1).strip(),
                "note": term_note.group(2).strip(),
            })

    cleaned = [_MARKER_STRIP_RE.sub("", line) for line in body_lines]
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned), existing


def process(
    notes: list[dict],
    line_count: int,
    chapter_order: int,
    history: dict,
    gap: int,
    keep_low: bool = False,
) -> tuple[list[dict], dict, list[str]]:
    """Filter generated notes for one chapter.

    notes: [{"line": int (0-based), "term": str, "note": str,
    "threshold": str (optional)}] from the model.

    - Comprehension gate (first, before any other validation or dedup): drop
      every note whose "threshold" is "low" (case-insensitive), silently --
      unless keep_low is true, in which case the gate is skipped entirely.
      This is the model's self-assessed comprehension-threshold gate. Notes
      missing the "threshold" key or carrying any other value are kept.
    - Drop invalid entries (line outside [0, line_count), empty term/note,
      wrong types) with a warning string.
    - Key = term.strip(). Within-chapter duplicates by key: keep the first
      (warning for the rest).
    - Gap rule: if key in history, was annotated in a DIFFERENT chapter, and
      0 <= chapter_order - last_order <= gap (an earlier chapter, at most
      `gap` chapters before this one) -> drop silently (expected behavior)
      and leave history unchanged. A previous annotation in this same chapter
      (retranslation/retry) or in a LATER chapter (retranslating an earlier
      chapter after a later one already annotated the term -- negative
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
    # discarded silently, by design -- unless the project opts to keep them
    # (tn_keep_low_confidence; some models self-assess too harshly). Notes
    # missing "threshold" or with any other value are kept.
    if not keep_low:
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
            # means this very chapter is being retranslated -- its own note
            # must be restored, not suppressed; last_order > chapter_order
            # (negative distance) means an earlier chapter is being
            # retranslated after a later one already annotated the term --
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
