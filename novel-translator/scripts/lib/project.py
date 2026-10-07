"""Chapter discovery, markdown frontmatter IO, and the chapter manifest."""

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

# EXACTLY 4 digit chapter numbers, ALL CAPS, no suffix: "CHAPTER_0001.md" is
# the ONLY accepted spelling. 9999 chapters is far beyond any novel, and a
# fixed width removes the padding ambiguity that used to let
# "Chapter_001.md" and "Chapter_0001.md" both be admitted and both claim
# chapter 1. The optional letter suffix (Chapter_0042a.md, "extras/bonus
# chapters") was removed after it turned out nobody used it: it cost a field
# on the Chapter dataclass, a field in chapters.json, a branch in
# pipeline._entry_keys, a sort key, and four doc mirrors -- and it carried the
# one case a migration cannot resolve (what number does "0042a" want?).
#
# The pattern is deliberately NOT re.IGNORECASE. Case-insensitivity would let
# both "Chapter_0042.md" and "CHAPTER_0042.md" match and parse to number 42 --
# impossible on a case-insensitive filesystem, but two entries with the same
# number in chapters.json on a case-sensitive one, making "translate 42"
# ambiguous. Requiring one exact spelling closes that axis for good; a
# differently-cased name is a near-miss and gets reported instead. Digits are
# spelled [0-9], never \d: \d also matches full-width/Arabic-Indic decimal
# digits, so "CHAPTER_０００７.md" would be discovered and int()-collapse onto
# the real CHAPTER_0007. near_miss_reason() deliberately uses \d so those names
# are REPORTED instead of vanishing.
CHAPTER_RE = re.compile(r"^CHAPTER_([0-9]{4})\.md$")

# Looser patterns, used only to decide whether an ignored source/ file is worth
# warning about. They are never used to discover a chapter, and they stay
# case-insensitive precisely so the differently-cased near-misses above are
# recognized and reported. \d here is the point: it catches the
# full-width/Arabic-Indic digit names CHAPTER_RE rejects.
_CHAPTERISH_RE = re.compile(r"^chapter[\s_.-]*\d", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"^\d+$")
# The tail of a chapter-shaped stem, after "chapter_": digits and NOTHING else.
# Deliberately has no optional trailing letter, so a retired extra-chapter name
# ("Chapter_0042a.md") fails the match and gets reported instead of passing as
# a valid chapter. \d so full-width/Arabic-Indic digits are matched and can be
# reported rather than silently rejected.
_CHAPTER_TAIL_RE = re.compile(r"chapter_(\d+)", re.IGNORECASE)
STATUSES = ("pending", "in-progress", "needs-review", "translated")


@dataclass
class Chapter:
    path: Path
    file: str      # file name only, e.g. "CHAPTER_0042.md"
    number: int    # 42


def paths(project_dir: Path) -> dict:
    """Well-known paths within a project directory (all Path objects)."""
    root = Path(project_dir)
    return {
        "root": root,
        "source": root / "source",
        "draft": root / "draft",
        "translated": root / "translated",
        "export": root / "export",
        "covers": root / "covers",
        "templates": root / "templates",
        "config": root / "config.json",
        "novel_info": root / "novel_info.json",
        "manifest": root / "chapters.json",
        "glossary": root / "glossary.json",
        "tn_history": root / "tn_history.json",
        "story_state": root / "story_state.json",
        "notes": root / "notes",
    }


def discover(project_dir: Path) -> list[Chapter]:
    """All CHAPTER_NNNN.md files in source/, sorted by number (which with fixed
    4-digit padding is also plain name order)."""
    source = paths(project_dir)["source"]
    chapters: list[Chapter] = []
    if not source.is_dir():
        return chapters
    for entry in source.iterdir():
        if not entry.is_file():
            continue
        match = CHAPTER_RE.match(entry.name)
        if match:
            chapters.append(Chapter(path=entry, file=entry.name,
                                    number=int(match.group(1))))
    chapters.sort(key=lambda c: c.number)
    return chapters


def near_miss_reason(name: str) -> str | None:
    """Why a source/ file that does NOT match CHAPTER_RE looks like a dropped
    chapter, or None when it is plainly not one (README.md, .gitkeep, ...).

    Discovery stays silent about non-matching files -- it must, since source/
    legitimately holds more than chapters -- but silently ignoring something a
    user meant to be a chapter is data loss with no signal. init and sync print
    a [warn] for every name this function flags, so the near-miss classes named
    in references/ingestion.md ("Chapter_0007.zh.md", "chapter 7.md", "0007.md",
    a retired extra-chapter "Chapter_0042a.md", non-ASCII digits) are surfaced
    instead of requiring a manual count of the manifest against the TOC.

    \\d is intentional here and only here: it matches full-width and
    Arabic-Indic digits, which CHAPTER_RE's [0-9] deliberately does not. That
    is why the digit string is re-checked with str.isascii() below -- a regex
    match alone cannot tell ASCII from non-ASCII."""
    if CHAPTER_RE.match(name):
        return None
    stem = Path(name).stem
    if _BARE_NUMBER_RE.match(stem):
        return "no CHAPTER_ prefix"
    if not _CHAPTERISH_RE.match(stem):
        return None
    if Path(name).suffix.lower() != ".md":
        return f"extension {Path(name).suffix!r} is not .md"
    tail = _CHAPTER_TAIL_RE.fullmatch(stem)
    if re.fullmatch(r"chapter_\d+[a-z]", stem, re.IGNORECASE):
        return ("extras/bonus chapters (letter suffix) are no longer accepted - "
                "give this chapter its own number")
    if not tail:
        return "not CHAPTER_NNNN.md (exactly 4 digits)"
    digits = tail.group(1)
    if not digits.isascii():
        return "non-ASCII digits in the number"
    if len(digits) != 4:
        return f"{len(digits)} digits, not 4"
    return None


def ignored_chapters(project_dir: Path) -> list[tuple[str, str]]:
    """(filename, reason) for every source/ file discover() dropped that still
    looks like an intended chapter, sorted by NAME. Empty when source/ does
    not exist.

    Sorted by entry.name rather than by Path: PurePath ordering is
    case-insensitive on Windows, so sorting Paths would make the report order
    platform-dependent."""
    source = paths(project_dir)["source"]
    if not source.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for entry in sorted(source.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        reason = near_miss_reason(entry.name)
        if reason:
            out.append((entry.name, reason))
    return out


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace() with a brief exponential-backoff retry, silently
    succeeding or re-raising.

    On Windows the replace can raise PermissionError while another process
    holds the destination open (e.g. a parallel epub-build child reading
    chapters.json, or a reader holding the exported epub); seven attempts,
    backing off 0.1s..3.2s (~6.3s in total) between them, then the error
    surfaces. Shared by atomic_write_text, write_chapter, cover's JPEG
    writes, and the epub builder's binary tmp swap."""
    for attempt in range(7):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 6:
                raise
            time.sleep(0.1 * 2 ** attempt)


def atomic_write_text(path: Path, text: str, newline: str | None = None) -> None:
    """Atomically replace path's contents with text, creating the parent
    directory when it does not exist yet.

    Writes to a temporary file in the same directory and os.replace()s it into
    place, so an interrupt or crash mid-write can never leave a truncated or
    half-written destination file. newline semantics match Path.write_text
    (None = universal-newline translation). On Windows, os.replace can raise
    PermissionError while another process holds the destination open (e.g. a
    parallel epub-build child reading chapters.json); the replace is retried
    with backoff before the error surfaces.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        _replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_chapter(path: Path) -> tuple[dict, str]:
    """Parse a chapter into (frontmatter dict, body text).

    Frontmatter is optional YAML between an opening '---' line and a closing
    '---' line; blank lines between the closing '---' and the first body line
    are stripped, the body is otherwise preserved verbatim. No frontmatter ->
    ({}, entire file text). Raises ValueError (including the file name) if the
    YAML fails to parse or is not a mapping.
    """
    path = Path(path)
    # utf-8-sig: hand-edited chapters saved as UTF-8-with-BOM must not lose
    # their frontmatter to a leading "\ufeff" on the opening '---' line.
    text = path.read_text(encoding="utf-8-sig")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for close in range(1, len(lines)):
        if lines[close].strip() != "---":
            continue
        fm_text = "\n".join(lines[1:close])
        try:
            frontmatter = yaml.safe_load(fm_text)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML frontmatter in {path.name}: {exc}") from exc
        if frontmatter is None:
            frontmatter = {}
        if not isinstance(frontmatter, dict):
            raise ValueError(f"invalid YAML frontmatter in {path.name}: expected a mapping")
        body_lines = lines[close + 1:]
        first = 0
        while first < len(body_lines) and not body_lines[first].strip():
            first += 1
        last = len(body_lines)
        # Strip trailing blank lines: a file's final newline would otherwise
        # become a phantom empty line after body.split("\\n") in the pipeline
        # (models drop it and fail line-count validation). write_chapter
        # re-appends exactly one newline, so round-trips are stable.
        while last > first and not body_lines[last - 1].strip():
            last -= 1
        return frontmatter, "\n".join(body_lines[first:last])
    return {}, text.rstrip("\n")  # no closing '---': treat the whole file as body


def write_chapter(path: Path, frontmatter: dict, body: str) -> None:
    """Write frontmatter + body atomically; the body ends with exactly one
    newline."""
    body = body.rstrip("\n") + "\n"
    content = (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )
    # Same utf-8 / LF bytes as the previous direct write_text, but swapped in
    # atomically: a crash mid-write can no longer leave a truncated chapter
    # that read_chapter would silently parse as a frontmatter-less body.
    atomic_write_text(Path(path), content, newline="\n")


def load_manifest(project_dir: Path) -> list[dict]:
    """Read chapters.json; [] when missing."""
    manifest_path = paths(project_dir)["manifest"]
    if not manifest_path.is_file():
        return []
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def save_manifest(project_dir: Path, manifest: list[dict]) -> None:
    """Write chapters.json as UTF-8 JSON."""
    manifest_path = paths(project_dir)["manifest"]
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )


def load_novel_info(project_dir: Path) -> dict:
    """Read novel_info.json LENIENTLY: {} when the file is missing, corrupt,
    or not a JSON object -- the exact semantics every silent consumer needs
    (cmd_sync's backfill defaults, cmd_status's style tier, the pipeline's
    novel background/style resolution, tn_recheck's background frame).
    translate.py keeps its own STRICT _load_novel_info (raises CliError) for
    cmd_profile/cmd_build_epub, which rewrite or build from the file; do not
    route those through this lenient reader."""
    path = paths(project_dir)["novel_info"]
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):  # json.JSONDecodeError is a ValueError
        return {}
    return loaded if isinstance(loaded, dict) else {}


def drop_leading_chapter_title(lines: list[str], frontmatter: dict) -> tuple[list[str], bool]:
    """Drop lines[0] when it repeats the frontmatter chapter_title: models
    see the title twice (title instruction + body line 1) and emit an empty
    or dropped first line, and VALIDATE pairs source/translated bodies
    line-aligned. Returns (lines, dropped); silent by design -- the
    pipeline's [init] print stays at its call site."""
    first = lines[0].strip().strip("\u3000 ") if lines else ""
    if first and first == str(frontmatter.get("chapter_title", "")).strip().strip("\u3000 "):
        return lines[1:], True
    return lines, False


def backfill_frontmatter(chapters: list[Chapter], novel_title: str, author: str, source_url: str) -> int:
    """Fill missing novel-level frontmatter keys on source chapters and derive
    chapter_title from the first non-empty body line when absent. Empty
    novel-level values are skipped. Returns the number of chapters changed."""
    backfilled = 0
    for chapter in chapters:
        fm, body = read_chapter(chapter.path)
        changed = False
        for key, value in (
            ("novel_title", novel_title),
            ("author", author),
            ("source_url", source_url),
        ):
            if key not in fm and value:
                fm[key] = value
                changed = True
        if "chapter_title" not in fm:
            first_line = next(
                (ln.strip(" \u3000#") for ln in body.split("\n") if ln.strip()), ""
            )
            if first_line:
                fm["chapter_title"] = first_line
                changed = True
        if changed:
            write_chapter(chapter.path, fm, body)
            backfilled += 1
    return backfilled


def sync_manifest(project_dir: Path) -> list[dict]:
    """Rebuild the manifest from discover(), preserving status/title by file
    name, and write the recomputed 0-based order back into each source
    chapter's frontmatter (rewriting only when it changed)."""
    previous: dict[str, dict] = {}
    for entry in load_manifest(project_dir):
        if isinstance(entry, dict) and entry.get("file"):
            previous[entry["file"]] = entry
    manifest: list[dict] = []
    for order, chapter in enumerate(discover(project_dir)):
        frontmatter, body = read_chapter(chapter.path)
        prev = previous.get(chapter.file)
        status = prev.get("status", "pending") if prev else "pending"
        title = prev["title"] if prev and "title" in prev else frontmatter.get("chapter_title", "")
        manifest.append({
            "file": chapter.file,
            "number": chapter.number,
            "order": order,
            "status": status,
            "title": title,
        })
        if frontmatter.get("order") != order:
            frontmatter["order"] = order
            write_chapter(chapter.path, frontmatter, body)
    save_manifest(project_dir, manifest)
    return manifest


def find_entry(manifest: list[dict], file: str) -> dict | None:
    """Manifest entry with the given file name, or None."""
    for entry in manifest:
        if entry.get("file") == file:
            return entry
    return None


def set_status(manifest: list[dict], file: str, status: str) -> None:
    """Set a chapter's status; validates against STATUSES."""
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}; expected one of: {', '.join(STATUSES)}")
    entry = find_entry(manifest, file)
    if entry is None:
        raise KeyError(f"chapter {file!r} not found in manifest")
    entry["status"] = status
