# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "pillow>=10.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import cover as C
from lib import project as P
from PIL import Image

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def capture(fn, *args, **kwargs):
    """fn(*args, **kwargs) with stdout captured; returns (result, output,
    exc) -- result is None when the call raised."""
    buf = io.StringIO()
    result = None
    exc: Exception | None = None
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except Exception as caught:
        exc = caught
    return result, buf.getvalue(), exc


def write_novel_info(proj: Path, info: dict) -> None:
    """novel_info.json in init's style: 2-space indent, ensure_ascii=False,
    trailing newline, atomic write."""
    P.atomic_write_text(
        proj / "novel_info.json",
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )


def tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 10)).save(buf, "JPEG")
    return buf.getvalue()


class FakeResp:
    """A streamed-response stand-in: fixed status/headers, iter_content
    replaying pre-baked chunks, close() recorded."""

    def __init__(self, status_code=200, headers=None, chunks=(), encoding=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        self.closed = True


def install_fake_requests(responses: list[FakeResp]) -> tuple[list[dict], callable]:
    """Swap cover.requests for a module shim serving the queued responses;
    returns (recorded calls, restore)."""
    calls: list[dict] = []
    orig_requests = C.requests

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return responses.pop(0)

    C.requests = SimpleNamespace(get=fake_get)
    return calls, (lambda: setattr(C, "requests", orig_requests))


def case_1_save_jpeg_atomic() -> None:
    """_save_jpeg writes the JPEG atomically: the image lands at the target
    path as a readable JPEG with no *.tmp sibling, and a failing replace
    cleans the tmp up and leaves no target (a partial cover.jpg would
    otherwise satisfy the exists() fast paths forever)."""
    with tempfile.TemporaryDirectory() as td:
        covers = Path(td) / "covers"
        covers.mkdir()
        target = covers / "cover.jpg"
        C._save_jpeg(Image.new("RGB", (64, 32), (200, 10, 10)), target)
        check("1a save_jpeg: target written as a readable JPEG",
              target.is_file()
              and Image.open(target).format == "JPEG"
              and Image.open(target).size == (64, 32),
              f"exists={target.is_file()}")
        check("1b save_jpeg: no *.tmp sibling left",
              list(covers.glob("*.tmp")) == [],
              f"tmp={[p.name for p in covers.glob('*.tmp')]}")

    with tempfile.TemporaryDirectory() as td:
        covers = Path(td) / "covers"
        covers.mkdir()
        target = covers / "cover.jpg"

        def boom(src, dst):
            raise PermissionError("destination busy")

        orig_replace = P._replace_with_retry
        P._replace_with_retry = boom
        try:
            _res, _out, exc = capture(C._save_jpeg,
                                      Image.new("RGB", (8, 8)), target)
        finally:
            P._replace_with_retry = orig_replace
        check("1c save_jpeg: failing replace propagates",
              isinstance(exc, PermissionError), f"exc={exc!r}")
        check("1d save_jpeg: no target and no tmp after the failure",
              not target.exists() and list(covers.glob("*.tmp")) == [],
              f"target={target.exists()} "
              f"tmp={[p.name for p in covers.glob('*.tmp')]}")


def case_2_record_placeholder() -> None:
    """_record_placeholder sets "cover_placeholder": true only on change,
    removes it on a successful scrape, rewrites in init's JSON style (2-space
    indent, ensure_ascii=False, trailing LF), and never creates a missing
    novel_info.json."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        write_novel_info(proj, {"title": "测试小说", "author": "作者"})
        raw_before = (proj / "novel_info.json").read_bytes()

        _res, _out, exc = capture(C._record_placeholder, proj, True)
        raw = (proj / "novel_info.json").read_bytes()
        info = json.loads(raw.decode("utf-8"))
        check("2a record: placeholder flag set, no exception",
              exc is None and info.get("cover_placeholder") is True,
              f"exc={exc!r} info={info!r}")
        check("2b record: other keys intact",
              info.get("title") == "测试小说" and info.get("author") == "作者",
              f"info={info!r}")
        check("2c record: init's JSON style (CJK raw, 2-space indent, LF)",
              "测试小说".encode("utf-8") in raw and b"\\u6d4b" not in raw
              and raw.endswith(b"\n") and b"\r" not in raw
              and b'\n  "cover_placeholder": true' in raw,
              f"raw={raw!r}")

        _res, _out, exc = capture(C._record_placeholder, proj, True)
        check("2d record: setting True again is a byte-level no-op",
              exc is None and (proj / "novel_info.json").read_bytes() == raw,
              f"exc={exc!r}")

        _res, _out, exc = capture(C._record_placeholder, proj, False)
        info = json.loads((proj / "novel_info.json").read_text(encoding="utf-8"))
        check("2e record: a successful scrape removes the flag",
              exc is None and "cover_placeholder" not in info
              and info.get("title") == "测试小说", f"exc={exc!r} info={info!r}")

        raw_after_clear = (proj / "novel_info.json").read_bytes()
        _res, _out, exc = capture(C._record_placeholder, proj, False)
        check("2f record: clearing an absent flag is a byte-level no-op",
              exc is None
              and (proj / "novel_info.json").read_bytes() == raw_after_clear,
              f"exc={exc!r}")
        check("2g record: set->clear round-trip restores the original bytes",
              raw_after_clear == raw_before, "")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _res, _out, exc = capture(C._record_placeholder, proj, True)
        check("2h record: missing novel_info.json is left alone",
              exc is None and not (proj / "novel_info.json").exists(),
              f"exc={exc!r}")


def case_3_read_capped() -> None:
    """_read_capped drains a streamed body in chunks under the byte cap and
    the wall-clock deadline, closing the connection on every exit path."""
    deadline = time.monotonic() + C.SCRAPE_DEADLINE_S
    resp = FakeResp(chunks=[b"ab", b"cd"])
    data = C._read_capped(resp, deadline)
    check("3a capped: small body drained fully",
          data == b"abcd" and resp.closed, f"data={data!r} closed={resp.closed}")

    meg = b"x" * (1024 * 1024)
    resp = FakeResp(chunks=[meg] * 17)
    data = C._read_capped(resp, deadline)
    check("3b capped: body over MAX_IMAGE_BYTES -> None",
          data is None and resp.closed, f"data={data!r} closed={resp.closed}")

    resp = FakeResp(chunks=[b"stale"])
    data = C._read_capped(resp, time.monotonic() - 1)
    check("3c capped: expired deadline -> None without consuming",
          data is None and resp.closed, f"data={data!r} closed={resp.closed}")

    class BrokenResp:
        closed = False

        def iter_content(self, chunk_size=None):
            raise OSError("connection reset")

        def close(self):
            self.closed = True

    broken = BrokenResp()
    data = C._read_capped(broken, deadline)  # type: ignore[arg-type]
    check("3d capped: read errors -> None, connection still closed",
          data is None and broken.closed, f"data={data!r} closed={broken.closed}")


def case_4_scrape_hardening() -> None:
    """scrape_image streams with timeout=15 + stream=True, caps bodies at
    MAX_IMAGE_BYTES under the deadline (close on breach), and still resolves
    og:image from an HTML page through a second capped fetch."""
    image = tiny_jpeg()

    calls, restore = install_fake_requests(
        [FakeResp(headers={"content-type": "image/jpeg"}, chunks=[image])])
    try:
        data = C.scrape_image("http://example.com/cover.jpg")
    finally:
        restore()
    check("4a scrape: direct image returned",
          data == image, f"data={data!r}")
    check("4b scrape: requested with stream=True and timeout=15",
          calls and calls[0].get("stream") is True
          and calls[0].get("timeout") == 15, f"calls={calls!r}")

    resp = FakeResp(headers={"content-type": "image/jpeg"},
                    chunks=[b"y" * (1024 * 1024)] * 17)
    calls, restore = install_fake_requests([resp])
    try:
        data = C.scrape_image("http://example.com/huge.jpg")
    finally:
        restore()
    check("4c scrape: oversized image -> None, connection closed",
          data is None and resp.closed, f"data={data!r} closed={resp.closed}")

    resp = FakeResp(status_code=404)
    calls, restore = install_fake_requests([resp])
    try:
        data = C.scrape_image("http://example.com/missing.jpg")
    finally:
        restore()
    check("4d scrape: 404 -> None, connection closed",
          data is None and resp.closed, f"data={data!r} closed={resp.closed}")

    html = ('<html><head><meta property="og:image" '
            'content="/img/cover.jpg"></head></html>').encode("utf-8")
    image_resp = FakeResp(headers={"content-type": "image/jpeg"},
                          chunks=[image])
    calls, restore = install_fake_requests(
        [FakeResp(headers={"content-type": "text/html; charset=utf-8"},
                  chunks=[html]),
         image_resp])
    try:
        data = C.scrape_image("http://example.com/page")
    finally:
        restore()
    check("4e scrape: og:image resolved from a streamed HTML page",
          data == image, f"data={data!r}")
    check("4f scrape: the image fetch used the resolved absolute URL",
          len(calls) == 2 and calls[1]["url"] == "http://example.com/img/cover.jpg",
          f"calls={calls!r}")

    real_monotonic = C.time.monotonic
    state = {"calls": 0}

    def jumpy_monotonic() -> float:
        state["calls"] += 1
        return real_monotonic() if state["calls"] == 1 else real_monotonic() + 10_000

    orig_time = C.time
    C.time = SimpleNamespace(monotonic=jumpy_monotonic)
    calls, restore = install_fake_requests(
        [FakeResp(headers={"content-type": "image/jpeg"}, chunks=[image])])
    try:
        data = C.scrape_image("http://example.com/late.jpg")
    finally:
        restore()
        C.time = orig_time
    check("4g scrape: expired wall-clock deadline -> treated as failure",
          data is None and state["calls"] >= 2, f"data={data!r}")


def case_5_ensure_cover_end_to_end() -> None:
    """ensure_cover with a scripted network: a successful scrape writes the
    image and clears a stale placeholder flag; a failed scrape generates the
    placeholder and records the flag; an existing cover.jpg short-circuits
    without any network; no *.tmp siblings anywhere."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        write_novel_info(proj, {"title": "测试小说", "author": "作者",
                                "source_url": "http://example.com/cover.jpg",
                                "cover_placeholder": True})
        calls, restore = install_fake_requests(
            [FakeResp(headers={"content-type": "image/jpeg"},
                      chunks=[tiny_jpeg()])])
        try:
            res, out, exc = capture(C.ensure_cover, proj,
                                    {"source_url": "http://example.com/cover.jpg"})
        finally:
            restore()
        cover_path = proj / "covers" / "cover.jpg"
        info = json.loads((proj / "novel_info.json").read_text(encoding="utf-8"))
        check("5a ensure: scrape success writes the cover",
              exc is None and res == cover_path
              and Image.open(cover_path).format == "JPEG", f"exc={exc!r}")
        check("5b ensure: console reports the scrape",
              out == "[cover] scraped from http://example.com/cover.jpg\n",
              f"out={out!r}")
        check("5c ensure: stale placeholder flag cleared",
              "cover_placeholder" not in info, f"info={info!r}")
        check("5d ensure: no *.tmp sibling in covers/",
              list((proj / "covers").glob("*.tmp")) == [],
              f"tmp={[p.name for p in (proj / 'covers').glob('*.tmp')]}")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        write_novel_info(proj, {"title": "测试小说", "author": "作者",
                                "source_url": "http://example.com/page"})
        resp = FakeResp(status_code=404)
        calls, restore = install_fake_requests([resp])
        try:
            res, out, exc = capture(C.ensure_cover, proj,
                                    {"source_url": "http://example.com/page"})
        finally:
            restore()
        cover_path = proj / "covers" / "cover.jpg"
        info = json.loads((proj / "novel_info.json").read_text(encoding="utf-8"))
        check("5e ensure: failed scrape -> gradient placeholder generated",
              exc is None and res == cover_path
              and Image.open(cover_path).format == "JPEG"
              and Image.open(cover_path).size == C.COVER_SIZE, f"exc={exc!r}")
        check("5f ensure: console reports no image + the placeholder",
              out == "[cover] no image found at http://example.com/page\n"
                     "[cover] no image found; generated placeholder\n",
              f"out={out!r}")
        check("5g ensure: placeholder flag recorded in novel_info.json",
              info.get("cover_placeholder") is True, f"info={info!r}")
        check("5h ensure: no *.tmp sibling in covers/",
              list((proj / "covers").glob("*.tmp")) == [],
              f"tmp={[p.name for p in (proj / 'covers').glob('*.tmp')]}")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "covers").mkdir()
        (proj / "covers" / "cover.jpg").write_bytes(b"existing bytes")
        write_novel_info(proj, {"title": "测试小说", "cover_placeholder": True})
        raw_before = (proj / "novel_info.json").read_bytes()

        def no_network(url, **kwargs):
            raise AssertionError("network touched despite the existing cover")

        orig_requests = C.requests
        C.requests = SimpleNamespace(get=no_network)
        try:
            res, out, exc = capture(C.ensure_cover, proj, {})
        finally:
            C.requests = orig_requests
        check("5i ensure: existing cover.jpg short-circuits (no network)",
              exc is None and out == ""
              and res == proj / "covers" / "cover.jpg", f"exc={exc!r} out={out!r}")
        check("5j ensure: novel_info.json untouched by the fast path",
              (proj / "novel_info.json").read_bytes() == raw_before, "")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_save_jpeg_atomic()
    case_2_record_placeholder()
    case_3_read_capped()
    case_4_scrape_hardening()
    case_5_ensure_cover_end_to_end()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
