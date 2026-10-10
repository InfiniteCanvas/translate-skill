"""v10: rename source chapters to the fixed 4-digit, ALL-CAPS canonical form.

project.CHAPTER_RE went from `^Chapter_([0-9]{1,4})([a-z]?)\.md$` (IGNORECASE)
to `^CHAPTER_([0-9]{4})\.md$` -- case-sensitive, no IGNORECASE flag at all. Every
old spelling stops being discovered: short padding (Chapter_001.md), the old
title-case prefix (chapter_0042.md, Chapter_0042.md), a wrong-case extension
(CHAPTER_0042.MD), and the letter suffix that used to mark extras/bonus
chapters (Chapter_0042a.md). Anything not renamed is silently dropped from the
manifest -- no entry, no status, no translation target -- so projects built
before v10 need their files moved, not just re-discovered.

Most renames here differ ONLY IN CASE, which is the awkward part. On a
case-insensitive filesystem (Windows, default macOS) the destination already
"exists" as the very file being renamed, so a direct Path.rename() raises
FileExistsError. _rename() therefore stages through an intermediate name when
only the case differs, and _taken() keeps that same fact from being mistaken
for a name clash -- otherwise every canonical rename would report a phantom
collision and skip itself.

A rename orphans everything keyed on the file name, which is why this step does
not stop at source/: the manifest's `file` field, story_state.json's recap
keys, and the draft/translated/notes artifacts all move with it. draft/ and
notes/ are globbed per stem rather than listed file-by-file, so an artifact
added by a later version is carried along instead of stranded.

Extra chapters are NOT renamed here, and that is a correctness requirement:
with the suffix gone, the canonical name for Chapter_0042a.md would be
CHAPTER_0042.md -- which a real CHAPTER_0042.md may already occupy. Renaming
would either clobber a chapter or collide in the manifest, and no mechanical
answer exists to what number an extra actually wants (is 0042a chapter 43? a
second part?). These get a [warn] naming each file and telling the operator to
hand the decision to their agent with the TOC.

Idempotent: a second run finds every name already canonical, defers the same
extra chapters, and reports nothing beyond the delegated template sync.

No config keys and no templates change, so this step does NOT delegate to
standard_step -- there is nothing to materialize, and running
materialize_config here would fold unrelated defaults into a rename's report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from . import common
from lib import project

VERSION = 10
DESCRIPTION = ("rename source chapters to the fixed 4-digit, all-caps canonical "
               "form (Chapter_001.md -> CHAPTER_0001.md), carrying "
               "chapters.json, story_state.json and the per-chapter artifacts")

LEGACY_RE = re.compile(r"^Chapter_([0-9]{1,4})([a-z]?)\.md$", re.IGNORECASE)


def migrate(project_dir: Path, templates_src: Path,
            dry_run: bool = False, force: bool = False,
            confirm: Callable[[str], bool] | None = None) -> list[str]:
    project_dir = Path(project_dir)
    paths = project.paths(project_dir)
    lines: list[str] = []

    renames, deferred = _plan(paths["source"])
    collisions = [(old, new) for old, new in renames.items()
                  if _taken(paths["source"] / old, paths["source"] / new)]
    for old, new in collisions:
        del renames[old]
        lines.append(f"[warn] chapters: {old} NOT renamed - {new} already "
                     "exists in source/. Resolve the name clash by hand (one "
                     "of the two files is the wrong chapter number) and "
                     "re-run migrate.")

    if renames:
        example = f"{sorted(renames)[0]} -> {renames[sorted(renames)[0]]}"
        verb = "would rename" if dry_run else "renamed"
        lines.append(f"[ok] chapters: {verb} {len(renames)} source file(s) to "
                     f"the 4-digit all-caps canonical form (e.g. {example})")
        if not dry_run:
            _apply(paths, renames)

    if deferred:
        listed = ", ".join(sorted(deferred))
        lines.append(
            f"[warn] chapters: {len(deferred)} extra chapter(s) with a letter "
            f"suffix need a DECISION and were LEFT UNCHANGED: {listed}"
        )
        lines.append(
            "[warn] chapters: hand these to your agent, do not bulk-rename "
            "them. The suffix is gone, so the mechanical rename would be "
            "Chapter_0042a.md -> CHAPTER_0042.md -- clobbering a real chapter. "
            "Give it this list plus the TOC and the rule: assign each its own "
            "CHAPTER_NNNN.md number, then update chapters.json, "
            "story_state.json, draft/, translated/ and notes/ to match."
        )

    if renames and not dry_run:
        lines.extend(_rewrite_manifest(paths, renames))
        lines.extend(_rewrite_story_state(paths, renames))
        moved = _count_artifacts(paths, renames)
        if moved:
            lines.append(f"[ok] chapters: carried {moved} draft/translated/"
                         "notes artifact(s) across the rename")

    return lines + common.sync_templates(
        project_dir, templates_src, dry_run, force, confirm)


def _canonical(number: str) -> str:
    """The one spelling v10 accepts for this chapter. Takes no suffix: a
    suffixed name never reaches this (it is deferred instead), and computing
    it would hand back a name a real chapter may already own."""
    return f"CHAPTER_{int(number):04d}.md"


def _taken(src: Path, dst: Path) -> bool:
    """True when dst is occupied by a DIFFERENT file, so moving src there
    would clobber it.

    A case-only difference is NOT a clash: after the rename to all-caps, most
    renames in this step differ only in case, and on a case-insensitive
    filesystem dst already exists precisely because it IS src. Treating that
    as a collision would skip every canonical rename."""
    if not dst.exists():
        return False
    return src.name.lower() != dst.name.lower()


def _rename(src: Path, dst: Path) -> None:
    """Move src to dst, staging through a temp name when only the case differs.

    On a case-insensitive filesystem the destination of a case-only rename
    resolves to src itself, so a direct rename() raises FileExistsError. Going
    via an intermediate whose name differs from BOTH endpoints frees the
    destination before the second step."""
    if src.name.lower() != dst.name.lower():
        src.rename(dst)
        return
    tmp = src.with_name(src.name + ".v010tmp")
    if tmp.exists():
        tmp.unlink()
    src.rename(tmp)
    tmp.rename(dst)


def _plan(source: Path) -> tuple[dict[str, str], list[str]]:
    """Split source/ into (old->new renames, extra chapters left to an agent).

    A name that already equals its canonical form is not a rename. A name
    carrying a letter suffix is never automated -- see the module docstring."""
    renames: dict[str, str] = {}
    deferred: list[str] = []
    if not source.is_dir():
        return renames, deferred
    for entry in sorted(source.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        match = LEGACY_RE.match(entry.name)
        if not match:
            continue
        number, suffix = match.group(1), match.group(2)
        if suffix:
            deferred.append(entry.name)
            continue
        canonical = _canonical(number)
        if entry.name != canonical:
            renames[entry.name] = canonical
    return renames, deferred


def _apply(paths: dict, renames: dict[str, str]) -> None:
    """Move the source file and every artifact keyed on its name.

    Artifacts are moved BEFORE the manifest is rewritten, so a failure partway
    leaves the manifest pointing at names that still exist -- recoverable -- and
    never the reverse."""
    for old, new in renames.items():
        old_stem, new_stem = Path(old).stem, Path(new).stem
        _rename(paths["source"] / old, paths["source"] / new)

        translated = paths["translated"] / old
        if translated.exists():
            _rename(translated, paths["translated"] / new)

        for folder in (paths["draft"], paths["notes"]):
            if not folder.is_dir():
                continue
            for artifact in sorted(folder.glob(f"{old_stem}.*")):
                tail = artifact.name[len(old_stem):]
                target = folder / f"{new_stem}{tail}"
                if _taken(artifact, target):
                    continue
                _rename(artifact, target)


def _count_artifacts(paths: dict, renames: dict[str, str]) -> int:
    """How many artifacts the rename had to carry (reported, not re-moved)."""
    count = 0
    for old, new in renames.items():
        new_stem = Path(new).stem
        if (paths["translated"] / new).exists():
            count += 1
        for folder in (paths["draft"], paths["notes"]):
            if folder.is_dir():
                count += len(list(folder.glob(f"{new_stem}.*")))
    return count


def _rewrite_manifest(paths: dict, renames: dict[str, str]) -> list[str]:
    """Point every manifest entry at its renamed file."""
    manifest = paths["manifest"]
    if not manifest.is_file():
        return ["[warn] chapters: chapters.json not found - status and titles "
                "were not carried across the rename"]
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return [f"[warn] chapters: chapters.json unreadable ({exc}) - status "
                "and titles were not carried across the rename"]
    if not isinstance(entries, list):
        return ["[warn] chapters: chapters.json is not a list - status and "
                "titles were not carried across the rename"]

    changed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        new = renames.get(entry.get("file", ""))
        if new is not None:
            entry["file"] = new
            changed += 1
    if changed:
        project.atomic_write_text(
            manifest,
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
            newline="\n",
        )
    return [f"[ok] chapters: updated chapters.json ({changed} entries re-pointed)"]


def _rewrite_story_state(paths: dict, renames: dict[str, str]) -> list[str]:
    """Re-key story_state.json's recap entries from the old stem to the new.

    Read and written directly rather than through lib/story, whose load_state
    prunes entries whose stem is absent from the manifest -- run after the
    manifest rewrite, every old-stem recap would be discarded instead of moved."""
    state = paths["story_state"]
    if not state.is_file():
        return []
    try:
        data = json.loads(state.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return [f"[warn] chapters: story_state.json unreadable ({exc}) - "
                "recaps were not carried across the rename"]
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(chapters, dict):
        return []

    stems = {Path(old).stem: Path(new).stem for old, new in renames.items()}
    moved = 0
    for old_stem, new_stem in stems.items():
        if old_stem in chapters and old_stem != new_stem:
            chapters[new_stem] = chapters.pop(old_stem)
            moved += 1
    if moved:
        project.atomic_write_text(
            state,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            newline="\n",
        )
    return [f"[ok] chapters: re-keyed story_state.json ({moved} recap(s))"]
