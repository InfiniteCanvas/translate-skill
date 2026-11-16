"""Chapter discovery, markdown frontmatter IO, and the chapter manifest."""

import json
import os
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

CHAPTER_RE = re.compile(r"^CHAPTER_([0-9]{4})\.md$")

# The source's own table of contents, when the project carries one. The
# acquisition skill (zh-source-downloader) writes it next to the raw cache
# and leaves it in place; it is where the source states how many chapters
# the book has, which is the only number a completeness check can be
# measured against.
SOURCE_TOC_FILE = "zh-toc.json"

# A chapter heading, in the two scripts the mirrors serve: 第N章 … and
# Chapter N …
_HEADING_RE = re.compile(
    r"^\s*(?:第\s*[0-9０-９零一二三四五六七八九十百千两]+\s*[章節节回卷篇]"
    r"|chapter\s+[0-9ivxlcdmIVXLCDM]+\b)",
    re.IGNORECASE,
)

_CHAPTERISH_RE = re.compile(r"^chapter[\s_.-]*\d", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"^\d+$")
_CHAPTER_TAIL_RE = re.compile(r"chapter_(\d+)", re.IGNORECASE)
STATUSES = ("pending", "in-progress", "needs-review", "translated")


@dataclass
class Chapter:
    path: Path
    file: str
    number: int


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
        "logs": root / "logs",
        "source_toc": root / SOURCE_TOC_FILE,
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
        while last > first and not body_lines[last - 1].strip():
            last -= 1
        return frontmatter, "\n".join(body_lines[first:last])
    return {}, text.rstrip("\n")


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
    except (OSError, ValueError):
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


# ---------------------------------------------------------------------------
# Source hygiene: the repairs ingestion makes, and what it reports about them
# ---------------------------------------------------------------------------


def heading_shaped(title: str) -> bool:
    """Whether a recorded chapter title looks like a chapter heading.

    The test matters because the skill's own frontmatter backfill derives
    a missing chapter_title from the chapter's WHOLE first body line, so a
    line of prose can arrive wearing a title. A title-driven repair must
    not trust such a title.
    """
    return bool(_HEADING_RE.match(str(title or "").strip()))


def find_duplicated_opening(paragraphs: list[str]) -> tuple[int, int] | None:
    """The (start, length) of a repeated opening block, or None.

    A duplicated opening block is a run of three or more consecutive
    paragraphs that repeats an earlier run of the same paragraphs - the
    shape a mirror's preview produces when it serves its excerpt and then
    the whole chapter. The later occurrence is the one to go.
    """
    total = len(paragraphs)
    best: tuple[int, int] | None = None
    for start in range(total - 2):
        for earlier in range(start):
            length = 0
            while (start + length < total and earlier + length < start
                   and paragraphs[start + length] == paragraphs[earlier + length]):
                length += 1
            if length < 3:
                continue
            if best is None or start < best[0] or (start == best[0] and length > best[1]):
                best = (start, length)
    return best


def repair_source_body(frontmatter: dict, body: str) -> tuple[str, list[str]]:
    """Apply the source repairs to one chapter's body.

    Returns (body, notes) where notes name what changed, in the order it
    changed. Both repairs are idempotent: a second run over the repaired
    body finds nothing and returns the body unchanged.
    """
    notes: list[str] = []
    lines = body.split("\n")

    title = str(frontmatter.get("chapter_title") or "").strip()
    if heading_shaped(title):
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(title) and len(stripped) > len(title):
                remainder = stripped[len(title):].strip()
                if remainder:
                    lines[index] = remainder
                else:
                    del lines[index]
                notes.append(
                    f"stripped the chapter heading ({title}) from the first paragraph")
            break

    duplicated = find_duplicated_opening(lines)
    if duplicated is not None:
        start, length = duplicated
        del lines[start:start + length]
        notes.append(
            f"removed a duplicated opening block: {length} paragraph(s) "
            f"repeated from paragraph {start + 1 - length}")

    return "\n".join(lines), notes


def repair_source_chapters(project_dir: Path, chapters: list[Chapter]) -> list[dict]:
    """Repair the source chapters, and report the ones it did not touch.

    A chapter that already has a translation is never rewritten: its
    source and its translation are paired line for line, so removing a
    line from the source would leave the pair broken - and the pipeline
    compares a chapter against the source snapshot it took when the
    chapter started, so it cannot see the drift. Such a chapter is
    reported instead, because it needs re-translating and that is the
    operator's decision.

    Returns one record per chapter that needed attention:
      {"file", "repaired": bool, "translated": bool, "notes": [...]}
    """
    translated_dir = paths(project_dir)["translated"]
    records: list[dict] = []
    for chapter in chapters:
        frontmatter, body = read_chapter(chapter.path)
        repaired, notes = repair_source_body(frontmatter, body)
        if not notes:
            continue
        if (translated_dir / chapter.file).is_file():
            records.append({"file": chapter.file, "repaired": False,
                            "translated": True, "notes": notes})
            continue
        write_chapter(chapter.path, frontmatter, repaired)
        records.append({"file": chapter.file, "repaired": True,
                        "translated": False, "notes": notes})
    return records


# ---------------------------------------------------------------------------
# Completeness: what the book is missing, and what looks cut short
# ---------------------------------------------------------------------------


def load_source_toc(project_dir: Path) -> dict:
    """The source's own table of contents, when the project carries one.

    Lenient like every other silent reader: {} when the file is missing,
    unreadable or not a JSON object.
    """
    path = paths(project_dir)["source_toc"]
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _toc_positions(toc: dict) -> set[int]:
    positions: set[int] = set()
    for entry in toc.get("chapters") or []:
        if not isinstance(entry, dict):
            continue
        try:
            positions.add(int(entry.get("position")))
        except (TypeError, ValueError):
            continue
    return positions


def _expected_chapters(toc: dict, cfg: dict) -> int | None:
    """How many chapters the source says the book has.

    The source's own table of contents states it; an operator-entered
    value (the config key `expected_chapters`) stands in when the project
    carries no table of contents. None means nobody has said, and no
    shortfall can be measured.
    """
    raw = toc.get("chapter_count")
    if raw is None:
        raw = toc.get("official_chapter_count")
    try:
        if raw is not None:
            return int(raw)
    except (TypeError, ValueError):
        pass
    try:
        entered = cfg.get("expected_chapters")
    except AttributeError:
        entered = None
    if entered is None:
        return None
    try:
        return int(entered)
    except (TypeError, ValueError):
        return None


def completeness_report(project_dir: Path, cfg: dict | None = None) -> dict:
    """What the book is missing, and which chapters look cut short.

    Two independent signals, because they catch different failures:

    - the EXPECTED COUNT: how many chapters the source says the book has
      (its table of contents, or an operator-entered value), against the
      chapters the project holds. The positions missing from the source's
      table of contents are the ones the SOURCE has not published; the
      positions the table lists but the project lacks are the ones the
      ACQUISITION did not fetch. The two are different facts and the
      report keeps them apart.
    - the LENGTH OUTLIER: a chapter whose source text is far shorter than
      the rest of the book, compared against the book's own median. A
      truncated acquisition looks like this.

    The report never blocks anything: it is a fact about the book, and
    what to do about it is the operator's decision.
    """
    cfg = cfg or {}
    chapters = discover(project_dir)
    paragraph_counts: dict[str, int] = {}
    for chapter in chapters:
        _, body = read_chapter(chapter.path)
        paragraph_counts[chapter.file] = len(
            [line for line in body.split("\n") if line.strip()])

    median = int(statistics.median(paragraph_counts.values())) if paragraph_counts else 0
    try:
        ratio = float(cfg.get("completeness_outlier_ratio", 0.3))
    except (TypeError, ValueError):
        ratio = 0.3
    threshold = ratio * median
    outliers = [
        {"chapter": name, "paragraphs": count, "median": median}
        for name, count in sorted(paragraph_counts.items())
        if threshold > 0 and count < threshold
    ]

    toc = load_source_toc(project_dir)
    expected = _expected_chapters(toc, cfg)
    held = {chapter.number for chapter in chapters}
    listed = _toc_positions(toc)

    missing_from_toc: list[int] = []
    missing_from_project: list[int] = []
    if expected is not None:
        if listed:
            missing_from_toc = sorted(set(range(1, expected + 1)) - listed)
            missing_from_project = sorted(listed - held)
        else:
            missing_from_project = sorted(set(range(1, expected + 1)) - held)

    return {
        "expected": expected,
        "present": len(chapters),
        "missing_from_toc": missing_from_toc,
        "missing_from_project": missing_from_project,
        "median_paragraphs": median,
        "outlier_ratio": ratio,
        "outliers": outliers,
    }


def write_completeness_report(project_dir: Path, report: dict) -> Path:
    """Write the completeness findings where the operator's tools read them.

    logs/completeness.json, written atomically: a run killed mid-write
    cannot leave a torn file behind, and the reader treats an unreadable
    one as "no findings" rather than a broken page.
    """
    path = paths(project_dir)["logs"] / "completeness.json"
    atomic_write_text(
        path,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )
    return path
