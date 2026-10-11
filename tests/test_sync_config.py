"""Tests for the config.local.json overlay: read-time layering and the init pass.

Covers the skill-level overlay that every command resolves through:

  - load_local_config: absent file -> None (a silent no-op, never an error);
    malformed JSON / non-object body -> ValueError; a `version` key is
    rejected (the stamp belongs to init/migrate alone).
  - merge semantics: the overlay WINS on the keys it names and the project's
    other keys survive -- languages, thresholds, and the version stamp.
    Lists (providers.<job> arrays) are replaced wholesale, never spliced.
  - cmd_sync_config: no overlay -> exit 0 no-op; uninitialized project ->
    CliError (exit 2); inline api_key warns; a success is REPORT-ONLY (it
    writes nothing at all into the project).
  - cmd_init: the overlay is read, never copied; the merged values reach the
    in-memory cfg the seed/profile passes below it read.
  - a CORRUPT project config.json (a non-object body, or a `providers` that
    is not an object) is refused with a clean ValueError -> exit 2, never the
    AttributeError/TypeError traceback that main() does not catch; and every
    legitimate shape (plain, empty, providers-bearing, BOM'd) still resolves.
  - the overlay's endpoint requirement is enforced everywhere: a config that
    resolves no base_url is refused by load_config, and cmd_init validates it
    BEFORE writing anything, so a failed init leaves the project untouched.
  - `_load_config_lenient` is lenient about ONE thing only -- an absent
    config.json, which the file-only upkeep commands tolerate. A malformed
    overlay or an unresolvable endpoint fails those commands too, with
    `[FAIL]` and exit 2, instead of being swallowed.

All fixtures live in tempfile.TemporaryDirectory() sandboxes per case. The
overlay path is derived from translate.SKILL_ROOT, so a fake skill root is
used via monkeypatching of that module global (resolved per call, never
captured at import).
Self-contained PASS/FAIL script (no pytest). The lib modules and
scripts/translate.py import pyyaml, requests, ebooklib and pillow, so run
via uv (deps declared inline below):

    uv run tests/test_sync_config.py
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
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config
import translate as translate_mod
from translate import CliError

PASSED = 0
FAILED: list[str] = []

REAL_SKILL_ROOT = translate_mod.SKILL_ROOT


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def write_overlay(skill_root: Path, payload: object) -> None:
    """Write config.local.json into a fake skill root (raw text so malformed
    JSON is expressible)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (skill_root / config.LOCAL_CONFIG_NAME).write_text(text, encoding="utf-8")


def write_config(project_dir: Path, payload: dict) -> Path:
    path = project_dir / "config.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def read_config(project_dir: Path) -> dict:
    return json.loads((project_dir / "config.json").read_text(encoding="utf-8"))


def _run_main_capturing(argv: list[str]) -> tuple[str, str, int]:
    """translate.main() with both streams captured; returns (stdout, stderr,
    code). main() RETURNS the exit code -- sys.exit only fires under __main__ --
    so the return value is what the CLI contract is asserted on."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = translate_mod.main(argv)
    return out.getvalue(), err.getvalue(), code


def case_1_load_absent_and_malformed() -> None:
    """Absent -> None (never an error); bad shapes -> ValueError."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check("1a absent: no overlay file returns None",
              config.load_local_config(root) is None,
              "expected None for an empty skill root")

        write_overlay(root, "{ not json")
        try:
            config.load_local_config(root)
            check("1b malformed: invalid JSON raises ValueError", False,
                  "returned without raising")
        except ValueError as exc:
            check("1b malformed: invalid JSON raises ValueError",
                  "not valid JSON" in str(exc), f"message={exc}")

        write_overlay(root, '["a", "list"]')
        try:
            config.load_local_config(root)
            check("1c non-object: array body raises ValueError", False,
                  "returned without raising")
        except ValueError as exc:
            check("1c non-object: array body raises ValueError",
                  "must contain a JSON object" in str(exc), f"message={exc}")

        write_overlay(root, {"version": 99, "source_lang": "zh"})
        try:
            config.load_local_config(root)
            check("1d version: overlay version key is rejected", False,
                  "returned without raising")
        except ValueError as exc:
            check("1d version: overlay version key is rejected",
                  "version" in str(exc), f"message={exc}")

        write_overlay(root, {"source_lang": "ja"})
        check("1e valid: a well-formed overlay loads",
              config.load_local_config(root) == {"source_lang": "ja"},
              f"got={config.load_local_config(root)}")

        (root / config.LOCAL_CONFIG_NAME).write_text(
            json.dumps({"a": 1}), encoding="utf-8-sig")
        check("1f BOM: overlay saved with a BOM loads",
              config.load_local_config(root) == {"a": 1},
              f"got={config.load_local_config(root)}")


def case_2_merge_semantics() -> None:
    """Overlay wins on the keys it names; the project keeps everything else."""
    project_cfg = {
        "source_lang": "zh",
        "target_lang": "en",
        "review_batch_size": 40,
        "version": 8,
        "providers": {
            "translator": [
                {"base_url": "http://project:8888/v1", "model": "project-model"},
            ],
        },
    }
    overlay = {
        "review_batch_size": 7,
        "providers": {"translator": [
            {"base_url": "http://local:9999/v1", "model": "local-model"},
        ]},
    }
    merged = config.merge_overlay(project_cfg, overlay)

    check("2a override: overlay scalar wins", merged["review_batch_size"] == 7,
          f"got={merged['review_batch_size']}")
    check("2b preserve: languages survive the merge",
          merged["source_lang"] == "zh" and merged["target_lang"] == "en",
          f"langs={merged['source_lang']}/{merged['target_lang']}")
    check("2c preserve: the project version stamp survives",
          merged["version"] == 8, f"version={merged['version']}")
    check("2d replace: providers array replaced wholesale (no splice)",
          merged["providers"]["translator"]
          == [{"base_url": "http://local:9999/v1", "model": "local-model"}],
          f"got={merged['providers']['translator']}")
    check("2e immutability: merge_overlay does not mutate its inputs",
          project_cfg["review_batch_size"] == 40
          and project_cfg["providers"]["translator"][0]["model"] == "project-model",
          f"base was mutated: {project_cfg}")

    partial = config.merge_overlay(project_cfg, {
        "providers": {"translator": {"model": "only-model"}}})
    check("2f partial: dict block merges into every project block",
          partial["providers"]["translator"]
          == [{"base_url": "http://project:8888/v1", "model": "only-model"}],
          f"got={partial['providers']['translator']}")

    fanout = {"providers": {"translator": [
        {"base_url": "http://a/v1", "model": "m-a"},
        {"base_url": "http://b/v1", "model": "m-b"},
    ]}}
    partial_fanout = config.merge_overlay(fanout, {
        "providers": {"translator": {"model": "shared"}}})
    check("2g partial fan-out: every block updated, none lost",
          partial_fanout["providers"]["translator"]
          == [{"base_url": "http://a/v1", "model": "shared"},
              {"base_url": "http://b/v1", "model": "shared"}],
          f"got={partial_fanout['providers']['translator']}")


def case_4_inline_api_key_detection() -> None:
    """Inline keys are located at any depth, including inside arrays."""
    overlay = {
        "providers": {
            "translator": [
                {"base_url": "http://x/v1", "api_key": "sk-one"},
                {"base_url": "http://y/v1", "api_key_env": "Y_KEY"},
            ],
            "reviewer": {"api_key": "sk-two"},
        },
        "top_secret_not_a_key": "ignore-me",
    }
    found = config.inline_api_keys(overlay)
    check("4a inline key inside a providers array is found",
          "providers.translator[0].api_key" in found, f"found={found}")
    check("4b inline key in a dict-shaped block is found",
          "providers.reviewer.api_key" in found, f"found={found}")
    check("4c api_key_env is NOT flagged (it is the safe form)",
          not any("api_key_env" in key for key in found), f"found={found}")
    check("4d unrelated key is not flagged",
          not any("top_secret" in key for key in found), f"found={found}")
    check("4e empty overlay finds nothing",
          config.inline_api_keys({}) == [], "expected no findings")


def case_5_cmd_sync_config() -> None:
    """cmd_sync_config: no overlay -> 0, uninitialized -> 2, and a success
    that REPORTS the composed layers without writing anything."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        skill.mkdir()
        project = root / "proj"
        project.mkdir()

        args = argparse.Namespace(project=str(project), dry_run=False,
                                 force=False)

        translate_mod.SKILL_ROOT = skill
        try:
            rc = translate_mod.cmd_sync_config(args, project)
            check("5a no overlay: exit 0 no-op", rc == 0, f"rc={rc}")

            write_overlay(skill, {"source_lang": "ja"})
            try:
                translate_mod.cmd_sync_config(args, project)
                check("5b uninitialized: missing config.json raises CliError",
                      False, "returned without raising")
            except CliError as exc:
                check("5b uninitialized: missing config.json raises CliError",
                      "init" in str(exc), f"message={exc}")

            write_overlay(skill, "{ broken")
            try:
                translate_mod.cmd_sync_config(args, project)
                check("5c malformed overlay: raises CliError", False,
                      "returned without raising")
            except CliError as exc:
                check("5c malformed overlay: raises CliError",
                      "config" in str(exc), f"message={exc}")

            write_config(project, {
                "source_lang": "zh",
                "target_lang": "en",
                "version": 8,
                "providers": {"translator": [
                    {"base_url": "http://project:8888/v1", "model": None}]},
            })
            write_overlay(skill, {
                "providers": {"translator": [
                    {"base_url": "http://local:9999/v1", "api_key": "sk-secret"}]},
            })
            with _Capture() as cap:
                rc = translate_mod.cmd_sync_config(args, project)
            on_disk = read_config(project)
            out = cap.out
            check("5d success: exit 0", rc == 0, f"rc={rc}")
            check("5e report-only: the overlay is NOT written into config.json",
                  on_disk["providers"]["translator"][0]["base_url"]
                  == "http://project:8888/v1",
                  f"got={on_disk['providers']['translator'][0]}")
            check("5f report-only: the inline api_key is NOT copied into the "
                  "project file",
                  "api_key" not in on_disk["providers"]["translator"][0],
                  f"block={on_disk['providers']['translator'][0]}")
            check("5g report-only: the report names each resolved endpoint",
                  out.count("http://project:8888/v1") == 7, f"out={out!r}")
            check("5h report-only: the report names the layers it composed",
                  "config.local.json <- config.json" in out, f"out={out!r}")
            check("5i report-only: the project's own keys are untouched",
                  on_disk["source_lang"] == "zh"
                  and on_disk["target_lang"] == "en"
                  and on_disk["version"] == 8,
                  f"got={ {k: on_disk.get(k) for k in ('source_lang', 'target_lang', 'version')} }")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT


def case_6_init_resolves_overlay_without_copying() -> None:
    """cmd_init writes ONLY authored keys, and the overlay still reaches the
    in-memory cfg the seed/profile passes below it read -- through the layers,
    not through a copy in config.json."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        (skill / "assets" / "templates").mkdir(parents=True)
        (skill / "assets" / "templates" / "translation.md").write_text(
            "placeholder\n", encoding="utf-8")
        write_overlay(skill, {
            "seed_min_count": 11,
            "providers": {"translator": [
                {"base_url": "http://local:9999/v1", "model": "local-model"}]},
        })

        project = root / "proj"
        source = project / "source"
        source.mkdir(parents=True)
        (source / "CHAPTER_0001.md").write_text(
            "第一章\nbody\n", encoding="utf-8", newline="\n")

        args = argparse.Namespace(
            project=str(project), title="测试小说", author="测试作者",
            source_url="", source_lang="zh", target_lang="en", tags="",
            api_base="http://from-flag:1/v1", cover_url=None, style="classic",
            background=None, skip_profile=True, force=False,
        )

        translate_mod.SKILL_ROOT = skill
        translate_mod.TEMPLATES_SRC_DIR = skill / "assets" / "templates"
        try:
            rc = translate_mod.cmd_init(args, project)
            on_disk = read_config(project)
            loaded = config.load_config(project, skill)

            check("6a init: exit 0", rc == 0, f"rc={rc}")
            check("6b init: the overlay is NOT copied into config.json -- the "
                  "authored --api-base value stays",
                  on_disk["providers"]["translator"][0]["base_url"]
                  == "http://from-flag:1/v1",
                  f"got={on_disk['providers']['translator'][0]['base_url']}")
            check("6c init: config.json holds no DEFAULTS keys at all",
                  not (set(config.DEFAULTS) & set(on_disk)),
                  f"leaked={sorted(set(config.DEFAULTS) & set(on_disk))}")
            check("6d init: the overlay's scalar resolves through the layers",
                  loaded["seed_min_count"] == 11,
                  f"seed_min_count={loaded['seed_min_count']}")
            check("6e init: the project's own endpoint outranks the overlay "
                  "(layer 2 over layer 1)",
                  config.provider(loaded, "translator")["base_url"]
                  == "http://from-flag:1/v1",
                  f"resolved={config.provider(loaded, 'translator')!r}")
            check("6f init: init's own keys survive the layering",
                  on_disk["source_lang"] == "zh"
                  and on_disk["target_lang"] == "en",
                  f"langs={on_disk['source_lang']}/{on_disk['target_lang']}")
            check("6g init: version stamped by init, not by the overlay",
                  isinstance(on_disk.get("version"), int)
                  and on_disk["version"] > 0,
                  f"version={on_disk.get('version')!r}")
            check("6h init: manifest still built after the overlay is read",
                  (project / "chapters.json").is_file(),
                  "chapters.json missing")

            bare = root / "bare"
            (bare / "source").mkdir(parents=True)
            (bare / "source" / "CHAPTER_0001.md").write_text(
                "第一章\nbody\n", encoding="utf-8", newline="\n")
            args.api_base = ""
            rc = translate_mod.cmd_init(args, bare)
            bare_cfg = config.load_config(bare, skill)
            check("6i init with no --api-base: config.json authors no "
                  "providers section at all",
                  "providers" not in read_config(bare),
                  f"keys={sorted(read_config(bare))}")
            check("6j init with no --api-base: the overlay's endpoint resolves "
                  "for every job",
                  rc == 0
                  and all(config.provider(bare_cfg, job)["base_url"]
                          == "http://local:9999/v1"
                          for job in config.PROVIDER_JOBS),
                  f"translator={config.provider(bare_cfg, 'translator')!r}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT
            translate_mod.TEMPLATES_SRC_DIR = (REAL_SKILL_ROOT
                                               / "assets" / "templates")


def case_7_init_without_overlay_is_unchanged() -> None:
    """A machine with no overlay file gets exactly the pre-existing behavior:
    --api-base is used verbatim and no extra output appears."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        (skill / "assets" / "templates").mkdir(parents=True)
        (skill / "assets" / "templates" / "translation.md").write_text(
            "placeholder\n", encoding="utf-8")

        project = root / "proj"
        source = project / "source"
        source.mkdir(parents=True)
        (source / "CHAPTER_0001.md").write_text(
            "第一章\nbody\n", encoding="utf-8", newline="\n")

        args = argparse.Namespace(
            project=str(project), title="测试小说", author="测试作者",
            source_url="", source_lang="zh", target_lang="en", tags="",
            api_base="http://from-flag:1/v1", cover_url=None, style="classic",
            background=None, skip_profile=True, force=False,
        )

        translate_mod.SKILL_ROOT = skill
        translate_mod.TEMPLATES_SRC_DIR = skill / "assets" / "templates"
        try:
            rc = translate_mod.cmd_init(args, project)
            on_disk = read_config(project)
            check("7a no overlay: exit 0", rc == 0, f"rc={rc}")
            check("7b no overlay: --api-base is honored verbatim",
                  on_disk["providers"]["translator"][0]["base_url"]
                  == "http://from-flag:1/v1",
                  f"got={on_disk['providers']['translator'][0]['base_url']}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT
            translate_mod.TEMPLATES_SRC_DIR = (REAL_SKILL_ROOT
                                               / "assets" / "templates")


def case_8_corrupt_base_config_is_a_clean_error() -> None:
    """A corrupt project config.json must fail cleanly, never with a traceback.

    Every command resolves the project file through load_config, which shape-
    checks the body it parsed: a config.json that is not a JSON object (a bare
    number, null, an array) is refused outright, and a `providers` that is not
    an object leaves nothing to normalize, which the endpoint requirement then
    refuses. Either way main() turns it into a clean `[FAIL]` line and exit 2
    instead of the AttributeError/TypeError traceback it does not catch, and
    the file is never rewritten on the way out.
    """
    bad_bodies = [
        ("array body", "[]"),
        ("number body", "42"),
        ("null body", "null"),
        ("string body", '"nope"'),
        ("providers is a list", '{"providers": ["x"]}'),
        ("providers is a string", '{"providers": "oops"}'),
    ]
    for label, body in bad_bodies:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "config.json").write_text(body, encoding="utf-8")
            before = (project / "config.json").read_bytes()
            try:
                config.load_config(project)
                check(f"8 corrupt ({label}): raises ValueError", False,
                      "returned without raising")
            except ValueError as exc:
                check(f"8 corrupt ({label}): raises ValueError (not TypeError/AttributeError)",
                      True, f"message={exc}")
            except Exception as exc:
                check(f"8 corrupt ({label}): raises ValueError (not TypeError/AttributeError)",
                      False, f"raised {type(exc).__name__}: {exc}")
            check(f"8 corrupt ({label}): file left byte-identical",
                  (project / "config.json").read_bytes() == before,
                  "config.json was modified by a refused read")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "proj"
        project.mkdir()
        (project / "config.json").write_text("42", encoding="utf-8")
        write_overlay(root, {"seed_min_count": 3})
        translate_mod.SKILL_ROOT = root
        try:
            code = translate_mod.main(
                ["sync-config", "--project", str(project)])
            check("8 cli: corrupt config.json exits 2 (not a traceback/exit 1)",
                  code == 2, f"code={code}")
        except Exception as exc:
            check("8 cli: corrupt config.json exits 2 (not a traceback/exit 1)",
                  False, f"escaped main(): {type(exc).__name__}: {exc}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT

    for label, body in [
        ("plain object",
         '{"source_lang": "zh", "version": 8, "providers": {"translator": '
         '[{"base_url": "http://x/v1"}]}}'),
        ("empty object",
         '{"providers": {"translator": [{"base_url": "http://x/v1"}]}}'),
        ("providers present",
         '{"providers": {"translator": [{"base_url": "u"}]}}'),
    ]:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "config.json").write_text(body, encoding="utf-8")
            write_overlay(Path(td), {"seed_min_count": 3})
            try:
                cfg = config.load_config(project, Path(td))
                check(f"8 legit ({label}): the overlay still resolves onto it",
                      cfg["seed_min_count"] == 3,
                      f"seed_min_count={cfg.get('seed_min_count')!r}")
            except Exception as exc:
                check(f"8 legit ({label}): the overlay still resolves onto it",
                      False, f"{type(exc).__name__}: {exc}")

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        (project / "config.json").write_text(
            '{"version": 8, "seed_min_count": 1, "providers": {"translator": '
            '[{"base_url": "http://x/v1"}]}}', encoding="utf-8-sig")
        write_overlay(Path(td), {"seed_min_count": 3})
        try:
            cfg = config.load_config(project, Path(td))
            check("8 BOM: utf-8-sig config.json still resolves, project key wins",
                  cfg["seed_min_count"] == 1
                  and config.provider(cfg, "translator")["base_url"]
                  == "http://x/v1",
                  f"seed_min_count={cfg.get('seed_min_count')!r}")
        except Exception as exc:
            check("8 BOM: utf-8-sig config.json still resolves, project key wins",
                  False, f"{type(exc).__name__}: {exc}")


def case_9_corrupt_overlay_is_a_clean_error() -> None:
    """A malformed OVERLAY must fail cleanly too.

    Mirror of case 8: load_local_config shape-checks the overlay before the
    providers merge ever reaches into `providers`, so an overlay whose
    `providers` is not an object is refused with a ValueError naming the file
    -- a typo in the user's own config.local.json must not surface as a
    traceback and exit 1 instead of the documented exit 2.
    """
    for label, payload in [
        ("providers is a list", '{"providers": ["x"]}'),
        ("providers is a string", '{"providers": "oops"}'),
        ("providers is a number", '{"providers": 7}'),
    ]:
        with tempfile.TemporaryDirectory() as td:
            write_overlay(Path(td), payload)
            try:
                config.load_local_config(Path(td))
                check(f"9 load ({label}): overlay rejected", False,
                      "returned without raising")
            except ValueError as exc:
                check(f"9 load ({label}): overlay rejected with ValueError",
                      "providers" in str(exc), f"message={exc}")

    for label, over in [
        ("overlay providers list", {"providers": ["x"]}),
        ("overlay providers number", {"providers": 7}),
        ("overlay not a dict", ["not", "an", "object"]),
    ]:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "config.json").write_text(
                '{"version": 8}', encoding="utf-8")
            before = (project / "config.json").read_bytes()
            write_overlay(Path(td), over)
            try:
                config.load_config(project, Path(td))
                check(f"9 load ({label}): raises ValueError", False,
                      "returned without raising")
            except ValueError as exc:
                check(f"9 load ({label}): raises ValueError", True,
                      f"message={exc}")
            except Exception as exc:
                check(f"9 load ({label}): raises ValueError", False,
                      f"raised {type(exc).__name__}: {exc}")
            check(f"9 load ({label}): file untouched",
                  (project / "config.json").read_bytes() == before,
                  "config.json modified by a refused read")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "proj"
        project.mkdir()
        (project / "config.json").write_text(
            '{"version": 8}', encoding="utf-8")
        write_overlay(root, '{"providers": ["x"]}')
        translate_mod.SKILL_ROOT = root
        try:
            code = translate_mod.main(
                ["sync-config", "--project", str(project)])
            check("9 cli: bad overlay providers exits 2 (no traceback)",
                  code == 2, f"code={code}")
        except Exception as exc:
            check("9 cli: bad overlay providers exits 2 (no traceback)",
                  False, f"escaped main(): {type(exc).__name__}: {exc}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        (skill / "assets" / "templates").mkdir(parents=True)
        (skill / "assets" / "templates" / "translation.md").write_text(
            "placeholder\n", encoding="utf-8")
        write_overlay(skill, '{"providers": ["x"]}')

        project = root / "proj"
        (project / "source").mkdir(parents=True)
        (project / "source" / "CHAPTER_0001.md").write_text(
            "第一章\nbody\n", encoding="utf-8", newline="\n")

        args = argparse.Namespace(
            project=str(project), title="测试小说", author="测试作者",
            source_url="", source_lang="zh", target_lang="en", tags="",
            api_base="http://from-flag:1/v1", cover_url=None, style="classic",
            background=None, skip_profile=True, force=False,
        )
        translate_mod.SKILL_ROOT = skill
        translate_mod.TEMPLATES_SRC_DIR = skill / "assets" / "templates"
        try:
            rc = translate_mod.cmd_init(args, project)
            check("9 init: a malformed overlay aborts init with exit 2, "
                  "naming the file", False,
                  f"rc={rc} -- init returned {rc} instead of raising")
        except CliError as exc:
            check("9 init: a malformed overlay aborts init with exit 2, "
                  "naming the file",
                  "config.local.json" in str(exc)
                  and "providers" in str(exc),
                  f"message={exc}")
        except Exception as exc:
            check("9 init: a malformed overlay aborts init with exit 2, "
                  "naming the file", False,
                  f"raised {type(exc).__name__}: {exc}")
        check("9 init: the refused overlay left no half-written config.json",
              not (project / "config.json").is_file()
              or read_config(project)["providers"]["translator"][0]["base_url"]
              == "http://from-flag:1/v1",
              "overlay corrupted a config it was refused for")
        translate_mod.SKILL_ROOT = REAL_SKILL_ROOT
        translate_mod.TEMPLATES_SRC_DIR = (REAL_SKILL_ROOT
                                           / "assets" / "templates")


def _provider_blocks(job_value):
    """A providers entry as a list of blocks: a bare dict is the legacy
    single-block shape, an array is the multi-model shape."""
    if isinstance(job_value, dict):
        return [job_value]
    if isinstance(job_value, list):
        return [b for b in job_value if isinstance(b, dict)]
    return []


def _effective_blocks(jobs: dict):
    """Yield (job, block) for every provider block AS IT WILL RUN.

    A per-job arbitrator (`consensus_<job>`, v015) is deliberately allowed to
    be a partial block -- it merges key-wise onto the global `consensus` block
    at load time. Validating the AUTHORED text instead would report false
    positives on exactly the shape the shipped examples are teaching: a
    `{"model": ...}` block that inherits a working `api_key_env` and a 128000
    `max_tokens` from the global arbitrator has neither of the problems these
    checks exist to catch. So resolve first, then validate what runs.
    """
    global_block = (_provider_blocks(jobs.get("consensus")) or [{}])[0]
    for job, value in jobs.items():
        if not job.startswith(config.CONSENSUS_PREFIX):
            for block in _provider_blocks(value):
                yield job, block
            continue
        for authored in _provider_blocks(value):
            merged = dict(config.PROVIDER_DEFAULTS["consensus"])
            merged.update(global_block)
            merged.update(authored)
            yield job, merged


def case_10_shipped_examples_are_valid() -> None:
    """The shipped example overlays must actually load.

    An example file that cannot be parsed, or that carries a shape the
    loader rejects, is worse than no example -- it invites a user to copy a
    config that fails on first `init`. These two are asserted against the
    real loader so they cannot silently rot as config.py evolves.
    """
    skill_root = Path(__file__).resolve().parent.parent / "novel-translator"
    examples = ["config.local.example.zai.json",
                "config.local.example.minimax.json",
                "config.local.example.mixed.json"]
    for name in examples:
        path = skill_root / name
        check(f"10 {name}: shipped example exists", path.is_file(),
              f"missing {path}")
        if not path.is_file():
            continue

        with tempfile.TemporaryDirectory() as td:
            write_overlay(Path(td), path.read_text(encoding="utf-8"))
            try:
                overlay = config.load_local_config(Path(td))
                check(f"10 {name}: passes load_local_config", overlay is not None)
            except Exception as exc:
                check(f"10 {name}: passes load_local_config", False,
                      f"{type(exc).__name__}: {exc}")
                continue

            check(f"10 {name}: carries no inline api_key",
                  config.inline_api_keys(overlay) == [],
                  f"found {config.inline_api_keys(overlay)}")
            check(f"10 {name}: uses api_key_env, not api_key",
                  all("api_key_env" in block
                      for _job, block in _effective_blocks(
                          overlay.get("providers", {}))),
                  "a block falls back to an inline key")

            project = Path(td) / "proj"
            project.mkdir()
            (project / "config.json").write_text(
                json.dumps({"source_lang": "zh", "target_lang": "en",
                            "version": 8,
                            "providers": overlay["providers"]}),
                encoding="utf-8")
            try:
                cfg = config.load_config(project)
                block = config.provider(cfg, "translator")
                check(f"10 {name}: normalizes to a working translator block",
                      all(block.get(k) for k in ("base_url", "model"))
                      and bool(config.provider_list(cfg, "translator")),
                      f"block={block!r}")
            except Exception as exc:
                check(f"10 {name}: normalizes to a working translator block",
                      False, f"{type(exc).__name__}: {exc}")

        named = set(overlay.get("providers", {}))
        missing = sorted(set(config.PROVIDER_JOBS) - named)
        check(f"10 {name}: covers ALL {len(config.PROVIDER_JOBS)} provider jobs",
              not missing, f"jobs left on the default endpoint: {missing}")

        jobs = overlay.get("providers", {})

        _effective = _effective_blocks

        def _labelled(job, block):
            return f"{job}[{block.get('model', '?')}]"

        unbounded_m3 = sorted(
            _labelled(job, block)
            for job, block in _effective(jobs)
            if block.get("model") == "MiniMax-M3"
            and (block.get("extra_body", {})
                 .get("thinking", {}).get("type")) != "disabled")
        check(f"10 {name}: MiniMax-M3 blocks disable thinking",
              not unbounded_m3,
              f"MiniMax-M3 blocks left thinking on: {unbounded_m3}")

        BOUNDED = {"low", "medium", "high", "xhigh", "max"}
        unpinned_flash = sorted(
            _labelled(job, block)
            for job, block in _effective(jobs)
            if block.get("model") == "MiniMax-M3.1-Flash-Preview"
            and (block.get("extra_body", {})
                 .get("reasoning_effort")) not in BOUNDED)
        check(f"10 {name}: Flash-Preview blocks pin reasoning_effort explicitly",
              not unpinned_flash,
              f"Flash-Preview blocks with no valid reasoning_effort: {unpinned_flash}")

        underbudgeted = sorted(
            _labelled(job, block)
            for job, block in _effective(jobs)
            if block.get("model") == "MiniMax-M3.1-Flash-Preview"
            and (block.get("extra_body", {}).get("reasoning_effort")) == "max"
            and int(block.get("max_tokens", 0)) < 131072)
        check(f"10 {name}: Flash-Preview blocks on `max` carry a >=131072 cap",
              not underbudgeted,
              f"`max` effort under a cap that cannot hold it: {underbudgeted}")

        small = sorted(f"{job}[{block.get('model', '?')}]"
                       for job, block in _effective(jobs)
                       if int(block.get("max_tokens", 0)) < 65536)
        check(f"10 {name}: every block carries the 64k output budget",
              not small, f"blocks below 65536 max_tokens: {small}")

        max_out = overlay.get("translate_max_output_tokens")
        translator_blocks = _provider_blocks(jobs.get("translator"))
        smallest = min((int(b.get("max_tokens", 0))
                        for b in translator_blocks), default=0)
        from lib import pipeline as _pl
        check(f"10 {name}: translate_max_output_tokens is a usable ceiling",
              isinstance(max_out, int) and max_out >= 65536
              and smallest >= _pl.MIN_TRANSLATOR_MAX_TOKENS,
              f"translate_max_output_tokens={max_out!r} "
              f"smallest translator max_tokens={smallest} "
              f"(floor {_pl.MIN_TRANSLATOR_MAX_TOKENS})")

        cblocks = _provider_blocks(jobs.get("consensus")) or _provider_blocks(
            jobs.get("translator"))
        unprotected: list[str] = []
        for cb in cblocks[:1]:
            declared = int(cb.get("max_tokens") or 0)
            synthesis = max(int(max_out or 0), declared)
            if synthesis > declared and not cb.get("max_tokens_limit"):
                unprotected.append(
                    f"max(ceiling {max_out}, block {declared}) = {synthesis} "
                    f"with no max_tokens_limit")
        check(f"10 {name}: the consensus merge cannot over-send its provider",
              not unprotected, f"unsprotected consensus block(s): {unprotected}")


def case_11_per_job_consensus_overlay() -> None:
    """A `consensus_<job>` key behaves like any other provider key under the
    layering (v015) -- which is the point of putting it in `providers`
    rather than in a new top-level section.

    Three paths, all of which the read-time merge already had to handle for
    every other job, so none of them needed special-casing:

    * absent in the project -> the overlay's block is added whole
    * present in the project -> the single-dict form merges KEY-WISE into the
      project's block, so a partial overlay still cannot un-point a working
      endpoint (merge_overlay's "must never quietly un-point" rule)
    * a two-block overlay ARRAY -> rejected at load, before anything runs,
      because an arbitrator is exactly one model
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "add"
        project.mkdir()
        write_config(project, {
            "source_lang": "zh", "version": 8,
            "providers": {"translator": [
                {"base_url": "http://project:8888/v1", "model": None}]},
        })
        write_overlay(root, {"providers": {
            "consensus_translator": {"base_url": "http://local:9999/v1",
                                     "model": "translator-arb"}}})
        added = config.load_config(project, root)
        block = added["providers"]["consensus_translator"]
        check("11a add: a per-job arbitrator absent from the project resolves "
              "whole from the overlay",
              block == [{"base_url": "http://local:9999/v1",
                         "model": "translator-arb"}],
              f"got={block!r}")

        project2 = root / "merge"
        project2.mkdir()
        write_config(project2, {
            "source_lang": "zh", "version": 8,
            "providers": {
                "translator": [{"base_url": "http://project:8888/v1",
                                "model": None}],
                "consensus_translator": {"model": "old-arb",
                                         "api_key_env": "OLD_ARB_KEY"},
            },
        })
        write_overlay(root, {"providers": {
            "consensus_translator": {"base_url": "http://local:9999/v1",
                                     "model": "new-arb"}}})
        merged = config.load_config(project2, root)["providers"]["consensus_translator"]
        check("11c merge: a partial project block keeps its own keys and takes "
              "the endpoint it did not name (a partial layer never un-points a "
              "working provider)",
              merged[0] == {"model": "old-arb", "api_key_env": "OLD_ARB_KEY",
                            "base_url": "http://local:9999/v1"},
              f"got={merged!r}")

        project3 = root / "repair"
        project3.mkdir()
        write_config(project3, {
            "source_lang": "zh", "version": 8,
            "providers": {"translator": [
                {"base_url": "http://project:8888/v1", "model": "m1"},
                {"base_url": "http://project:8888/v1", "model": "m2"}]},
        })
        try:
            config.load_config(project3)
            loads = "loaded anyway"
        except ValueError as exc:
            loads = str(exc)
        check("11f repair: the project is refused without the overlay",
              "providers.consensus is required" in loads, f"got {loads!r}")
        write_overlay(root, {"providers": {
            "consensus": {"base_url": "http://project:8888/v1",
                          "model": "arbiter"}}})
        repaired = config.load_config(project3, root)
        check("11g repair: an overlay that adds consensus composes into a "
              "valid config and the project loads (no lockout)",
              config.provider(repaired, "consensus")["model"] == "arbiter"
              and len(config.provider_list(repaired, "translator")) == 2,
              f"consensus={config.provider(repaired, 'consensus')!r}")

        before = (project2 / "config.json").read_bytes()
        write_overlay(root, {"providers": {
            "consensus_translator": [{"model": "a"}],
            "consensus_annotator": [{"model": "a"}, {"model": "b"}]}})
        try:
            config.load_config(project2, root)
            check("11d reject: a two-block per-job arbitrator is rejected",
                  False, "returned without raising")
        except ValueError as exc:
            check("11d reject: a two-block per-job arbitrator is rejected",
                  "consensus_annotator" in str(exc), f"message={exc}")
        check("11e reject: the working config survived the rejected load",
              (project2 / "config.json").read_bytes() == before,
              "config.json was modified by a refused load")


def _upkeep_project(root: Path, with_config: bool) -> Path:
    """A project shaped for the file-only upkeep commands (glossary count /
    util replace): one source chapter and a manifest, so neither stops on a
    missing file before reaching the config load under test."""
    project = root / "proj"
    source = project / "source"
    source.mkdir(parents=True)
    (source / "CHAPTER_0001.md").write_text(
        "第一章\nbody\n", encoding="utf-8", newline="\n")
    (project / "chapters.json").write_text(
        json.dumps([{"file": "CHAPTER_0001.md", "number": 1,
                     "status": "pending", "title": "第一章"}], indent=2) + "\n",
        encoding="utf-8")
    if with_config:
        write_config(project, {
            "source_lang": "zh", "target_lang": "en", "version": 16,
            "providers": {"translator": [
                {"base_url": "http://local:9999/v1", "model": "m"}]},
        })
    return project


def case_12_lenient_config_is_lenient_only_about_a_missing_file() -> None:
    """`_load_config_lenient` swallows ONE state: no config.json at all.

    `review fix`, `util replace` and `glossary replace|search|count` read
    chapters and glossary.json and need no provider endpoint, so an absent
    config.json is benign -- they fall back to config.DEFAULTS for the one
    threshold each wants. A config that is PRESENT but broken is not: a
    malformed overlay, or a project whose blocks resolve no endpoint, used to
    be caught by `except (OSError, ValueError)` and turned into a silent None,
    so those six commands reported success against defaults the user never
    asked for while their endpoint sat broken on disk. Both are now a clean
    `[FAIL]` and exit 2, exactly as every strict command already reported.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        skill.mkdir()
        write_overlay(skill, "{ broken json")
        project = _upkeep_project(root, with_config=True)

        translate_mod.SKILL_ROOT = skill
        try:
            for label, argv in (
                ("glossary count", ["glossary", "count", "TERM", "--min", "0"]),
                ("glossary search", ["glossary", "search", "TERM"]),
                ("util replace",
                 ["util", "replace", "--source", "旧", "--target", "new"]),
            ):
                out, err, code = _run_main_capturing(
                    [*argv, "--project", str(project)])
                check(f"12 {label}: malformed overlay exits 2",
                      code == 2, f"code={code} err={err!r}")
                check(f"12 {label}: [FAIL] names config.local.json",
                      "[FAIL]" in err and config.LOCAL_CONFIG_NAME in err,
                      f"err={err!r}")
                check(f"12 {label}: no traceback",
                      "Traceback" not in out + err, f"out={out!r} err={err!r}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT

        no_config = _upkeep_project(root / "empty", with_config=False)
        for label, argv in (
            ("glossary count", ["glossary", "count", "TERM", "--min", "0"]),
            ("glossary search", ["glossary", "search", "TERM"]),
            ("util replace",
             ["util", "replace", "--source", "旧", "--target", "new"]),
        ):
            _out, err, code = _run_main_capturing(
                [*argv, "--project", str(no_config)])
            check(f"12 missing config.json ({label}): still a clean run, not a "
                  f"setup error", code in (0, 1) and "[FAIL]" not in err,
                  f"code={code} err={err!r}")


def case_13_init_without_any_endpoint_writes_nothing() -> None:
    """`init` with NEITHER --api-base NOR a resolvable overlay must leave the
    project exactly as it found it.

    The pre-flight used to run AFTER the writes: directories, style.md and a
    bare config.json were on disk before the load that refuses a config with
    no endpoint, so a failed init left a half-built project -- no
    novel_info.json, no glossary.json, no manifest, no repository -- that a
    second `init` then refused ('already exists'). Endpoint resolvability is
    now checked before the first write, so init either completes or writes
    nothing.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        (skill / "assets" / "templates").mkdir(parents=True)
        (skill / "assets" / "templates" / "translation.md").write_text(
            "placeholder\n", encoding="utf-8")

        project = root / "proj"
        source = project / "source"
        source.mkdir(parents=True)
        (source / "CHAPTER_0001.md").write_text(
            "第一章\nbody\n", encoding="utf-8", newline="\n")

        args = argparse.Namespace(
            project=str(project), title="测试小说", author="测试作者",
            source_url="", source_lang="zh", target_lang="en", tags="",
            api_base="", cover_url=None, style="classic",
            background=None, skip_profile=True, force=False,
        )

        translate_mod.SKILL_ROOT = skill
        translate_mod.TEMPLATES_SRC_DIR = skill / "assets" / "templates"
        try:
            try:
                translate_mod.cmd_init(args, project)
                check("13 init with no endpoint anywhere: raises CliError",
                      False, "returned without raising")
            except CliError as exc:
                message = str(exc)
                check("13 init with no endpoint anywhere: raises CliError",
                      True, f"message={exc}")
                check("13 the message names the offending job",
                      "providers.translator" in message, f"message={message}")
                check("13 the message names both remedies",
                      "project's config.json" in message
                      and config.LOCAL_CONFIG_NAME in message,
                      f"message={message}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT
            translate_mod.TEMPLATES_SRC_DIR = (REAL_SKILL_ROOT
                                               / "assets" / "templates")

        leftovers = sorted(
            str(p.relative_to(project)) for p in project.rglob("*")
            if p.relative_to(project).parts[0] != "source")
        check("13 nothing was written: only the user's own source/ remains",
              not leftovers, f"created by the failed init: {leftovers}")
        check("13 no half-built config.json",
              not (project / "config.json").is_file(),
              "config.json exists")
        check("13 no project directory was scaffolded",
              not (project / "draft").exists() and not (project / "translated").exists()
              and not (project / "novel_info.json").is_file(),
              "init created part of the project")


class _Capture:
    """Minimal stdout capture, for the checks that assert what a command
    REPORTED rather than what it wrote to disk."""

    def __enter__(self) -> "_Capture":
        self._buf = io.StringIO()
        self._old = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *_exc) -> None:
        sys.stdout = self._old

    @property
    def out(self) -> str:
        return self._buf.getvalue()


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    capsys = _Capture()

    case_1_load_absent_and_malformed()
    case_2_merge_semantics()
    case_4_inline_api_key_detection()
    case_5_cmd_sync_config()
    case_6_init_resolves_overlay_without_copying()
    case_7_init_without_overlay_is_unchanged()
    case_8_corrupt_base_config_is_a_clean_error()
    case_9_corrupt_overlay_is_a_clean_error()
    case_10_shipped_examples_are_valid()
    case_11_per_job_consensus_overlay()
    case_12_lenient_config_is_lenient_only_about_a_missing_file()
    case_13_init_without_any_endpoint_writes_nothing()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
