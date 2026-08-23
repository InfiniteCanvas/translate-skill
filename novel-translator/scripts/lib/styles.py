"""Preset translation style guides: skill assets merged with project overrides."""

from __future__ import annotations

from pathlib import Path

STYLES_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "styles"


class StyleError(Exception):
    """Unknown style name or unreadable/empty style file."""


def parse_style_file(text: str) -> tuple[str, str]:
    """Split a style file into (description, body).

    A file may open with a one-line 'description: ...' header followed by a
    '---' separator line; files without that exact two-line prefix are all
    body.
    """
    lines = text.split("\n")
    if (
        len(lines) > 2
        and lines[0].startswith("description:")
        and lines[1].strip() == "---"
    ):
        return lines[0][len("description:"):].strip(), "\n".join(lines[2:]).strip()
    return "", text.strip()


def list_styles(project_dir: Path) -> list[tuple[str, str]]:
    """All styles as (name, description), sorted by name.

    Merges the skill's assets/styles/ presets with the project's styles/
    overrides (project wins on name collision). Unreadable files are skipped.
    """
    merged: dict[str, str] = {}
    for directory in (STYLES_DIR, Path(project_dir) / "styles"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            description, _body = parse_style_file(text)
            merged[path.stem] = description
    return sorted(merged.items())


def load_style(project_dir: Path, name: str) -> str:
    """The body text of the named style.

    Resolution: project styles/<name>.md first, then the skill's
    assets/styles/<name>.md. Raises StyleError (with the available names
    listed in the message) when not found, unreadable, or the body is empty.
    """
    for path in (Path(project_dir) / "styles" / f"{name}.md", STYLES_DIR / f"{name}.md"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StyleError(f"cannot read style file {path}: {exc}") from exc
        _description, body = parse_style_file(text)
        if not body:
            raise StyleError(f"style file {path} has an empty body")
        return body
    names = ", ".join(n for n, _ in list_styles(project_dir)) or "(none)"
    raise StyleError(f"unknown style '{name}' - available: {names}")
