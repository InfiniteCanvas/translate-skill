"""Chapter discovery, markdown frontmatter IO, and the chapter manifest."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# 1-4 digit chapter numbers (real projects use both 3-digit "Chapter_001.md"
# and 4-digit "Chapter_0001.md" conventions); sorting is by the parsed number,
# so projects stay correctly ordered either way.
CHAPTER_RE = re.compile(r"^Chapter_(\d{1,4})([a-z]?)\.md$", re.IGNORECASE)
STATUSES = ("pending", "in-progress", "needs-review", "translated")


@dataclass
class Chapter:
    path: Path
    file: str      # file name only, e.g. "Chapter_0042a.md"
    number: int    # 42
    suffix: str    # "a" or ""


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
    }


def discover(project_dir: Path) -> list[Chapter]:
    """All Chapter_NNNN[x].md files in source/, sorted by (number, suffix.lower())."""
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
                                    number=int(match.group(1)), suffix=match.group(2)))
    chapters.sort(key=lambda c: (c.number, c.suffix.lower()))
    return chapters


def atomic_write_text(path: Path, text: str, newline: str | None = None) -> None:
    """Atomically replace path's contents with text.

    Writes to a temporary file in the same directory and os.replace()s it into
    place, so an interrupt or crash mid-write can never leave a truncated or
    half-written destination file. newline semantics match Path.write_text
    (None = universal-newline translation).
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        os.replace(tmp, path)
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
    text = path.read_text(encoding="utf-8")
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
    """Write frontmatter + body; the body ends with exactly one newline."""
    body = body.rstrip("\n") + "\n"
    content = (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )
    Path(path).write_text(content, encoding="utf-8", newline="\n")


def load_manifest(project_dir: Path) -> list[dict]:
    """Read chapters.json; [] when missing."""
    manifest_path = paths(project_dir)["manifest"]
    if not manifest_path.is_file():
        return []
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def save_manifest(project_dir: Path, manifest: list[dict]) -> None:
    """Write chapters.json as UTF-8 JSON."""
    manifest_path = paths(project_dir)["manifest"]
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )


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
            "suffix": chapter.suffix,
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
