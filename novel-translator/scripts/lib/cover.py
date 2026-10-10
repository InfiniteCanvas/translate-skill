"""Cover image acquisition for novel-translator projects.

Guarantees ``covers/cover.jpg`` exists: scrapes an image from the novel's
source page (or a direct cover URL), falling back to a generated gradient
cover with the title on it.
"""

from __future__ import annotations

import html as _html
import io
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from lib import project

COVER_SIZE = (1600, 2560)
JPEG_QUALITY = 88
MAX_IMAGE_BYTES = 10 * 1024 * 1024
SCRAPE_DEADLINE_S = 30
_CHUNK_SIZE = 64 * 1024

_HEADERS = {"User-Agent": "Mozilla/5.0 (novel-translator)"}

_META_PATTERNS = (
    re.compile(
        r'<meta\s+[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', re.I
    ),
    re.compile(
        r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', re.I
    ),
    re.compile(
        r'<meta\s+[^>]*name=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', re.I
    ),
    re.compile(
        r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']og:image["\']', re.I
    ),
    re.compile(
        r'<meta\s+[^>]*name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']', re.I
    ),
    re.compile(
        r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']twitter:image["\']', re.I
    ),
)

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)

_GRADIENT_TOP = (0x1B, 0x1B, 0x3A)
_GRADIENT_BOTTOM = (0x0F, 0x2E, 0x2E)

_TEXT_FILL = (235, 235, 240)
_TEXT_SHADOW = (8, 8, 16)


def _ascii(text: str) -> str:
    """Replace non-ASCII with '?' before printing. The CLI already forces
    UTF-8 consoles, so this is a belt-and-braces guard for cover diagnostics
    (URLs may carry arbitrary bytes) -- not an output convention."""
    return str(text).encode("ascii", "replace").decode("ascii")


def _save_jpeg(image: Image.Image, path: Path) -> None:
    """Write a JPEG atomically: encode into a pid-suffixed tmp sibling, then
    os.replace() it into place (project's Windows retry/backoff). A partial
    direct write must never survive -- an existing cover.jpg short-circuits
    every later run (ensure_cover's fast path, epub.build's set_cover), so a
    truncated image would ship forever."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        image.save(tmp, "JPEG", quality=JPEG_QUALITY)
        project._replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _record_placeholder(project_dir: Path, placeholder: bool) -> None:
    """Record the cover outcome in novel_info.json's "cover_placeholder" key:
    True after a placeholder was generated (a failed scrape must stay visible
    in the project instead of silently shipping a gradient cover forever),
    removed again once a real scrape succeeds. Written only on change, in
    init's JSON style (2-space indent, ensure_ascii=False, trailing newline)
    through the atomic write. A missing novel_info.json is left alone --
    creating it here would change how later commands read the project. Best
    effort: bookkeeping must never break the cover."""
    path = project.paths(project_dir)["novel_info"]
    if not path.is_file():
        return
    try:
        info = project.load_novel_info(project_dir)
        if placeholder:
            if info.get("cover_placeholder") is True:
                return
            info["cover_placeholder"] = True
        else:
            if "cover_placeholder" not in info:
                return
            del info["cover_placeholder"]
        project.atomic_write_text(
            path, json.dumps(info, ensure_ascii=False, indent=2) + "\n",
            newline="\n",
        )
    except Exception as exc:
        print(_ascii(f"[warn] cover: recording placeholder state failed: {exc}"))


def ensure_cover(
    project_dir: Path, novel_info: dict, cover_url: str | None = None
) -> Path:
    """Guarantee covers/cover.jpg exists and return its path."""
    cover_path = Path(project.paths(project_dir)["covers"]) / "cover.jpg"
    if cover_path.exists():
        return cover_path
    cover_path.parent.mkdir(parents=True, exist_ok=True)

    url = cover_url or novel_info.get("source_url")
    if url:
        try:
            data = scrape_image(url)
        except Exception as exc:
            print(_ascii(f"[warn] cover scrape raised {type(exc).__name__}: {exc}"))
            data = None
        if data:
            try:
                with Image.open(io.BytesIO(data)) as im:
                    im = ImageOps.fit(
                        im.convert("RGB"), COVER_SIZE, Image.Resampling.LANCZOS
                    )
                    _save_jpeg(im, cover_path)
                print(_ascii(f"[cover] scraped from {url}"))
                _record_placeholder(project_dir, placeholder=False)
                return cover_path
            except Exception as exc:
                print(_ascii(f"[warn] cover image processing failed: {exc}"))
        else:
            print(_ascii(f"[cover] no image found at {url}"))

    title = novel_info.get("title_translated") or novel_info.get("title") or "Untitled"
    author = novel_info.get("author") or "Unknown"
    generate_cover(title, author, cover_path)
    print("[cover] no image found; generated placeholder")
    _record_placeholder(project_dir, placeholder=True)
    return cover_path


def _read_capped(resp: requests.Response, deadline: float) -> bytes | None:
    """Drain a streamed response in 64 KB chunks, enforcing MAX_IMAGE_BYTES
    and the wall-clock deadline between chunks; closes the connection and
    returns None on any breach or read error. requests' timeout=15 only
    bounds connect/idle reads -- without the cap a huge or trickle-feeding
    body could stall a run indefinitely."""
    buf = bytearray()
    try:
        for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
            if time.monotonic() > deadline:
                return None
            buf.extend(chunk)
            if len(buf) > MAX_IMAGE_BYTES:
                return None
    except Exception:
        return None
    finally:
        resp.close()
    return bytes(buf)


def scrape_image(url: str) -> bytes | None:
    """Fetch image bytes from a direct image URL, or scrape og:image /
    twitter:image from an HTML page. Returns None on any failure; never
    raises. Bodies are streamed and capped at MAX_IMAGE_BYTES under a
    SCRAPE_DEADLINE_S wall-clock deadline checked between stages and chunks."""
    deadline = time.monotonic() + SCRAPE_DEADLINE_S
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS, stream=True)
        if resp.status_code >= 400:
            resp.close()
            return None
        if time.monotonic() > deadline:
            resp.close()
            return None
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype.startswith("image/"):
            return _read_capped(resp, deadline)
        if ctype and "html" not in ctype:
            resp.close()
            return None

        page = _read_capped(resp, deadline)
        if page is None:
            return None
        text = page.decode(resp.encoding or "utf-8", "replace")
        tried = set()
        for pattern in _META_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            src = _html.unescape(match.group(1)).strip()
            if not src:
                continue
            image_url = urljoin(url, src)
            if image_url in tried or time.monotonic() > deadline:
                continue
            tried.add(image_url)
            resp2 = requests.get(image_url, timeout=15, headers=_HEADERS, stream=True)
            if resp2.status_code >= 400:
                resp2.close()
                continue
            ctype2 = (resp2.headers.get("content-type") or "").split(";")[0]
            ctype2 = ctype2.strip().lower()
            if not ctype2.startswith("image/"):
                resp2.close()
                continue
            data = _read_capped(resp2, deadline)
            if data is not None:
                return data
        return None
    except Exception:
        return None


def _load_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def _tokenize(text: str) -> list[str]:
    """Latin words stay whole; every other character (CJK, punctuation) is its
    own token so wrapping works for both scripts."""
    return re.findall(r"[A-Za-z0-9']+|\s+|.", text)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in _tokenize(text):
        if current and draw.textlength(current + token, font=font) > max_width:
            lines.append(current.rstrip())
            current = ""
            token = token.lstrip()
            if not token:
                continue
        current += token
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _fit_lines(
    draw: ImageDraw.ImageDraw, text: str, start_size: int, max_width: float, max_lines: int
) -> tuple[ImageFont.FreeTypeFont | None, list[str]]:
    """Shrink the font until the wrapped text fits max_lines lines of max_width."""
    size = start_size
    while True:
        font = _load_font(size)
        if font is None:
            return None, []
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
        if size <= 36:
            return font, lines[:max_lines]
        size = max(36, int(size * 0.85))


def _draw_centered(
    draw: ImageDraw.ImageDraw, text: str, font, y: int, width: int
) -> None:
    x = (width - draw.textlength(text, font=font)) / 2
    draw.text((x + 6, y + 6), text, font=font, fill=_TEXT_SHADOW)
    draw.text((x, y), text, font=font, fill=_TEXT_FILL)


def generate_cover(title: str, author: str, out_path: Path) -> None:
    """Generate a 1600x2560 JPEG gradient cover with title and author."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = COVER_SIZE

    image = Image.new("RGB", COVER_SIZE)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / (height - 1)
        color = tuple(
            round(a + (b - a) * t) for a, b in zip(_GRADIENT_TOP, _GRADIENT_BOTTOM)
        )
        draw.line([(0, y), (width, y)], fill=color)

    title_font, title_lines = _fit_lines(
        draw, title, start_size=240, max_width=width * 0.8, max_lines=6
    )
    if title_font is not None:
        line_height = int(title_font.size * 1.28)
        block_height = line_height * len(title_lines)
        y = int(height * 0.42) - block_height // 2
        for line in title_lines:
            _draw_centered(draw, line, title_font, y, width)
            y += line_height

        author_size = max(48, min(120, int(title_font.size * 0.5)))
        author_font = None
        while author_size >= 30:
            author_font = _load_font(author_size)
            if author_font is None:
                break
            if draw.textlength(author, font=author_font) <= width * 0.8:
                break
            author_size = max(30, int(author_size * 0.85))
        if author_font is not None and author:
            _draw_centered(draw, author, author_font, height - 340, width)

    _save_jpeg(image, out_path)
