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
- replace.replace_chapters (translated chapter reads): a BOM'd chapter's
  frontmatter stays out of the phrase match -- a YAML title containing the
  search phrase case-insensitively is neither counted nor rewritten (the
  full rewrite contract, including the BOM-stripping rewrite, is pinned in
  tests/test_replace.py case 8).
- migrations.common.sync_templates (drift comparison, BOTH sides): a dest
  whose only difference from the shipped copy is a BOM is not a user edit
  worth a prompt (pinned end-to-end in tests/test_migrate.py case 10).
- pipeline._load_template (project templates/*.md reads): the template text
  comes back with no leading \ufeff.
- profile.generate_profile (templates/style_profile.md read): the filled
  prompt sent to the profile provider carries no \ufeff.
- styles presets/overrides (list_styles / load_style): a BOM'd style file
  keeps its 'description:' header out of the body and its description
  visible in list_styles (the BOM used to hide line 1 from
  parse_style_file, leaking the header into the body).

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

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import translate
from lib import config, fix, glossary, pipeline, profile, project, styles, tn

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
    """Write text as plain UTF-8 with explicit LF newlines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_bom(path: Path, text: str) -> None:
    """Write text as UTF-8 WITH a leading BOM (\ufeff) -- the bytes a Windows
    editor saving 'UTF-8 with BOM' produces (EF BB BF before the text)."""
    write_lf(path, "\ufeff" + text)


def case_1_bom_json_project_files() -> None:
    """Every JSON project file loads cleanly with its data when BOM-prefixed."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

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

        manifest_in = [{"file": "CHAPTER_0001.md", "number": 1, "suffix": "",
                        "order": 0, "status": "translated",
                        "title": "Spirit Root"}]
        write_bom(root / "chapters.json",
                  json.dumps(manifest_in, ensure_ascii=False))
        m, out, exc = capture(project.load_manifest, root)
        check("1c manifest: BOM'd chapters.json loads with its entry",
              exc is None and m == manifest_in, f"exc={exc!r} m={m}")
        check("1d manifest: silent", out == "", f"out={out!r}")

        history_in = {"灵根": {"note": "Innate aptitude for cultivation.",
                               "last_order": 3, "times": 2}}
        write_bom(root / "tn_history.json",
                  json.dumps(history_in, ensure_ascii=False))
        h, out, exc = capture(tn.load_history, root)
        check("1e tn_history: BOM'd file loads with its term",
              exc is None and h == history_in, f"exc={exc!r} h={h}")
        check("1f tn_history: silent", out == "", f"out={out!r}")
        _res, _out_save, exc_save = capture(tn.save_history, root, history_in)
        raw = (root / "tn_history.json").read_bytes()
        check("1f2 tn_history: save_history pins LF + one trailing newline",
              exc_save is None
              and raw == json.dumps(history_in, ensure_ascii=False, indent=2)
              .encode("utf-8") + b"\n"
              and b"\r" not in raw,
              f"exc={exc_save!r} raw={raw!r}")

        notes_in = [{"line": 0, "term": "灵根",
                     "note": "Innate aptitude for cultivation.",
                     "anchor": "He tested his spirit root."}]
        write_bom(tn.notes_path(root, "CHAPTER_0001.md"),
                  json.dumps({"chapter": "CHAPTER_0001.md",
                              "updated_at": "2026-01-01T00:00:00+00:00",
                              "notes": notes_in}, ensure_ascii=False))
        n, out, exc = capture(tn.load_notes, root, "CHAPTER_0001.md")
        check("1g notes: BOM'd sidecar loads with its note",
              exc is None and n == notes_in, f"exc={exc!r} n={n}")
        check("1h notes: silent", out == "", f"out={out!r}")

        state_in = {"stage": "FAITH", "attempt": 1, "feedback": ["b1"],
                    "title": "T", "lines": None, "notes": None,
                    "rejected": None, "updated_at": "", "pipeline": 2}
        draft = root / "draft"
        write_bom(draft / "CHAPTER_0001.state.json",
                  json.dumps(state_in, ensure_ascii=False))
        s, out, exc = capture(pipeline.load_state, draft, "CHAPTER_0001.md")
        check("1i state: BOM'd draft state loads with its stage",
              exc is None and s == state_in
              and s is not None and s["stage"] == "FAITH",
              f"exc={exc!r} s={s}")
        check("1j state: silent", out == "", f"out={out!r}")

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
        chapter = root / "CHAPTER_0001.md"
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

        plain = root / "CHAPTER_0002.md"
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
        path = draft / "CHAPTER_0001.state.json"

        write_lf(path, "{not json")
        state, out, exc = capture(pipeline.load_state, draft, "CHAPTER_0001.md")
        check("4a state: JSON syntax error -> None, no exception",
              exc is None and state is None, f"exc={exc!r} state={state!r}")
        check("4b state: exactly one unreadable warn (JSONDecodeError)",
              out == "[warn] CHAPTER_0001.state.json unreadable "
                     "(JSONDecodeError) - restarting chapter state\n",
              f"out={out!r}")

        write_lf(path, "[]")
        state, out, exc = capture(pipeline.load_state, draft, "CHAPTER_0001.md")
        check("4c state: non-object document -> None, no exception",
              exc is None and state is None, f"exc={exc!r} state={state!r}")
        check("4d state: the warn names the reason in parentheses (list)",
              out == "[warn] CHAPTER_0001.state.json unreadable (list) "
                     "- restarting chapter state\n", f"out={out!r}")


def case_5_pipeline_template_bom() -> None:
    """pipeline._load_template reads the project's templates/ copy
    BOM-tolerantly. The BOM'd template MUST exist in the project dir -- a
    missing copy falls back to the skill's shipped assets, which would mask
    the fixture."""
    with tempfile.TemporaryDirectory() as td:
        templates = Path(td) / "templates"
        write_bom(templates / "translation.md", "Translate {{source_lang}}.\n")
        tpl, out, exc = capture(pipeline._load_template, templates, "translation.md")
        check("5a template: BOM'd project template loads without \\ufeff",
              exc is None and tpl == "Translate {{source_lang}}.\n"
              and "\ufeff" not in tpl,
              f"exc={exc!r} tpl={tpl!r}")
        check("5b template: silent", out == "", f"out={out!r}")


def case_6_profile_template_bom() -> None:
    """profile.generate_profile fills the project's BOM'd style_profile.md
    copy without leaking the BOM into the prompt. lib.client.chat is
    stubbed via profile.client (the call site does a module-attribute
    lookup) with orig/restore in try/finally -- the same harness as the
    glossary-review suites. log_llm is False in the fixture config, so the
    meta hook never writes a log."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_lf(root / "source" / "CHAPTER_0001.md",
                 "第一章 灵根\n\n林枫将手掌贴在水晶上。\n")
        write_lf(root / "config.json", json.dumps({
            "source_lang": "zh", "target_lang": "en",
            "log_llm": False, "providers": {},
        }, indent=2) + "\n")
        write_bom(root / "templates" / "style_profile.md",
                  "Analyze this {{source_lang}} novel for {{target_lang}}.\n\n"
                  "[Sample]\n{{sample_text}}\n")
        cfg, _out, _exc = capture(config.load_config, root)
        prompts: list[str] = []

        def fake_chat(provider_cfg, prompt, json_schema=None, temperature=None,
                      max_tokens=None, meta_hook=None):
            prompts.append(prompt)
            return json.dumps({"style_summary": "Plain, steady wuxia prose.",
                               "background": "A cultivation epic."},
                              ensure_ascii=False)

        orig = profile.client.chat
        profile.client.chat = fake_chat
        try:
            result, out, exc = capture(profile.generate_profile, root, cfg, 1, 1000)
        finally:
            profile.client.chat = orig
        check("6a profile: BOM'd style_profile.md fills with no \\ufeff in the prompt",
              exc is None and len(prompts) == 1
              and "\ufeff" not in prompts[0]
              and "林枫将手掌贴在水晶上" in prompts[0],
              f"exc={exc!r} prompts={prompts!r}")
        check("6b profile: stub response parsed back",
              result == {"style_summary": "Plain, steady wuxia prose.",
                         "background": "A cultivation epic."},
              f"result={result!r}")
        check("6c profile: silent", out == "", f"out={out!r}")


def case_7_styles_bom() -> None:
    """A BOM'd style preset keeps its 'description:' header out of the body:
    list_styles reports the description and load_style returns the clean
    body (the BOM used to hide line 1 from parse_style_file's startswith
    check, so list_styles showed an empty description and the header leaked
    into the body). styles.STYLES_DIR is swapped to a temp dir
    (module-attribute swap with orig/restore in try/finally); the fixture
    project has no styles/ overrides of its own."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        presets = Path(td) / "presets"
        write_bom(presets / "literary.md",
                  "description: literary prose\n"
                  "---\n"
                  "Prefer formal register and longer sentences.\n")
        orig = styles.STYLES_DIR
        styles.STYLES_DIR = presets
        try:
            listed, out, exc = capture(styles.list_styles, root)
            check("7a styles: BOM'd preset lists its description",
                  exc is None and listed == [("literary", "literary prose")],
                  f"exc={exc!r} listed={listed}")
            body, out, exc = capture(styles.load_style, root, "literary")
            check("7b styles: BOM'd preset body has no leaked header",
                  exc is None
                  and body == "Prefer formal register and longer sentences."
                  and "description:" not in body and "\ufeff" not in body,
                  f"exc={exc!r} body={body!r}")
            check("7c styles: silent", out == "", f"out={out!r}")
        finally:
            styles.STYLES_DIR = orig


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_bom_json_project_files()
    case_2_read_chapter_bom()
    case_3_parse_report_bom()
    case_4_load_state_loud_discard()
    case_5_pipeline_template_bom()
    case_6_profile_template_bom()
    case_7_styles_bom()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
