"""Translator's-note deduplication, cross-chapter history, and sidecar IO.

The pipeline generates candidate notes per chapter; this module drops the
model's self-assessed low-comprehension notes (threshold "low") unless
cfg tn_keep_low_confidence is set, then drops invalid entries, collapses
within-chapter duplicates, and suppresses notes for terms that were
already explained recently (the gap rule), maintaining a persistent
tn_history.json at the project root. Every drop the reader might want to
second-guess (low threshold, invalid entry, cap overflow) is recorded in
the per-chapter notes/<stem>.dropped.json review artifact (save_dropped);
within-chapter duplicates and gap-rule suppressions are not (the term is
still noted elsewhere / deliberately suppressed).

Notes are no longer baked into chapter markdown: they live in a
per-chapter sidecar notes/<stem>.json (load_notes/save_notes), whose line
indexes refer to the translated body lines and whose anchors let the epub
builder re-resolve lines after hand-edits. Kept notes are held in READING
order -- ascending by that line index -- because that list is what the epub
numbers footnotes from and what `review notes` reads as reading order;
process sorts its survivors after the severity-ordered cap, and load_notes
re-sorts what it reads, so a sidecar written before this ordering still
renders in reading order. strip_marked_notes migrates
legacy chapters whose notes were baked in as "[^N]" markers plus a
"## Translator's Notes" section.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from lib import project

# Legacy baked-in format (epub.py's chapter parser uses the same rules):
# markers "[^N]" appended to body lines, definitions "[^N]: **term** — note"
# under a "## Translator's Notes" heading.
_TN_HEADING = "## Translator's Notes"
_MARKER_RE = re.compile(r"\[\^\d+\]")
_MARKER_STRIP_RE = re.compile(r"\s*\[\^\d+\]")
_DEFINITION_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
_TERM_NOTE_RE = re.compile(r"^\*\*(.+?)\*\*\s*\u2014\s*(.*)$")

# The annotator tags each note with one of these categories (the
# tn_generate.md return schema); anything else -- missing, unknown, wrong
# type -- silently defaults to "other" (tn._category).
NOTE_CATEGORIES = ("cultural", "idiom", "wordplay", "honorific", "unit", "other")

# Drop reasons recorded in notes/<stem>.dropped.json, in the fixed report
# order (low_threshold first, then overflow, then invalid).
DROP_REASONS = ("low_threshold", "overflow", "invalid")


def _category(value: object) -> str:
    """Normalize a note category: a string in NOTE_CATEGORIES passes
    through; anything else (missing, unknown, wrong type) becomes
    "other", silently."""
    return value if isinstance(value, str) and value in NOTE_CATEGORIES else "other"


def _dropped_entry(entry: object, reason: str) -> dict:
    """Best-effort snapshot of a dropped candidate note: the reason is the
    only guaranteed field; the rest pass through as-is when the entry is a
    dict (term/note may be missing or non-string -- invalid entries are
    recorded for review, not re-validated)."""
    if not isinstance(entry, dict):
        return {"line": None, "term": None, "note": None,
                "category": None, "threshold": None, "reason": reason}
    return {
        "line": entry.get("line"),
        "term": entry.get("term"),
        "note": entry.get("note"),
        "category": _category(entry.get("category")),
        "threshold": entry.get("threshold"),
        "reason": reason,
    }


def _line_sort_key(entry: object) -> tuple[int, int]:
    """Reading-order sort key for one note: (0, line) for a usable integer
    line index, (1, 0) for anything else (not an object, `line` missing,
    `line` a bool or a non-int).

    The sort must stay TOTAL on hand-edited sidecars -- load_notes accepts
    any dict, and the pipeline's own output always carries a validated int
    -- so unusable entries park at the end of the order instead of raising
    a TypeError mid-build. They are not silently lost: every consumer
    (epub.chapter_md_to_xhtml, review_notes._resolve_line) already drops an
    unresolvable note with a [warn] of its own. Python's sort is stable, so
    several notes sharing one line keep the annotator's relative order.
    """
    line = entry.get("line") if isinstance(entry, dict) else None
    if isinstance(line, bool) or not isinstance(line, int):
        return (1, 0)
    return (0, line)


def _history_path(project_dir: Path) -> Path:
    return Path(project.paths(project_dir)["tn_history"])


def load_history(project_dir: Path) -> dict:
    """Load tn_history.json ({} when missing or malformed; discarding a
    malformed file prints a [warn] so the reset is never silent)."""
    path = _history_path(project_dir)
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        reason = type(exc).__name__
    else:
        reason = None if isinstance(data, dict) else type(data).__name__
    if reason is not None:
        print(f"[warn] tn_history.json unreadable ({reason}) - resetting note-gap tracking")
        return {}
    return data


def save_history(project_dir: Path, h: dict) -> None:
    project.atomic_write_text(
        _history_path(project_dir),
        json.dumps(h, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )


def notes_path(project_dir: Path, file: str) -> Path:
    """Sidecar path for a chapter's notes: notes/<stem>.json."""
    return project.paths(project_dir)["notes"] / (Path(file).stem + ".json")


def dropped_path(project_dir: Path, file: str) -> Path:
    """Path of the dropped-candidates review artifact: notes/<stem>.dropped.json."""
    return project.paths(project_dir)["notes"] / (Path(file).stem + ".dropped.json")


def load_notes(project_dir: Path, file: str) -> list[dict]:
    """Read the notes sidecar for a chapter in READING order (ascending
    `line`); [] when missing or malformed
    (load_history's leniency: a broken sidecar means "no notes", never a
    crashed epub build -- discarding a malformed sidecar prints a [warn]).

    The sort happens here, not only on write, so a sidecar stored in the
    old severity order still yields line-ordered footnotes in an epub built
    today. It reorders nothing on disk: the file itself is rewritten in
    reading order the next time the chapter is translated or `tn`-re-checked.
    """
    path = notes_path(project_dir, file)
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        reason = type(exc).__name__
    else:
        notes = data.get("notes") if isinstance(data, dict) else None
        reason = (
            None
            if isinstance(notes, list) and all(isinstance(note, dict) for note in notes)
            else "invalid notes"
        )
    if reason is not None:
        print(f"[warn] {path.name} unreadable ({reason}) - treating as no notes")
        return []
    return sorted(notes, key=_line_sort_key)


def save_notes(project_dir: Path, file: str, lines: list[str], notes: list) -> list[dict]:
    """Validate notes against the chapter body and write the sidecar.

    lines are the exact body lines the indexes refer to (the list assemble
    joins; read_chapter's normalization means there is no phantom trailing
    "" element). Each kept note is normalized to {line, term, note,
    category, anchor}, where anchor snapshots the line's first 80
    characters so the epub builder can re-resolve the line after
    hand-edits shift them, and category is normalized via tn._category
    (hand-edited or legacy entries may lack it; anything unrecognized
    becomes "other"). Invalid entries are dropped defensively with a
    warning. An empty kept list DELETES the sidecar (absent = no notes).
    Returns the kept notes, i.e. the sidecar content.
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
            "category": _category(entry.get("category")),
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
        path, json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )
    return kept


def save_dropped(project_dir: Path, file: str, dropped: list[dict]) -> None:
    """Write the dropped-candidates review artifact notes/<stem>.dropped.json.

    dropped is tn.process's fourth return value: [{"line", "term", "note",
    "category", "threshold", "reason"}] snapshots of candidates that were
    discarded (low_threshold, overflow, or invalid) -- the record lets a
    human second-guess the gates without re-running the annotator. Review
    artifact only: the epub builder does not read it. Same lifecycle as
    save_notes: an empty list DELETES the file (absent = nothing dropped).
    """
    path = dropped_path(project_dir, file)
    if not dropped:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "chapter": Path(file).name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dropped": dropped,
    }
    project.atomic_write_text(
        path, json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )


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
    max_notes: int | None = None,
) -> tuple[list[dict], dict, list[str], list[dict]]:
    """Filter generated notes for one chapter.

    notes: [{"line": int (0-based), "term": str, "note": str,
    "category": str (optional), "threshold": str (optional)}] from the model.

    - Comprehension gate (first, before any other validation or dedup): drop
      every note whose "threshold" is "low" (case-insensitive), silently --
      unless keep_low is true, in which case the gate is skipped entirely.
      This is the model's self-assessed comprehension-threshold gate. Notes
      missing the "threshold" key or carrying any other value are kept.
      Dropped low-threshold notes are recorded in `dropped` (reason
      "low_threshold"), still without a warning.
    - Drop invalid entries (line outside [0, line_count), empty term/note,
      wrong types) with a warning string; recorded in `dropped` with reason
      "invalid" (best-effort fields).
    - Key = term.strip(). Within-chapter duplicates by key: keep the first
      (warning for the rest). NOT recorded in `dropped` (the term is still
      noted elsewhere).
    - Gap rule: if key in history, was annotated in a DIFFERENT chapter, and
      0 <= chapter_order - last_order <= gap (an earlier chapter, at most
      `gap` chapters before this one) -> drop silently (expected behavior,
      NOT recorded in `dropped`) and leave history unchanged. A previous
      annotation in this same chapter (retranslation/retry) or in a LATER
      chapter (retranslating an earlier chapter after a later one already
      annotated the term -- negative distance; the reader hits the earlier
      chapter first) never suppresses the note. Otherwise keep the note and
      set history[key] = {"note", "last_order", "times": previous times + 1
      or 1}.
    - Reading order: the survivors are sorted by `line` ascending (ties keep
      the annotator's relative order), AFTER the cap, so the sidecar, the epub
      footnote numbers and `review notes`' reading order all follow the text.
      The severity order the annotator returned is a selection input only --
      it decides WHICH notes survive the cap, never how they are ordered.
    - Category: each kept entry's "category" is normalized to the entry's
      value when it is a string in NOTE_CATEGORIES, else "other" (silent
      default).
    - Cap (max_notes, None = no cap): AFTER the gap rule has produced the
      final kept list -- a gap-suppressed note must not waste a cap slot --
      kept is truncated to max_notes entries; the cut tail goes to
      `dropped` with reason "overflow". The annotator prompt asks for
      severity-ordered entries, so the truncation keeps the most severe
      context loss. An overflow-dropped note was never shown to the
      reader, so it must not suppress the term in later chapters inside
      the gap window: each dropped key's history entry is rolled back --
      restored to its input-history entry when one existed (including a
      same-chapter retranslation entry whose last_order equals this
      chapter's), removed when this call introduced the key.

    Returns (kept_notes, updated_history, warnings, dropped). The input
    history dict is not mutated; a shallow copy is returned.
    """
    warnings: list[str] = []
    kept: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    updated = dict(history)

    # Comprehension gate (Hy-MT2 convention), before any other validation or
    # dedup: notes the model marked threshold="low" (case-insensitive) are
    # discarded, by design -- unless the project opts to keep them
    # (tn_keep_low_confidence; some models self-assess too harshly). Notes
    # missing "threshold" or with any other value are kept. The discards are
    # recorded for review (reason "low_threshold") but stay warning-free.
    if not keep_low:
        survivors: list[object] = []
        for entry in notes:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("threshold"), str)
                and entry["threshold"].lower() == "low"
            ):
                dropped.append(_dropped_entry(entry, "low_threshold"))
            else:
                survivors.append(entry)
        notes = survivors

    for idx, entry in enumerate(notes):
        position = f"note #{idx + 1}"
        if not isinstance(entry, dict):
            warnings.append(f"dropped {position}: not an object")
            dropped.append(_dropped_entry(entry, "invalid"))
            continue
        line = entry.get("line")
        term = entry.get("term")
        note = entry.get("note")
        if isinstance(line, bool) or not isinstance(line, int):
            warnings.append(f"dropped {position}: 'line' must be an integer")
            dropped.append(_dropped_entry(entry, "invalid"))
            continue
        if not isinstance(term, str) or not isinstance(note, str):
            warnings.append(f"dropped {position}: 'term' and 'note' must be strings")
            dropped.append(_dropped_entry(entry, "invalid"))
            continue
        if line < 0 or line >= line_count:
            warnings.append(
                f"dropped {position} ('{term.strip() or '?'}'): "
                f"line {line} out of range [0, {line_count})"
            )
            dropped.append(_dropped_entry(entry, "invalid"))
            continue
        if not term.strip() or not note.strip():
            warnings.append(f"dropped {position}: empty term or note")
            dropped.append(_dropped_entry(entry, "invalid"))
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
        entry["category"] = _category(entry.get("category"))
        kept.append(entry)

    # Cap enforcement, AFTER the gap rule: notes the gap rule suppressed
    # never consumed a slot, so the kept list truncated here is the most
    # severe context loss the annotator offered (severity-ordered output).
    if max_notes is not None and len(kept) > max_notes:
        overflowed = kept[max_notes:]
        dropped.extend(_dropped_entry(entry, "overflow") for entry in overflowed)
        # The reader never saw an overflow-dropped note, so it must not
        # suppress the term inside the gap window of later chapters: roll
        # each dropped key back to its input-history entry when one existed
        # (restoring, not deleting, even when that entry's last_order equals
        # this chapter's order -- the same-chapter retranslation case), or
        # remove the key when this call introduced it. Restoring re-points
        # at the input entry, never mutating it: the input history stays a
        # pristine lookup table.
        for entry in overflowed:
            key = entry["term"].strip()
            if key in history:
                updated[key] = history[key]
            else:
                del updated[key]
        kept = kept[:max_notes]

    # Reading order LAST, after the cap above: the annotator returns
    # severity-ordered entries so the truncation keeps the most severe
    # context loss, but the survivors are then re-sorted by the line each
    # note sits on -- that is the order the reader meets, because the epub
    # numbers its footnotes straight off this list (a note on line 4 that
    # ranked first would otherwise print as [1] before [2] on line 1).
    kept.sort(key=_line_sort_key)

    return kept, updated, warnings, dropped
