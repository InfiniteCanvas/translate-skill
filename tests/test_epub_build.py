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
- os.replace racing an open reader (Windows PermissionError) is retried
  with exponential backoff: two failed swaps followed by a success still
  produce the final epub, while a replace that never succeeds re-raises
  after seven attempts (0.1s..3.2s backoff) with the tmp cleaned up;
- docker infrastructure failures (exit 125 or a known exit-1 pattern:
  daemon down, image missing, daemon-socket permission denied, platform
  manifest mismatch, credential-helper failure, pull rate limit) read as
  "could not run" (ok=None), never as a validation failure -- while an
  epubcheck-side "permission denied" finding still reads as a validation
  failure, and the fake docker run records its kwargs so the round-2
  hardening (stdin=DEVNULL, timeout=300) stays pinned;
- a covers/cover.jpg fixture is embedded into the epub (the written zip
  carries EPUB/cover.jpg with the exact source bytes), while a project
  without covers/ embeds no cover;
- translate._maybe_autobuild's ok tri-state pins: epub.build returning
  ok=None prints the exact "could not run (epubcheck unavailable)" warn
  (the skip path, never a failure), ok=True the [epub-auto] ok line, and
  ok=False the failed-validation warn -- and changed=False prints nothing;
- translate.cmd_build_epub with an ok=None build exits 0 with the
  "[warn] epubcheck skipped" line (the ok-is-None consumer half).

translate.py is imported for the autobuild cases, and it pulls the whole
lib package, so the deps below also carry requests + pillow.

Self-contained PASS/FAIL script (no pytest). epub.py imports ebooklib, so
run via uv (deps declared inline below):

    uv run tests/test_epub_build.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import epub as E
from lib import project as P
import translate

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


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


CHAPTERS = {
    "CHAPTER_0001.md": (
        "---\n"
        "chapter_title: 第一章 原子\n"
        "title: Atomic One\n"
        "---\n"
        "\n"
        "The first chapter body.\n"
    ),
    "CHAPTER_0002.md": (
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


JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xd9"
)


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
    retried with exponential backoff: two busy failures then a success
    still land the epub with no tmp left; a replace that never succeeds
    re-raises after seven attempts (0.1s..3.2s backoff) with the tmp
    cleaned up and no final file. The os attribute on BOTH the epub module
    (tmp naming) and the project module (the shared _replace_with_retry
    resolves os.replace there) is swapped for a shim (getpid delegates,
    replace is scripted), and project time for a sleep recorder, so the
    retry schedule is pinned without real sleeping -- the real stdlib
    modules are never touched."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        _export, final = export_paths(root)
        orig_os, orig_time = E.os, P.time
        attempts = {"n": 0}
        sleeps: list[float] = []

        def flaky_replace(src, dst):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise PermissionError("destination busy")
            return orig_os.replace(src, dst)

        E.os = SimpleNamespace(getpid=orig_os.getpid, replace=flaky_replace)
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=flaky_replace)
        P.time = SimpleNamespace(sleep=sleeps.append)
        try:
            _res, _out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                      skip_check=True)
        finally:
            E.os = orig_os
            P.os = orig_os
            P.time = orig_time
        check("3a retry: build survives two busy replaces",
              exc is None, f"exc={exc!r}")
        check("3b retry: three replace attempts, backoff 0.1s then 0.2s",
              attempts["n"] == 3 and sleeps == [0.1, 0.2],
              f"attempts={attempts['n']} sleeps={sleeps!r}")
        check("3c retry: final epub present, no tmp sibling",
              final.is_file() and zipfile.is_zipfile(final)
              and list(final.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in final.parent.glob('*.tmp')]}")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        _export, final = export_paths(root)
        orig_os, orig_time = E.os, P.time
        attempts = {"n": 0}
        sleeps: list[float] = []

        def busy_forever(src, dst):
            attempts["n"] += 1
            raise PermissionError("destination busy")

        E.os = SimpleNamespace(getpid=orig_os.getpid, replace=busy_forever)
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=busy_forever)
        P.time = SimpleNamespace(sleep=sleeps.append)
        try:
            _res, _out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                      skip_check=True)
        finally:
            E.os = orig_os
            P.os = orig_os
            P.time = orig_time
        check("3d retry: a never-free destination re-raises PermissionError",
              isinstance(exc, PermissionError), f"exc={exc!r}")
        check("3e retry: seven attempts before giving up",
              attempts["n"] == 7, f"attempts={attempts['n']}")
        check("3f retry: full backoff schedule 0.1s..3.2s",
              sleeps == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2], f"sleeps={sleeps!r}")
        check("3g retry: no final epub and no tmp sibling left",
              not final.exists()
              and list(final.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in final.parent.glob('*.tmp')]}")


def case_4_docker_infra_tri_state() -> None:
    """Docker infrastructure trouble reads as 'could not run' (None), never
    as a validation failure for a good epub: _docker_infra_failure flags
    exit 125 and the daemon-down / image-missing / connection errors
    (case-insensitively) and nothing else, and run_epubcheck -- with the
    epub module's subprocess swapped for a scripted fake, per the file's
    attribute-swap convention (the fake records the run kwargs, pinning the
    round-2 hardening) -- maps exit 125 and an 'error during connect'
    exit 1 to (None, output) while a genuine validation failure stays
    (False, output) and exit 0 stays (True, output)."""
    pairs = [
        (125, "", True),
        (1, "docker: Cannot connect to the Docker daemon at "
            "unix:///var/run/docker.sock. Is the docker daemon running?", True),
        (1, "docker: Unable to find image 'epubcheck:latest' locally", True),
        (1, "Error During Connect: The client cannot connect to the daemon",
         True),
        (1, "There were validation errors in the EPUB", False),
        (1, "", False),
        (2, "some other docker failure", False),
        (127, "command not found", False),
        (1, "Error response from daemon: pull access denied for epubcheck",
         True),
        (1, "Error response from daemon: Could not select a version for the "
            "image", True),
        (1, "dial tcp: lookup epubcheck on host: no such host", True),
        (1, "manifest for epubcheck:1.0 not found: manifest unknown", True),
        (1, "docker: permission denied while trying to connect to the "
            "docker daemon socket at unix:///var/run/docker.sock", True),
        (1, "docker: no matching manifest for windows/amd64 in the "
            "manifest list entries", True),
        (1, "error getting credentials - err: exec: "
            "\"docker-credential-desktop\": executable file not found", True),
        (1, "toomanyrequests: You have reached your pull rate limit", True),
        (1, "ERROR: permission denied to write output file", False),
    ]
    for i, (rc, output, expected) in enumerate(pairs):
        got = E._docker_infra_failure(rc, output)
        check(f"4a-{i} infra: rc={rc} output classified {expected}",
              got == expected, f"got={got!r}")

    def run_with(rc: int, stdout: str, stderr: str):
        orig_subprocess = E.subprocess
        recorded: dict = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            recorded["kwargs"] = kwargs
            return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

        E.subprocess = SimpleNamespace(
            run=fake_run,
            TimeoutExpired=subprocess.TimeoutExpired,
            FileNotFoundError=FileNotFoundError,
            DEVNULL=subprocess.DEVNULL,
        )
        try:
            ok, out = E.run_epubcheck(Path("nowhere") / "book.epub")
        finally:
            E.subprocess = orig_subprocess
        return ok, out, recorded

    ok, out, recorded = run_with(125, "docker: Error response from daemon.", "")
    check("4b epubcheck: exit 125 -> (None, output)",
          ok is None and out == "docker: Error response from daemon.",
          f"ok={ok!r} out={out!r}")
    ok, out, _ = run_with(1, "", "error during connect: daemon down")
    check("4c epubcheck: connect-error exit 1 -> (None, output)",
          ok is None and out == "error during connect: daemon down",
          f"ok={ok!r} out={out!r}")
    ok, out, _ = run_with(1, "ERROR ITunes: assets not present in the OPF", "")
    check("4d epubcheck: real validation failure stays (False, output)",
          ok is False and "ERROR ITunes" in out, f"ok={ok!r} out={out!r}")
    ok, out, _ = run_with(0, "No errors or warnings detected", "")
    check("4e epubcheck: exit 0 stays (True, output)",
          ok is True and out == "No errors or warnings detected",
          f"ok={ok!r} out={out!r}")
    check("4f epubcheck: docker run stdin=DEVNULL",
          recorded["kwargs"].get("stdin") is subprocess.DEVNULL,
          f"kwargs={recorded['kwargs']!r}")
    check("4g epubcheck: docker run timeout=300",
          recorded["kwargs"].get("timeout") == 300,
          f"kwargs={recorded['kwargs']!r}")


def make_autobuild_project(td: str) -> Path:
    """Sandbox with the two files _maybe_autobuild/cmd_build_epub read
    directly: config.json (for cmd_build_epub's strict load and the
    auto_build_epub gate) and novel_info.json (the autobuild's guard)."""
    root = Path(td)
    write_lf(root / "config.json", '{"providers": {}}\n')
    write_lf(root / "novel_info.json",
             json.dumps(NOVEL_INFO, ensure_ascii=False, indent=2) + "\n")
    return root


def case_5_autobuild_tri_state() -> None:
    """_maybe_autobuild's ok tri-state, with epub.build swapped for a
    scripted fake (the file's attribute-swap convention): ok=None is the
    skip path -- the exact "could not run (epubcheck unavailable)" warn,
    never a validation failure -- while ok=True prints the [epub-auto] ok
    line and ok=False the failed-validation warn; changed=False prints
    nothing at all. cmd_build_epub consumes the same ok=None build as a
    clean exit 0 with "[warn] epubcheck skipped"."""
    fake_path = Path("R:/fake/export/book.epub")

    with tempfile.TemporaryDirectory() as td:
        root = make_autobuild_project(td)
        orig_build = translate.epub.build
        translate.epub.build = lambda *a, **k: (fake_path, None, "")
        try:
            _res, out, exc = capture(
                translate._maybe_autobuild, root, {"target_lang": "en"},
                "glossary replace", True)
        finally:
            translate.epub.build = orig_build
        check("5a autobuild ok=None: no raise (the skip path, not failure)",
              exc is None, f"exc={exc!r}")
        check("5b autobuild ok=None: exact warn",
              out == "[warn] epub auto-build could not run (epubcheck "
                     "unavailable) - skipping validation\n",
              f"out={out!r}")

    with tempfile.TemporaryDirectory() as td:
        root = make_autobuild_project(td)
        orig_build = translate.epub.build
        translate.epub.build = lambda *a, **k: (fake_path, True, "")
        try:
            _res, out, exc = capture(
                translate._maybe_autobuild, root, {"target_lang": "en"},
                "glossary replace", True)
        finally:
            translate.epub.build = orig_build
        check("5c autobuild ok=True: exact ok line",
              exc is None
              and out == f"[epub-auto] build ok (after glossary replace): "
                         f"{fake_path}\n",
              f"exc={exc!r} out={out!r}")

    with tempfile.TemporaryDirectory() as td:
        root = make_autobuild_project(td)
        orig_build = translate.epub.build
        translate.epub.build = lambda *a, **k: (fake_path, False, "")
        try:
            _res, out, exc = capture(
                translate._maybe_autobuild, root, {"target_lang": "en"},
                "glossary replace", True)
        finally:
            translate.epub.build = orig_build
        check("5d autobuild ok=False: exact failed-validation warn",
              exc is None
              and out == f"[warn] epub auto-build failed validation: "
                         f"{fake_path}\n",
              f"exc={exc!r} out={out!r}")

    with tempfile.TemporaryDirectory() as td:
        root = make_autobuild_project(td)
        _res, out, exc = capture(
            translate._maybe_autobuild, root, {"target_lang": "en"},
            "glossary replace", False)
        check("5e autobuild changed=False: silent no-op",
              exc is None and out == "", f"exc={exc!r} out={out!r}")

    with tempfile.TemporaryDirectory() as td:
        root = make_autobuild_project(td)
        orig_build = translate.epub.build
        translate.epub.build = lambda *a, **k: (fake_path, None, "")
        try:
            res, out, exc = capture(
                translate.cmd_build_epub,
                argparse.Namespace(skip_check=False), root)
        finally:
            translate.epub.build = orig_build
        check("5f build-epub ok=None: exit 0, no raise",
              exc is None and res == 0, f"res={res} exc={exc!r}")
        check("5g build-epub ok=None: [ok] epub line then the exact skip "
              "warn",
              out == f"[ok] epub: {fake_path}\n[warn] epubcheck skipped\n",
              f"out={out!r}")


def case_6_cover_embed() -> None:
    """covers/cover.jpg is embedded into the built epub: the written zip
    carries the EPUB/cover.jpg member with the EXACT source bytes (plus
    ebooklib's cover page), while a project without covers/ embeds no
    cover at all (the exists() gate stays shut)."""
    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        cover = root / "covers" / "cover.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(JPEG_BYTES)
        _export, final = export_paths(root)
        res, out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                skip_check=True)
        check("6a cover: build succeeds",
              exc is None and res is not None, f"exc={exc!r}")
        out_path = res[0] if res else None
        check("6b cover: the build still lands the final epub",
              out_path == final and final.is_file(), f"out_path={out_path!r}")
        members: list[str] = []
        with zipfile.ZipFile(final) as zf:
            members = zf.namelist()
            embedded = zf.read("EPUB/cover.jpg") if "EPUB/cover.jpg" in \
                members else None
        check("6c cover: the zip carries EPUB/cover.jpg (and the cover page)",
              "EPUB/cover.jpg" in members and "EPUB/cover.xhtml" in members,
              f"members={[m for m in members if 'cover' in m]!r}")
        check("6d cover: embedded bytes are the exact source JPEG",
              embedded == JPEG_BYTES,
              f"got={None if embedded is None else len(embedded)} bytes")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        _export, final = export_paths(root)
        res, _out, exc = capture(E.build, root, NOVEL_INFO, CFG,
                                 skip_check=True)
        check("6e no-cover control: build succeeds",
              exc is None and res is not None, f"exc={exc!r}")
        with zipfile.ZipFile(final) as zf:
            members = zf.namelist()
        check("6f no-cover control: no cover member in the zip",
              not any("cover" in m for m in members),
              f"members={[m for m in members if 'cover' in m]!r}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_success_atomic()
    case_2_write_failure_cleans_tmp()
    case_3_replace_retry()
    case_4_docker_infra_tri_state()
    case_5_autobuild_tri_state()
    case_6_cover_embed()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
