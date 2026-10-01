"""Tests for epub.build's atomic export write.

build() writes export/<slug>.epub through a `<name>.<pid>.tmp` sibling and
os.replace()s it into place, mirroring project.atomic_write_text's pattern
(binary, so not routed through the text helper): a concurrent reader must
never observe a half-written epub at the export path. Covered hermetically
with skip_check=True (no docker, no epubcheck):

- a successful build lands the final file at the normal export path as a
  valid zip (zipfile.is_zipfile; mimetype + chapter items inside) with NO
  *.tmp siblings left in export/, and a rebuild over the existing epub
  stays just as clean;
- a write failure (ebooklib's write_epub swapped for a raiser) propagates,
  leaves no final epub and NO *.tmp sibling behind (the finally-unlink);
- os.replace racing an open reader (Windows PermissionError) is retried:
  two failed swaps followed by a success still produce the final epub,
  while a replace that never succeeds re-raises after its attempts with
  the tmp cleaned up.

Self-contained PASS/FAIL script (no pytest). epub.py imports ebooklib, so
run via uv (deps declared inline below):

    uv run tests/test_epub_build.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["ebooklib>=0.18", "pyyaml>=6.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import epub as E  # noqa: E402
from lib import project as P  # noqa: E402

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
    except Exception as caught:  # noqa: BLE001 - the caller asserts on it
        exc = caught
    return result, buf.getvalue(), exc


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


CHAPTERS = {
    "Chapter_0001.md": (
        "---\n"
        "chapter_title: 第一章 原子\n"
        "title: Atomic One\n"
        "---\n"
        "\n"
        "The first chapter body.\n"
    ),
    "Chapter_0002.md": (
        "---\n"
        "chapter_title: 第二章 原子\n"
        "title: Atomic Two\n"
        "---\n"
        "\n"
        "The second chapter body.\n"
    ),
}
MANIFEST = [
    {"file": name, "number": i + 1, "suffix": "", "order": i,
     "status": "translated", "title": f"Atomic {i + 1}"}
    for i, name in enumerate(sorted(CHAPTERS))
]
NOVEL_INFO = {"title": "原子测试", "title_translated": "Atomic Test Novel",
              "author": "Tester"}
CFG = {"target_lang": "en"}


def make_project(td: str) -> Path:
    root = Path(td)
    for name, text in CHAPTERS.items():
        write_lf(root / "translated" / name, text)
    write_lf(root / "chapters.json",
             json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n")
    return root


def export_paths(root: Path) -> tuple[Path, Path]:
    export = root / "export"
    return export, export / "atomic-test-novel.epub"


# ---------------------------------------------------------------------- cases


def case_1_success_atomic() -> None:
    """A successful build: final epub at the normal path, a valid zip with
    the expected members, the [epub] wrote line, and no *.tmp siblings --
    also across a rebuild over the existing file."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        _export, final = export_paths(root)
        res, out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                skip_check=True)
        check("1a build: returns without raising, epubcheck skipped",
              exc is None and res is not None and res[1] is None
              and res[2] == "",
              f"exc={exc!r} res={res!r}")
        out_path = res[0] if res else None
        check("1b build: final file at the normal export path",
              out_path == final and final.is_file(), f"out_path={out_path!r}")
        check("1c build: console reports the write",
              out == f"[epub] wrote {final}\n", f"out={out!r}")
        check("1d build: export/ holds no *.tmp sibling",
              list(final.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in final.parent.glob('*.tmp')]}")
        check("1e build: the epub is a valid zip", zipfile.is_zipfile(final),
              "")
        with zipfile.ZipFile(final) as zf:
            names = zf.namelist()
        check("1f build: zip members include mimetype and both chapters",
              "mimetype" in names and "EPUB/chapter_0001.xhtml" in names
              and "EPUB/chapter_0002.xhtml" in names, f"names={names}")

        # Rebuild over the existing epub: still atomic, still clean (the
        # rebuilt zip is NOT compared byte-for-byte: ebooklib stamps the
        # OPF with a dcterms:modified clock time).
        _res, _out2, exc2 = capture(E.build, root, NOVEL_INFO, CFG,
                                    skip_check=True)
        check("1g build: rebuild over the existing epub succeeds",
              exc2 is None, f"exc={exc2!r}")
        check("1h build: rebuild leaves no *.tmp sibling",
              list(final.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in final.parent.glob('*.tmp')]}")
        check("1i build: rebuilt epub still a valid zip",
              zipfile.is_zipfile(final), "")


def case_2_write_failure_cleans_tmp() -> None:
    """A failing write_epub propagates and cleans up: no final epub, no
    *.tmp sibling (the finally-unlink), and the error is the original one."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        _export, final = export_paths(root)
        orig_write = E.epub.write_epub

        def boom(path, book):
            raise OSError("simulated disk full")

        E.epub.write_epub = boom
        try:
            _res, _out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                      skip_check=True)
        finally:
            E.epub.write_epub = orig_write
        check("2a failure: the write error propagates out of build",
              isinstance(exc, OSError) and "simulated disk full" in str(exc),
              f"exc={exc!r}")
        check("2b failure: no final epub at the export path",
              not final.exists(), f"final={final}")
        check("2c failure: the tmp sibling was cleaned up",
              list(final.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in final.parent.glob('*.tmp')]}")


def case_3_replace_retry() -> None:
    """os.replace racing an open reader (Windows PermissionError) is
    retried: two busy failures then a success still land the epub with no
    tmp left; a replace that never succeeds re-raises after its attempts
    with the tmp cleaned up and no final file. The os attribute on BOTH
    the epub module (tmp naming) and the project module (the shared
    _replace_with_retry resolves os.replace there) is swapped for a shim
    (getpid delegates, replace is scripted) so the retry loop sees the
    scripted replace while the real stdlib os module is never touched."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        _export, final = export_paths(root)
        orig_os = E.os
        attempts = {"n": 0}

        def flaky_replace(src, dst):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise PermissionError("destination busy")
            return orig_os.replace(src, dst)

        E.os = SimpleNamespace(getpid=orig_os.getpid, replace=flaky_replace)
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=flaky_replace)
        try:
            _res, _out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                      skip_check=True)
        finally:
            E.os = orig_os
            P.os = orig_os
        check("3a retry: build survives two busy replaces",
              exc is None, f"exc={exc!r}")
        check("3b retry: exactly three replace attempts were made",
              attempts["n"] == 3, f"attempts={attempts['n']}")
        check("3c retry: final epub present, no tmp sibling",
              final.is_file() and zipfile.is_zipfile(final)
              and list(final.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in final.parent.glob('*.tmp')]}")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        _export, final = export_paths(root)
        orig_os = E.os
        attempts = {"n": 0}

        def busy_forever(src, dst):
            attempts["n"] += 1
            raise PermissionError("destination busy")

        E.os = SimpleNamespace(getpid=orig_os.getpid, replace=busy_forever)
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=busy_forever)
        try:
            _res, _out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                      skip_check=True)
        finally:
            E.os = orig_os
            P.os = orig_os
        check("3d retry: a never-free destination re-raises PermissionError",
              isinstance(exc, PermissionError), f"exc={exc!r}")
        check("3e retry: five attempts before giving up",
              attempts["n"] == 5, f"attempts={attempts['n']}")
        check("3f retry: no final epub and no tmp sibling left",
              not final.exists()
              and list(final.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in final.parent.glob('*.tmp')]}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_success_atomic()
    case_2_write_failure_cleans_tmp()
    case_3_replace_retry()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
