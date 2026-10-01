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


# Fallback for projects without a generated style profile.
DEFAULT_STYLE_SUMMARY = "a faithful literary translation that preserves the original's tone and register"


def resolve_style(project_dir: Path, novel_info: dict) -> tuple[str, str]:
    """The style summary the pipeline injects as {{style}}, plus its tier:
    "style_md" | "profile" | "default".

    The project's style.md (copied from a preset at init, hand-editable)
    wins -- but only when its body is non-empty; the legacy style_profile
    from --style auto follows; the generic default last. Tier and summary
    can disagree in one deliberate edge: a whitespace-only profile
    style_summary keeps the profile branch of the expression (yielding "")
    rather than falling back to the default, while the tier reports
    "default" -- exactly what the pipeline injected and what cmd_status
    displayed before the resolution moved here."""
    style_profile = novel_info.get("style_profile")
    style_profile = style_profile if isinstance(style_profile, dict) else {}
    style_summary = ""
    style_path = Path(project_dir) / "style.md"
    if style_path.is_file():
        try:
            _, style_summary = parse_style_file(
                style_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
            style_summary = ""
    style_summary = style_summary.strip()
    if style_summary:
        return style_summary, "style_md"
    style_summary = str(
        style_profile.get("style_summary") or DEFAULT_STYLE_SUMMARY
    ).strip()
    tier = (
        "profile"
        if str(style_profile.get("style_summary") or "").strip()
        else "default"
    )
    return style_summary, tier


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
                # utf-8-sig: project styles/ are user-editable overrides, so
                # tolerate a BOM; it would hide line 1's 'description:'
                # header from parse_style_file.
                text = path.read_text(encoding="utf-8-sig")
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
            # utf-8-sig: project styles/ are user-editable overrides, so
            # tolerate a BOM; it would leak the 'description:' header into
            # the returned body.
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise StyleError(f"cannot read style file {path}: {exc}") from exc
        _description, body = parse_style_file(text)
        if not body:
            raise StyleError(f"style file {path} has an empty body")
        return body
    names = ", ".join(n for n, _ in list_styles(project_dir)) or "(none)"
    raise StyleError(f"unknown style '{name}' - available: {names}")
