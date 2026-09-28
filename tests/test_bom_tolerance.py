"""Tests for BOM-tolerant reads of every hand-editable project file, plus
load_state's loud-discard contract for genuinely corrupt content.

Windows editors save "UTF-8 with BOM" by default, so every file the skill
re-reads must open with encoding="utf-8-sig": a leading \ufeff otherwise
breaks JSON parsing outright (json.JSONDecodeError) or, for line-anchored
markdown, hides line 1 (read_chapter's opening '---', fix.parse_report's
'^- Command:' and '^### [N]' regexes). Covers, per file:

- glossary.load (glossary.json): the entry survives intact.
- project.load_manifest (chapters.json): the entry list survives intact.
- tn.load_history (tn_history.json): the term map survives intact.
- tn.load_notes (notes/<stem>.json sidecar): the notes list survives intact.
- pipeline.load_state (draft/<stem>.state.json): the state dict survives
  with its stage.
- config.load_config (config.json): the USER value wins over DEFAULTS while
  untouched keys still deep-merge from DEFAULTS and providers normalize.
- translate._load_novel_info (novel_info.json, the strict helper profile /
  build-epub share): the document survives with no CliError.
- project.read_chapter: a BOM'd chapter WITH frontmatter keeps both its
  frontmatter dict and its body (the BOM used to hide the opening '---'
  line, silently degrading the chapter to ({}, whole file)); a BOM'd
  frontmatter-less chapter keeps the BOM out of the body text.
- fix.parse_report: a BOM'd report still yields its commands in BOTH parser
  modes -- the explicit '- Command:' bullet on line 1 and the legacy
  synthesis '### [N]' heading on line 1 are exactly the ^-anchored patterns
  a file-start BOM used to hide.

Every BOM'd load must also be SILENT: the utf-8-sig read may not fall into
the malformed-file discard path (which would print a [warn] and return the
empty default -- the "silent-empty" failure mode this suite guards against).
stdout is captured with contextlib.redirect_stdout around every call.

The final case is the corrupt-content companion of the load_state BOM case
(the tn.load_history / tn.load_notes counterparts live in
test_tn_sidecar.py): genuinely corrupt content -- a JSON syntax error, or a
non-object document where a dict is required -- keeps the lenient default
(None) AND prints exactly one '[warn] <file> unreadable (<reason>) -
restarting chapter state' line with the reason in parentheses.

All fixtures are written with a forced leading \ufeff (or, for the corrupt
case, plain text) and explicit LF newlines inside tempfile.TemporaryDirectory()
sandboxes; the repo's own files are never touched.

Self-contained PASS/FAIL script (no pytest). The lib modules and
scripts/translate.py import pyyaml, requests, ebooklib and pillow, so run
via uv (deps declared inline below):

    uv run tests/test_bom_tolerance.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

# scripts/ (and therefore lib/ and translate.py) lives at
# novel-translator/scripts relative to this file (CWD-independent).
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import translate  # noqa: E402
from lib import config, fix, glossary, pipeline, project, tn  # noqa: E402

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
    """Write text as plain UTF-8 with explicit LF newlines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_bom(path: Path, text: str) -> None:
    """Write text as UTF-8 WITH a leading BOM (\ufeff) -- the bytes a Windows
    editor saving 'UTF-8 with BOM' produces (EF BB BF before the text)."""
    write_lf(path, "\ufeff" + text)


# ---------------------------------------------------------------------- cases


def case_1_bom_json_project_files() -> None:
    """Every JSON project file loads cleanly with its data when BOM-prefixed."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # glossary.json
        glossary_in = {"terms": [
            {"source": "灵根", "variants": ["靈根"],
             "translation": "spirit root"},
        ]}
        write_bom(root / "glossary.json",
                  json.dumps(glossary_in, ensure_ascii=False))
        g, out, exc = capture(glossary.load, root)
        check("1a glossary: BOM'd glossary.json loads with its entry",
              exc is None and g == glossary_in, f"exc={exc!r} g={g}")
        check("1b glossary: silent (no discard warning)",
              out == "", f"out={out!r}")

        # chapters.json
        manifest_in = [{"file": "Chapter_0001.md", "number": 1, "suffix": "",
                        "order": 0, "status": "translated",
                        "title": "Spirit Root"}]
        write_bom(root / "chapters.json",
                  json.dumps(manifest_in, ensure_ascii=False))
        m, out, exc = capture(project.load_manifest, root)
        check("1c manifest: BOM'd chapters.json loads with its entry",
              exc is None and m == manifest_in, f"exc={exc!r} m={m}")
        check("1d manifest: silent", out == "", f"out={out!r}")

        # tn_history.json
        history_in = {"灵根": {"note": "Innate aptitude for cultivation.",
                               "last_order": 3, "times": 2}}
        write_bom(root / "tn_history.json",
                  json.dumps(history_in, ensure_ascii=False))
        h, out, exc = capture(tn.load_history, root)
        check("1e tn_history: BOM'd file loads with its term",
              exc is None and h == history_in, f"exc={exc!r} h={h}")
        check("1f tn_history: silent", out == "", f"out={out!r}")

        # notes sidecar
        notes_in = [{"line": 0, "term": "灵根",
                     "note": "Innate aptitude for cultivation.",
                     "anchor": "He tested his spirit root."}]
        write_bom(tn.notes_path(root, "Chapter_0001.md"),
                  json.dumps({"chapter": "Chapter_0001.md",
                              "updated_at": "2026-01-01T00:00:00+00:00",
                              "notes": notes_in}, ensure_ascii=False))
        n, out, exc = capture(tn.load_notes, root, "Chapter_0001.md")
        check("1g notes: BOM'd sidecar loads with its note",
              exc is None and n == notes_in, f"exc={exc!r} n={n}")
        check("1h notes: silent", out == "", f"out={out!r}")

        # draft/<stem>.state.json
        state_in = {"stage": "FAITH", "attempt": 1, "feedback": ["b1"],
                    "title": "T", "lines": None, "notes": None,
                    "rejected": None, "updated_at": "", "pipeline": 2}
        draft = root / "draft"
        write_bom(draft / "Chapter_0001.state.json",
                  json.dumps(state_in, ensure_ascii=False))
        s, out, exc = capture(pipeline.load_state, draft, "Chapter_0001.md")
        check("1i state: BOM'd draft state loads with its stage",
              exc is None and s == state_in
              and s is not None and s["stage"] == "FAITH",
              f"exc={exc!r} s={s}")
        check("1j state: silent", out == "", f"out={out!r}")

        # config.json: the user override must win over DEFAULTS while the
        # deep-merge still fills untouched keys -- a BOM-broken read would
        # raise instead of quietly returning pure DEFAULTS.
        write_bom(root / "config.json",
                  json.dumps({"max_attempts": 9, "providers": {}}, indent=2))
        cfg, out, exc = capture(config.load_config, root)
        check("1k config: BOM'd config.json loads; user value wins, "
              "DEFAULTS still fill untouched keys",
              exc is None and cfg.get("max_attempts") == 9
              and config.DEFAULTS["max_attempts"] != 9
              and cfg.get("review_batch_size")
              == config.DEFAULTS["review_batch_size"],
              f"exc={exc!r} max_attempts={cfg.get('max_attempts') if cfg else None!r}")
        check("1l config: providers normalized for every job",
              isinstance(cfg, dict)
              and set(cfg.get("providers", {})) >= set(config.PROVIDER_JOBS),
              f"providers={sorted(cfg.get('providers', {})) if cfg else None}")
        check("1m config: silent", out == "", f"out={out!r}")

        # novel_info.json via translate's strict helper
        info_in = {"title": "凡人修仙传", "author": "忘语",
                   "background": "A mortal's cultivation epic."}
        write_bom(root / "novel_info.json",
                  json.dumps(info_in, ensure_ascii=False))
        info, out, exc = capture(translate._load_novel_info, root)
        check("1n novel_info: BOM'd file loads via translate._load_novel_info",
              exc is None and info == info_in, f"exc={exc!r} info={info}")
        check("1o novel_info: silent (no CliError, no warning)",
              out == "", f"out={out!r}")


def case_2_read_chapter_bom() -> None:
    """A BOM'd chapter keeps its frontmatter (the BOM used to hide the
    opening '---' line) and its body; a BOM'd frontmatter-less chapter
    keeps the BOM out of the body text."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        chapter = root / "Chapter_0001.md"
        write_bom(chapter,
                  "---\n"
                  "chapter_title: 第二章 灵根\n"
                  "order: 3\n"
                  "---\n"
                  "\n"
                  "正文第一行。\n"
                  "正文第二行。\n")
        fm, body = project.read_chapter(chapter)
        check("2a read_chapter: BOM'd chapter keeps its frontmatter dict",
              fm == {"chapter_title": "第二章 灵根", "order": 3}, f"fm={fm}")
        check("2b read_chapter: body intact after the BOM'd frontmatter",
              body == "正文第一行。\n正文第二行。", f"body={body!r}")

        plain = root / "Chapter_0002.md"
        write_bom(plain, "只有正文。\n第二行。\n")
        fm, body = project.read_chapter(plain)
        check("2c read_chapter: BOM'd frontmatter-less chapter -> ({}, body), "
              "BOM stays out of the body",
              fm == {} and body == "只有正文。\n第二行。\n"
              and "\ufeff" not in body, f"fm={fm} body={body!r}")


def case_3_parse_report_bom() -> None:
    """A BOM'd review report still yields its commands in both parser
    modes; every ^-anchored pattern involved sits on line 1, exactly where
    a file-start BOM lands."""
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "review-report.md"

        # explicit mode: the - Command: bullet ON LINE 1
        write_bom(report, "- Command: glossary replace --source '灵根' "
                          "--translation 'spiritual root'\n")
        result, out, exc = capture(fix.parse_report, report)
        specs, count = result if exc is None else ([], -1)
        check("3a report: BOM'd report yields its line-1 Command bullet",
              exc is None and len(specs) == 1
              and specs[0].argv == ["glossary", "replace", "--source", "灵根",
                                    "--translation", "spiritual root"]
              and specs[0].line_no == 1,
              f"exc={exc!r} specs={[(s.argv, s.line_no) for s in specs]}")

        # legacy synthesis mode: the '### [N]' heading ON LINE 1
        write_bom(report,
                  "### [1] warn / mundane / 灵根\n"
                  "\n"
                  "- Reason: ordinary word, not a novel-specific term\n"
                  "- Tier: model\n")
        result, out, exc = capture(fix.parse_report, report)
        specs, count = result if exc is None else ([], -1)
        check("3b report: BOM'd line-1 heading still synthesizes (legacy mode)",
              exc is None and count == 1 and len(specs) == 1
              and specs[0].argv == ["glossary", "retire", "--source", "灵根"]
              and specs[0].line_no == 0,
              f"exc={exc!r} count={count} "
              f"specs={[(s.argv, s.line_no) for s in specs]}")


def case_4_load_state_loud_discard() -> None:
    """Genuinely corrupt draft state (not a BOM): the lenient default holds
    AND the discard is loud -- exactly one '[warn] ... unreadable (<reason>)'
    line with the reason in parentheses. The tn.load_history / tn.load_notes
    counterparts live in test_tn_sidecar.py."""
    with tempfile.TemporaryDirectory() as td:
        draft = Path(td)
        path = draft / "Chapter_0001.state.json"

        write_lf(path, "{not json")
        state, out, exc = capture(pipeline.load_state, draft, "Chapter_0001.md")
        check("4a state: JSON syntax error -> None, no exception",
              exc is None and state is None, f"exc={exc!r} state={state!r}")
        check("4b state: exactly one unreadable warn (JSONDecodeError)",
              out == "[warn] Chapter_0001.state.json unreadable "
                     "(JSONDecodeError) - restarting chapter state\n",
              f"out={out!r}")

        write_lf(path, "[]")
        state, out, exc = capture(pipeline.load_state, draft, "Chapter_0001.md")
        check("4c state: non-object document -> None, no exception",
              exc is None and state is None, f"exc={exc!r} state={state!r}")
        check("4d state: the warn names the reason in parentheses (list)",
              out == "[warn] Chapter_0001.state.json unreadable (list) "
                     "- restarting chapter state\n", f"out={out!r}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_bom_json_project_files()
    case_2_read_chapter_bom()
    case_3_parse_report_bom()
    case_4_load_state_loud_discard()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
