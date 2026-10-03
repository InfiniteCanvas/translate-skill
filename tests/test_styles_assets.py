"""Tests for the shipped style-guide assets (assets/styles/*.md) against
the styles module's real loader contract.

The four-plus preset style guides are skill-shipped prompt material: at
init one is copied to the project's style.md and its body is injected as
{{style}} into every translate prompt, so a broken asset (empty body, a
lost description header, or a leaked template placeholder) would poison
every chapter quietly. The tests read through the module's REAL loader
with the REAL STYLES_DIR -- no directory swapping: parse_style_file must
yield a non-empty description (the `description: ...` + `---` header) and
a non-empty body; load_style against a project without styles/ overrides
must return exactly the parsed body; the body must carry no "{{key}}"
placeholder syntax (pipeline.fill does not re-scan substituted values, so
a literal {{...}} inside a style body would pass through into the prompt
verbatim); and an unknown name must raise styles.StyleError listing every
shipped style name.

The shipped names are enumerated from the directory (a style added later
is picked up automatically; nothing is hardcoded to fail).

Self-contained PASS/FAIL script (no pytest). lib/styles.py itself only
needs the stdlib, but lib/__init__ siblings pull the usual package -- run
via uv (deps declared inline below):

    uv run tests/test_styles_assets.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file
# (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import styles  # noqa: E402

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


def case_shipped_styles_load() -> None:
    """Every shipped style guide parses and loads through the real loader:
    non-empty description header, non-empty body, no template placeholder
    syntax anywhere, and load_style returns exactly the parsed body."""
    # The REAL shipped directory, never swapped: enumerate whatever the
    # skill ships today (a style added later is picked up automatically).
    shipped = sorted(styles.STYLES_DIR.glob("*.md"))
    check("1a shipped: assets/styles holds .md style guides",
          bool(shipped), f"styles_dir={styles.STYLES_DIR}")
    names = [p.stem for p in shipped]
    check("1b shipped: stems are unique (no resolution ambiguity)",
          len(names) == len(set(names)), f"names={names}")

    with tempfile.TemporaryDirectory() as td:
        # A project WITHOUT styles/ overrides, so every load resolves to
        # the shipped assets (load_style checks the project first).
        proj = Path(td)
        for path in shipped:
            name = path.stem
            text = path.read_text(encoding="utf-8-sig")
            description, body = styles.parse_style_file(text)
            check(f"2a-{name}: the description header is non-empty",
                  bool(description.strip()), f"description={description!r}")
            check(f"2b-{name}: the body is non-empty",
                  bool(body.strip()), f"len={len(body)}")
            check(f"2c-{name}: no template placeholder syntax ({{{{key}}}} "
                  "would pass through fill() into prompts)",
                  "{{" not in body and "{{" not in description,
                  f"description={description!r} body[:80]={body[:80]!r}")
            loaded = styles.load_style(proj, name)
            check(f"2d-{name}: load_style returns exactly the parsed body",
                  loaded == body,
                  f"loaded[:80]={loaded[:80]!r} body[:80]={body[:80]!r}")


def case_unknown_style_raises() -> None:
    """An unknown style name raises styles.StyleError naming the style and
    listing every shipped name (the loader's documented message shape)."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        raised: Exception | None = None
        try:
            styles.load_style(proj, "no-such-style")
        except styles.StyleError as exc:
            raised = exc
        names = [p.stem for p in sorted(styles.STYLES_DIR.glob("*.md"))]
        check("3a unknown: StyleError raised (never a bare KeyError/FileNotFound)",
              isinstance(raised, styles.StyleError), f"exc={raised!r}")
        check("3b unknown: the message names the requested style",
              raised is not None and "unknown style 'no-such-style'"
              in str(raised), f"message={str(raised)!r}")
        check("3c unknown: the message lists every shipped name",
              raised is not None and all(n in str(raised) for n in names),
              f"message={str(raised)!r} names={names}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_shipped_styles_load()
    case_unknown_style_raises()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
