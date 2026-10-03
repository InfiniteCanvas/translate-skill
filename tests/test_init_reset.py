"""Tests for cmd_init's model-grown state handling (reset only under --force).

H3 contract: init's unconditional wipe of glossary.json, tn_history.json,
and story_state.json destroyed a recovery-init's reason to exist -- a
deleted config.json re-initialized WITHOUT --force must leave the
model-grown state byte-identical and say so
("[init] preserving existing ... (pass --force to reset)"), skipping both
the reset and the "[init] initialized glossary.json and tn_history.json"
line. Only --force resets, announcing the loss with the best-effort term
count. A fresh directory (no state files) inits silently either way.

Case 1 drives the recovery-init without --force (config.json deleted,
all three state files present); case 2 re-runs the same fixture with
--force and pins the reset; case 3 pins the fresh-directory path with
and without --force. state files are compared by bytes, so a partial
rewrite can never pass.

cmd_init is driven in-process with an argparse.Namespace (test_sync's
pattern); --style classic and a blank source_url keep the whole run
offline (no profile LLM call, no cover scrape). vcs still commits inside
the sandbox when git is on PATH.

Self-contained PASS/FAIL script (no pytest). The lib modules and
scripts/translate.py import pyyaml, requests, ebooklib and pillow, so run
via uv (deps declared inline below):

    uv run tests/test_init_reset.py
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
import sys
import tempfile
from pathlib import Path

# scripts/ (and therefore lib/) lives at novel-translator/scripts relative
# to this file (CWD-independent); translate.py puts it on sys.path itself.
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from translate import cmd_init  # noqa: E402

PASSED = 0
FAILED: list[str] = []

GLOSSARY_2 = json.dumps(
    {"terms": [
        {"source": "灵根", "translation": "spirit root", "category": "technique"},
        {"source": "青云宗", "translation": "Azure Cloud Sect", "category": "place"},
    ]},
    ensure_ascii=False, indent=2,
) + "\n"
TN_HISTORY = json.dumps(
    {"灵根": {"first_order": 1, "last_order": 2, "gloss": "spirit root"}},
    ensure_ascii=False, indent=2,
) + "\n"
STORY_STATE = json.dumps(
    {"last_recap_chapter": 2, "recap": "林凡觉醒灵根。"},
    ensure_ascii=False, indent=2,
) + "\n"

PRESERVE = ("[init] preserving existing glossary.json, tn_history.json, and"
            " story_state.json (pass --force to reset)")
RESETTING = ("[init] --force: resetting glossary.json (2 term(s)),"
             " tn_history.json, and story_state.json")
INITIALIZED = "[init] initialized glossary.json and tn_history.json"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def init_args(force: bool) -> argparse.Namespace:
    """The full flag surface cmd_init reads; --style classic and an empty
    source_url keep the run offline (preset style, placeholder cover)."""
    return argparse.Namespace(
        force=force,
        style="classic",
        title="测试小说",
        author="测试作者",
        source_url="",
        source_lang="zh",
        target_lang="en",
        tags="仙侠",
        api_base="https://api.example.com/v1",
        cover_url=None,
        background=None,
        skip_profile=False,
    )


def write_source(root: Path, name: str, text: str) -> None:
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / name).write_text(text, encoding="utf-8", newline="\n")


def make_state_fixture(root: Path) -> None:
    """A half-dead project: source chapters and the three model-grown
    state files survive; config.json is gone."""
    write_source(root, "Chapter_001.md", "第一章 灵根初现\n\n林凡睁开双眼。\n")
    write_source(root, "Chapter_002.md", "第二章 青云宗\n\n他踏上修行路。\n")
    (root / "glossary.json").write_text(GLOSSARY_2, encoding="utf-8", newline="\n")
    (root / "tn_history.json").write_text(TN_HISTORY, encoding="utf-8", newline="\n")
    (root / "story_state.json").write_text(STORY_STATE, encoding="utf-8", newline="\n")


def run_init(root: Path, force: bool) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_init(init_args(force), root)
    return rc, buf.getvalue()


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Byte contents of the three state files (None when absent)."""
    return {
        "glossary": (root / "glossary.json").read_bytes()
        if (root / "glossary.json").is_file() else None,
        "tn_history": (root / "tn_history.json").read_bytes()
        if (root / "tn_history.json").is_file() else None,
        "story_state": (root / "story_state.json").read_bytes()
        if (root / "story_state.json").is_file() else None,
    }


# ---------------------------------------------------------------------- cases


def case_1_recovery_init_preserves_state() -> None:
    """Deleted config.json + init WITHOUT --force: all three model-grown
    state files survive byte-identical, the preserve line prints, and no
    reset/initialized line does."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_state_fixture(root)
        before = snapshot(root)

        rc, out = run_init(root, force=False)

        check("1a recovery init: exits 0", rc == 0, f"rc={rc}")
        check("1b recovery init: preserve line printed", PRESERVE in out,
              f"out={out!r}")
        check("1c recovery init: no resetting line", "--force: resetting" not in out)
        check("1d recovery init: no initialized-glossary line", INITIALIZED not in out)
        after = snapshot(root)
        check("1e recovery init: glossary.json byte-identical",
              after["glossary"] == before["glossary"])
        check("1f recovery init: tn_history.json byte-identical",
              after["tn_history"] == before["tn_history"])
        check("1g recovery init: story_state.json byte-identical",
              after["story_state"] == before["story_state"])
        check("1h recovery init: story_state.json still present",
              (root / "story_state.json").is_file())
        check("1i recovery init: config.json restored",
              (root / "config.json").is_file())


def case_2_force_resets_state() -> None:
    """The same fixture WITH --force: the three files reset (glossary
    emptied, tn_history '{}', story_state deleted), announced with the
    term count."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_state_fixture(root)

        rc, out = run_init(root, force=True)

        check("2a force init: exits 0", rc == 0, f"rc={rc}")
        check("2b force init: resetting line with the term count", RESETTING in out,
              f"out={out!r}")
        check("2c force init: initialized line printed", INITIALIZED in out)
        check("2d force init: glossary.json emptied",
              json.loads((root / "glossary.json").read_text(encoding="utf-8-sig"))
              .get("terms") == [])
        check("2e force init: tn_history.json is '{}'",
              (root / "tn_history.json").read_bytes() == b"{}\n")
        check("2f force init: story_state.json deleted",
              not (root / "story_state.json").exists())
        check("2g force init: no preserve line", PRESERVE not in out)


def case_3_fresh_init_is_silent() -> None:
    """A fresh directory (no state files) inits normally with or without
    --force: no preserve line, no resetting announce, glossary created
    empty."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_source(root, "Chapter_001.md", "第一章\n\n林凡睁开双眼。\n")

        rc, out = run_init(root, force=False)

        check("3a fresh init: exits 0", rc == 0, f"rc={rc}")
        check("3b fresh init: no preserve line", PRESERVE not in out)
        check("3c fresh init: no resetting line", "--force: resetting" not in out)
        check("3d fresh init: glossary.json created empty",
              json.loads((root / "glossary.json").read_text(encoding="utf-8-sig"))
              .get("terms") == [])
        check("3e fresh init: tn_history.json is '{}'",
              (root / "tn_history.json").read_bytes() == b"{}\n")
        check("3f fresh init: initialized line printed", INITIALIZED in out)

        rc2 = None
        with tempfile.TemporaryDirectory() as td2:
            fresh2 = Path(td2)
            write_source(fresh2, "Chapter_001.md", "第一章\n\n林凡睁开双眼。\n")
            rc2, out2 = run_init(fresh2, force=True)
            check("3g fresh --force init: no resetting line either",
                  rc2 == 0 and "--force: resetting" not in out2, f"rc={rc2}")
            check("3h fresh --force init: glossary.json created empty",
                  json.loads((fresh2 / "glossary.json")
                             .read_text(encoding="utf-8-sig")).get("terms") == [])


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_recovery_init_preserves_state()
    case_2_force_resets_state()
    case_3_fresh_init_is_silent()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
