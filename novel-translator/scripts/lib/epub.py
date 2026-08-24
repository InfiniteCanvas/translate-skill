"""EPUB3 export for novel-translator projects.

Builds ``export/<slug>.epub`` from the translated chapters listed in the
project manifest, with real epub3 footnotes (``epub:type="noteref"`` anchors
pointing at ``epub:type="footnote"`` asides), a flat chapter TOC, shared CSS
and an optional cover.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from ebooklib import epub

try:  # package-style import when scripts/lib is imported as a package
    from . import project
except ImportError:  # flat import when scripts/lib is on sys.path
    import project


class EpubError(Exception):
    pass


_CSS = (
    "body{line-height:1.6;margin:0 5%}\n"
    "p{margin:0 0 .75em 0}\n"
    "h1{font-size:1.3em;margin:1.2em 0 .6em}\n"
    "h2{font-size:1.1em}\n"
    "aside{font-size:.85em;color:#444;margin:1em 0}\n"
    "sup a{text-decoration:none}\n"
)

_TN_HEADING = "## Translator's Notes"
_MARKER_RE = re.compile(r"\[\^(\d+)\]")
_DEFINITION_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_STRONG_RE = re.compile(r"\*\*(.+?)\*\*")
_EM_RE = re.compile(r"\*([^*]+?)\*")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    text = _STRONG_RE.sub(r"<strong>\1</strong>", text)
    text = _EM_RE.sub(r"<em>\1</em>", text)
    return text


def _convert(text: str) -> str:
    """XML-escape first, then inline markdown (**bold**, *italic*)."""
    return _inline(_esc(text))


def chapter_md_to_xhtml(md_path: Path) -> tuple[str, str, bool]:
    """Parse a translated chapter -> (title, xhtml <body> inner, has_notes)."""
    fm, body = project.read_chapter(Path(md_path))
    lines = body.split("\n")

    # Split off the trailing Translator's Notes section.
    tn_start = None
    for i, line in enumerate(lines):
        if line.strip() == _TN_HEADING:
            tn_start = i
            break
    body_lines = lines if tn_start is None else lines[:tn_start]
    note_lines = [] if tn_start is None else lines[tn_start + 1 :]

    # Definitions: [^3]: **term** — note text
    definitions: dict[int, str] = {}
    for line in note_lines:
        match = _DEFINITION_RE.match(line.strip())
        if match:
            definitions[int(match.group(1))] = match.group(2).strip()

    # Strip footnote markers from body lines, remembering which line owns them.
    markers: dict[int, list[int]] = {}
    cleaned: list[str] = []
    for i, line in enumerate(body_lines):
        ids = [int(n) for n in _MARKER_RE.findall(line)]
        if ids:
            markers[i] = ids
        cleaned.append(_MARKER_RE.sub("", line))

    # Render body elements.
    elements: list[dict] = []
    for i, line in enumerate(cleaned):
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            elements.append(
                {
                    "kind": "h",
                    "level": len(heading.group(1)),
                    "html": _esc(heading.group(2).strip()),
                    "line": i,
                }
            )
        else:
            elements.append({"kind": "p", "html": _convert(stripped), "line": i})

    # Attach noteref anchors to the end of the owning paragraph. Anchors are
    # only emitted for ids that have a definition, so hrefs never dangle.
    emitted: set[int] = set()

    def anchor(nid: int) -> str:
        attrs = f'epub:type="noteref" href="#tn-{nid}"'
        if nid not in emitted:
            attrs += f' id="ref-{nid}"'
            emitted.add(nid)
        return f'<a {attrs}><sup>[{nid}]</sup></a>'

    for i, ids in markers.items():
        target = None
        for element in elements:  # the paragraph on this very line
            if element["line"] == i and element["kind"] == "p":
                target = element
                break
        if target is None:  # next non-empty paragraph ...
            for element in elements:
                if element["kind"] == "p" and element["line"] > i:
                    target = element
                    break
        if target is None:  # ... falling back to the previous paragraph
            for element in reversed(elements):
                if element["kind"] == "p":
                    target = element
                    break
        if target is not None:
            for nid in ids:
                if nid in definitions:
                    target["html"] += anchor(nid)

    parts: list[str] = []
    title = str(fm.get("title") or fm.get("chapter_title") or Path(md_path).stem)

    # The translated format carries the chapter title in frontmatter only, so
    # give the chapter a visible heading when the body has none of its own.
    if not any(element["kind"] == "h" for element in elements):
        parts.append(f"<h1>{_esc(title)}</h1>")
    for element in elements:
        if element["kind"] == "h":
            parts.append(
                f'<h{element["level"]}>{element["html"]}</h{element["level"]}>'
            )
        else:
            parts.append(f'<p>{element["html"]}</p>')

    has_notes = bool(emitted)
    if has_notes:
        parts.append("<hr/>")
        for nid in sorted(emitted):
            parts.append(
                f'<aside epub:type="footnote" role="doc-footnote" id="tn-{nid}">'
                f"<p>[{nid}] {_convert(definitions[nid])}</p></aside>"
            )

    return title, "\n".join(parts), has_notes


_UNSAFE_FS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _slugify(title: str) -> str:
    """Filename slug for an epub: ASCII words joined by hyphens. When the
    title has no ASCII words (e.g. a pure CJK title), fall back to the title
    itself minus filesystem-unsafe characters — named after the title beats
    collapsing to 'novel'."""
    parts = re.findall(r"[A-Za-z0-9]+", title)
    if parts:
        return "-".join(parts).lower()
    fallback = _UNSAFE_FS_RE.sub("", title)
    fallback = re.sub(r"\s+", "-", fallback.strip())
    return fallback.strip(".-") or "novel"


def build(
    project_dir: Path, novel_info: dict, cfg: dict, skip_check: bool = False
) -> tuple[Path, bool | None, str]:
    """Build export/<slug>.epub from all manifest chapters with status
    'translated'. Returns (epub path, epubcheck ok | None, epubcheck output)."""
    paths = project.paths(project_dir)
    manifest = project.load_manifest(project_dir)

    chapter_paths: list[Path] = []
    for entry in manifest:
        if entry.get("status") != "translated":
            continue
        path = Path(paths["translated"]) / entry["file"]
        if path.exists():
            chapter_paths.append(path)
        else:
            print(f"[warn] missing translated chapter: {path}")
    if not chapter_paths:
        raise EpubError("no translated chapters")

    title = novel_info.get("title_translated") or novel_info.get("title") or "Untitled"
    if not novel_info.get("title_translated") and not re.search(r"[A-Za-z0-9]", title):
        print('[warn] epub titled from the original-language title; set "title_translated" '
              "in novel_info.json for an English filename")
    author = novel_info.get("author") or ""
    lang = cfg.get("target_lang", "en")

    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid5(uuid.NAMESPACE_URL, title + author)))
    book.set_title(title)
    book.set_language(lang)
    book.add_author(author)
    for tag in novel_info.get("tags") or []:
        book.add_metadata("DC", "subject", tag)

    css_item = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=_CSS.encode("utf-8"),
    )
    book.add_item(css_item)

    items = []
    for i, path in enumerate(chapter_paths, start=1):
        ch_title, body_xhtml, _has_notes = chapter_md_to_xhtml(path)
        item = epub.EpubHtml(
            title=ch_title, file_name=f"chapter_{i:04d}.xhtml", lang=lang
        )
        # ebooklib's chapter template already emits xmlns:epub on <html> (and
        # supplies <head>/<title>), so content is just the inner body markup.
        item.content = body_xhtml
        item.add_link(href="style/main.css", rel="stylesheet", type="text/css")
        book.add_item(item)
        items.append(item)

    book.toc = tuple(items)  # flat TOC, one entry per chapter

    cover_path = Path(paths["covers"]) / "cover.jpg"
    if cover_path.exists():
        book.set_cover("cover.jpg", cover_path.read_bytes())

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *items]

    out_dir = Path(paths["export"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slugify(title)}.epub"
    epub.write_epub(str(out_path), book)
    print(f"[epub] wrote {out_path}")

    if skip_check:
        return out_path, None, ""
    ok, output = run_epubcheck(out_path)
    return out_path, ok, output


def run_epubcheck(epub_path: Path) -> tuple[bool | None, str]:
    """Validate with the epubcheck docker image. Returns (ok, output); ok is
    None when epubcheck could not be run at all. Never raises."""
    epub_path = Path(epub_path)
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{epub_path.parent.resolve()}:/data",
        "epubcheck",
        f"/data/{epub_path.name}",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=300
        )
    except FileNotFoundError:
        return None, "[warn] docker not found; epubcheck skipped"
    except subprocess.TimeoutExpired:
        return None, "[warn] epubcheck timed out after 300s"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
