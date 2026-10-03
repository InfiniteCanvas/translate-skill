"""Tests for project-side file writes: chapter discovery and atomic writes.

Covers project.discover's ASCII-digit rule (CHAPTER_RE spells [0-9], never
\\d: \\d also matches full-width/Arabic-Indic decimal digits, so a
"Chapter_０００７.md" file would be discovered and int()-collapse onto the
real Chapter_0007 -- Unicode-digit names must NOT be discovered while ASCII
"Chapter_0007.md" and the suffixed, differently-cased "chapter_0012b.md"
still are, with re.IGNORECASE preserved for the name and the a/b suffix),
the atomic write helpers: atomic_write_text creates a missing parent
directory (deep paths work on the first write, round-trip their text, and
leave no *.tmp siblings), _replace_with_retry's exponential backoff (seven
attempts, sleeping 0.1s..3.2s between them, then the error surfaces: a
replace failing twice then succeeding lands the file, a permanently busy
destination re-raises after the full schedule, and a failing atomic write
leaves the previous content intact with no tmp sibling) -- os/time on the
project module are swapped for scripted shims per the suite's
attribute-swap convention, no unittest.mock -- and write_chapter routing
through the atomic write (frontmatter + body round-trip byte-stable with
single-LF endings, a failing replace leaves the previous chapter untouched,
and writing into a not-yet-existing directory succeeds via the inherited
parent creation).

Self-contained PASS/FAIL script (no pytest). project.py imports pyyaml, so
run via uv (deps declared inline below):

    uv run tests/test_project_writes.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# scripts/ (and therefore lib/) lives at novel-translator/scripts relative
# to this file (CWD-independent).
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

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


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ---------------------------------------------------------------------- cases


def case_1_discovery_ascii_digits() -> None:
    """discover() matches ASCII digits only: "Chapter_0007.md" and the
    suffixed, differently-cased "chapter_0012b.md" are discovered (IGNORECASE
    preserved), while full-width and Arabic-Indic digit twins, 5-digit
    numbers, and wrong extensions are not."""
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "source"
        source.mkdir()
        names = [
            "Chapter_0007.md",    # discovered, number 7
            "chapter_0012b.md",   # discovered, number 12, suffix b
            "Chapter_0099A.md",   # discovered: IGNORECASE keeps matching the A
            "Chapter_12.md",      # 2 digits are inside the 1-4 bound
            "Chapter_０００７.md",  # full-width digits: NOT discovered
            "Chapter_٠٠٠٧.md",    # Arabic-Indic digits: NOT discovered
            "Chapter_12345.md",   # 5 digits: NOT discovered
            "Chapter_0007.txt",   # wrong extension: NOT discovered
        ]
        for name in names:
            write_lf(source / name, "正文\n")
        found = P.discover(Path(td))
        by_name = {c.file: c for c in found}
        check("1a discover: ASCII 4-digit chapter discovered",
              by_name.get("Chapter_0007.md") is not None
              and by_name["Chapter_0007.md"].number == 7,
              f"found={[c.file for c in found]!r}")
        check("1b discover: lowercase name + suffix still discovered",
              by_name.get("chapter_0012b.md") is not None
              and by_name["chapter_0012b.md"].number == 12
              and by_name["chapter_0012b.md"].suffix == "b",
              f"found={[c.file for c in found]!r}")
        check("1c discover: uppercase suffix matches (IGNORECASE)",
              by_name.get("Chapter_0099A.md") is not None
              and by_name["Chapter_0099A.md"].suffix == "A",
              f"found={[c.file for c in found]!r}")
        check("1d discover: 2-digit chapter inside the 1-4 bound",
              by_name.get("Chapter_12.md") is not None
              and by_name["Chapter_12.md"].number == 12,
              f"found={[c.file for c in found]!r}")
        check("1e discover: full-width digits NOT discovered",
              "Chapter_０００７.md" not in by_name,
              f"found={[c.file for c in found]!r}")
        check("1f discover: Arabic-Indic digits NOT discovered",
              "Chapter_٠٠٠٧.md" not in by_name,
              f"found={[c.file for c in found]!r}")
        check("1g discover: 5-digit number NOT discovered",
              "Chapter_12345.md" not in by_name,
              f"found={[c.file for c in found]!r}")
        check("1h discover: wrong extension NOT discovered",
              "Chapter_0007.txt" not in by_name,
              f"found={[c.file for c in found]!r}")
        check("1i discover: exactly the four ASCII chapters, sorted by number",
              [c.file for c in found] == ["Chapter_0007.md", "Chapter_12.md",
                                          "chapter_0012b.md", "Chapter_0099A.md"],
              f"found={[c.file for c in found]!r}")


def case_2_atomic_write_creates_parent() -> None:
    """atomic_write_text creates a missing parent directory: a deep, not
    yet existing path works on the first write, round-trips its text with
    LF endings, and leaves no *.tmp sibling; a rewrite over the existing
    file stays just as clean."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "a" / "b" / "chapters.json"
        P.atomic_write_text(target, '{"k": "值"}\n', newline="\n")
        check("2a atomic: deep missing parent created, content written",
              target.is_file()
              and target.read_text(encoding="utf-8") == '{"k": "值"}\n',
              f"exists={target.is_file()}")
        raw = target.read_bytes()
        check("2b atomic: LF-only bytes with the trailing newline",
              b"\r" not in raw and raw.endswith(b"\n"), f"raw={raw!r}")
        check("2c atomic: no *.tmp sibling left",
              list(target.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in target.parent.glob('*.tmp')]}")
        P.atomic_write_text(target, '{"k": 2}\n', newline="\n")
        check("2d atomic: rewrite over the existing file still clean",
              target.read_text(encoding="utf-8") == '{"k": 2}\n'
              and list(target.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in target.parent.glob('*.tmp')]}")


def case_3_replace_retry_backoff() -> None:
    """_replace_with_retry backs off exponentially: seven attempts, sleeping
    0.1/0.2/0.4/0.8/1.6/3.2s between them, then the error surfaces. os.replace
    on the project module is swapped for a scripted shim and time.sleep for
    a recorder (the suite's attribute-swap convention), so the schedule is
    pinned without real sleeping: a replace failing twice then succeeding
    lands the file, a permanently busy destination re-raises after the full
    schedule, and a failing atomic write leaves the previous content intact
    with no tmp sibling."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src.tmp"
        dst = root / "dst.json"
        write_lf(src, "payload\n")
        real_replace = P.os.replace
        attempts = {"n": 0}
        sleeps: list[float] = []

        def flaky(src_, dst_):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise PermissionError("destination busy")
            return real_replace(src_, dst_)

        orig_os, orig_time = P.os, P.time
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=flaky)
        P.time = SimpleNamespace(sleep=sleeps.append)
        try:
            P._replace_with_retry(src, dst)
        finally:
            P.os, P.time = orig_os, orig_time
        check("3a retry: two busy replaces then success lands the file",
              dst.is_file() and dst.read_text(encoding="utf-8") == "payload\n",
              f"dst={dst.is_file()}")
        check("3b retry: three attempts, backoff 0.1s then 0.2s",
              attempts["n"] == 3 and sleeps == [0.1, 0.2],
              f"attempts={attempts['n']} sleeps={sleeps!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src.tmp"
        dst = root / "dst.json"
        write_lf(src, "payload\n")
        attempts = {"n": 0}
        sleeps: list[float] = []

        def busy_forever(src_, dst_):
            attempts["n"] += 1
            raise PermissionError("destination busy")

        orig_os, orig_time = P.os, P.time
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=busy_forever)
        P.time = SimpleNamespace(sleep=sleeps.append)
        raised: Exception | None = None
        try:
            try:
                P._replace_with_retry(src, dst)
            except PermissionError as exc:
                raised = exc
        finally:
            P.os, P.time = orig_os, orig_time
        check("3c retry: permanently busy destination re-raises",
              isinstance(raised, PermissionError), f"raised={raised!r}")
        check("3d retry: seven attempts before giving up",
              attempts["n"] == 7, f"attempts={attempts['n']}")
        check("3e retry: full backoff schedule 0.1s..3.2s",
              sleeps == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2], f"sleeps={sleeps!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / "novel_info.json"
        write_lf(target, '{"old": true}\n')

        def busy(src_, dst_):
            raise PermissionError("destination busy")

        orig_os, orig_time = P.os, P.time
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=busy)
        P.time = SimpleNamespace(sleep=lambda s: None)
        raised: Exception | None = None
        try:
            try:
                P.atomic_write_text(target, '{"new": true}\n', newline="\n")
            except PermissionError as exc:
                raised = exc
        finally:
            P.os, P.time = orig_os, orig_time
        check("3f atomic: failing replace propagates out of atomic_write_text",
              isinstance(raised, PermissionError), f"raised={raised!r}")
        check("3g atomic: previous content intact after the failed write",
              target.read_text(encoding="utf-8") == '{"old": true}\n',
              f"content={target.read_text(encoding='utf-8')!r}")
        check("3h atomic: no *.tmp sibling left by the failed write",
              list(root.glob("*.tmp")) == [],
              f"tmp={[p.name for p in root.glob('*.tmp')]}")


def case_4_write_chapter_atomic() -> None:
    """write_chapter routes through the atomic write: frontmatter + body
    round-trip with exactly one trailing LF and no CR bytes; a failing
    replace leaves the previous chapter bytes intact (and no tmp sibling);
    and writing into a not-yet-existing directory succeeds (parent creation
    inherited from atomic_write_text)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "translated" / "Chapter_0001.md"
        P.write_chapter(path, {"chapter_title": "第一章", "order": 0},
                        "第一行\n第二行\n\n\n")
        fm, body = P.read_chapter(path)
        check("4a write_chapter: frontmatter round-trips",
              fm.get("chapter_title") == "第一章" and fm.get("order") == 0,
              f"fm={fm!r}")
        check("4b write_chapter: body round-trips with one trailing newline",
              body == "第一行\n第二行", f"body={body!r}")
        raw = path.read_bytes()
        check("4c write_chapter: LF-only bytes, exactly one trailing newline",
              b"\r" not in raw and raw.endswith("第二行\n".encode("utf-8"))
              and not raw.endswith(b"\n\n"), f"raw={raw!r}")
        check("4d write_chapter: no *.tmp sibling",
              list(path.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in path.parent.glob('*.tmp')]}")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "translated" / "Chapter_0001.md"
        P.write_chapter(path, {"chapter_title": "旧"}, "旧正文\n")
        before = path.read_bytes()

        def busy(src_, dst_):
            raise PermissionError("destination busy")

        orig_os, orig_time = P.os, P.time
        P.os = SimpleNamespace(getpid=orig_os.getpid, replace=busy)
        P.time = SimpleNamespace(sleep=lambda s: None)
        raised: Exception | None = None
        try:
            try:
                P.write_chapter(path, {"chapter_title": "新"}, "新正文\n")
            except PermissionError as exc:
                raised = exc
        finally:
            P.os, P.time = orig_os, orig_time
        check("4e write_chapter: failing replace propagates",
              isinstance(raised, PermissionError), f"raised={raised!r}")
        check("4f write_chapter: previous chapter bytes intact",
              path.read_bytes() == before, f"raw={path.read_bytes()!r}")
        check("4g write_chapter: no tmp left by the failed write",
              list(path.parent.glob("*.tmp")) == [],
              f"tmp={[p.name for p in path.parent.glob('*.tmp')]}")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "deep" / "translated" / "Chapter_0001.md"
        P.write_chapter(path, {"chapter_title": "深"}, "正文\n")
        fm, body = P.read_chapter(path)
        check("4h write_chapter: missing parent directory created",
              fm.get("chapter_title") == "深" and body == "正文",
              f"fm={fm!r} body={body!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_discovery_ascii_digits()
    case_2_atomic_write_creates_parent()
    case_3_replace_retry_backoff()
    case_4_write_chapter_atomic()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
