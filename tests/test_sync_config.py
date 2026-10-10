"""Tests for the config.local.json overlay: `sync-config` and the init pass.

Covers the skill-level overlay that `init` copies into a new project and
`sync-config` merges into an existing one:

  - load_local_config: absent file -> None (a silent no-op, never an error);
    malformed JSON / non-object body -> ValueError; a `version` key is
    rejected (the stamp belongs to init/migrate alone).
  - merge semantics: the overlay WINS on the keys it names and the project's
    other keys survive -- languages, thresholds, and the version stamp.
    Lists (providers.<job> arrays) are replaced wholesale, never spliced.
  - apply_overlay writes the raw merged file (not load_config's expanded
    form), and validates BEFORE writing so a bad overlay cannot leave a
    config.json that every later command rejects.
  - cmd_sync_config: no overlay -> exit 0 no-op; uninitialized project ->
    CliError (exit 2); inline api_key warns; success commits.
  - cmd_init: applies the overlay, and the merged values reach the
    in-memory cfg the seed/profile passes below it read.
  - a CORRUPT project config.json (a non-object body, or a `providers`
    that is not an object) is refused with a clean ValueError -> exit 2
    and an untouched file, never the AttributeError/TypeError traceback
    that main() does not catch; and every legitimate shape (plain, empty,
    providers-bearing, BOM'd) still merges.

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
import json
import sys
import tempfile
from pathlib import Path

# lib/ and translate.py live at novel-translator/scripts relative to this
# file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config  # noqa: E402
import translate as translate_mod  # noqa: E402
from translate import CliError  # noqa: E402

PASSED = 0
FAILED: list[str] = []

# The skill's real root, restored around the init case. cmd_sync_config and
# cmd_init read translate.SKILL_ROOT at call time, so rebinding the module
# global is enough to point both at a fixture -- no monkeypatching library.
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


# ---------------------------------------------------------------------- cases


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

        # A version key in the overlay would make `migrate` replay or skip
        # steps, so it is rejected outright rather than silently dropped.
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

        # utf-8-sig: a hand-edited file saved with a BOM must still load.
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

    # A partial provider block (model only) must NOT wipe the project's
    # base_url: replacing the array here would drop the endpoint, and the
    # missing value would then fall back to the hard-coded DEFAULT_BASE_URL
    # -- silently pointing the next run at a different server.
    partial = config.merge_overlay(project_cfg, {
        "providers": {"translator": {"model": "only-model"}}})
    check("2f partial: dict block merges into every project block",
          partial["providers"]["translator"]
          == [{"base_url": "http://project:8888/v1", "model": "only-model"}],
          f"got={partial['providers']['translator']}")

    # A multi-block (consensus fan-out) job: the partial overlay applies to
    # every block, and no block is dropped or duplicated.
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


def case_3_apply_overlay_writes_raw_merged_form() -> None:
    """The written file is raw+overlay, not load_config's expanded form, and
    a bad overlay is rejected before it can replace a working config."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "proj"
        project.mkdir()
        write_config(project, {
            "source_lang": "zh",
            "version": 8,
            "providers": {"translator": [
                {"base_url": "http://project:8888/v1", "model": None}]},
        })
        overlay = {"providers": {"translator": [
            {"base_url": "http://local:9999/v1"}]}}

        changed, lines = config.apply_overlay(project, overlay)
        on_disk = read_config(project)

        check("3a apply: changed keys name the provider path",
              any("translator" in key for key in changed),
              f"changed={changed}")
        check("3b apply: report line is [ok] and names the count",
              lines and lines[0].startswith("[ok] config.local.json:"),
              f"lines={lines}")
        check("3c apply: overlay values landed on disk",
              on_disk["providers"]["translator"][0]["base_url"]
              == "http://local:9999/v1",
              f"got={on_disk['providers']['translator'][0]}")
        check("3d apply: version stamp preserved verbatim",
              on_disk["version"] == 8, f"version={on_disk['version']}")
        # The file must NOT gain every DEFAULTS key: a minimal diff keeps the
        # change reviewable and leaves `version` untouched by the merge.
        check("3e apply: no DEFAULTS keys expanded into the file",
              "auto_build_epub" not in on_disk,
              f"keys={sorted(on_disk)}")

        # Re-running with the same overlay is a no-op (idempotent).
        again, lines2 = config.apply_overlay(project, overlay)
        check("3f idempotent: re-applying the same overlay changes nothing",
              again == [] and "no changes" in lines2[0],
              f"changed={again}, lines={lines2}")

        # An overlay that would make the config unloadable must be rejected
        # BEFORE the working file is touched.
        before = read_config(project)
        try:
            config.apply_overlay(project, {
                "providers": {"translator": ["not-a-block"]}})
            check("3g invalid: bad providers shape raises ValueError", False,
                  "returned without raising")
        except ValueError:
            check("3g invalid: bad providers shape raises ValueError", True)
        check("3h invalid: the working config survived the rejected merge",
              read_config(project) == before,
              "config.json was modified by a failed apply")


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
    """cmd_sync_config: no overlay -> 0, uninitialized -> 2, success commits."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        skill.mkdir()
        project = root / "proj"
        project.mkdir()

        args = argparse.Namespace(project=str(project), dry_run=False,
                                 force=False)

        # No overlay at all: a clean no-op, NOT an error.
        translate_mod.SKILL_ROOT = skill
        try:
            rc = translate_mod.cmd_sync_config(args, project)
            check("5a no overlay: exit 0 no-op", rc == 0, f"rc={rc}")

            # Uninitialized project with an overlay present -> CliError (2).
            write_overlay(skill, {"source_lang": "ja"})
            try:
                translate_mod.cmd_sync_config(args, project)
                check("5b uninitialized: missing config.json raises CliError",
                      False, "returned without raising")
            except CliError as exc:
                check("5b uninitialized: missing config.json raises CliError",
                      "init" in str(exc), f"message={exc}")

            # Malformed overlay -> CliError (exit 2), never a traceback.
            write_overlay(skill, "{ broken")
            try:
                translate_mod.cmd_sync_config(args, project)
                check("5c malformed overlay: raises CliError", False,
                      "returned without raising")
            except CliError as exc:
                check("5c malformed overlay: raises CliError",
                      "config" in str(exc), f"message={exc}")

            # The real thing: overlay merges into the project config.
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
            rc = translate_mod.cmd_sync_config(args, project)
            on_disk = read_config(project)
            check("5d success: exit 0", rc == 0, f"rc={rc}")
            check("5e success: overlay provider landed",
                  on_disk["providers"]["translator"][0]["base_url"]
                  == "http://local:9999/v1",
                  f"got={on_disk['providers']['translator'][0]}")
            check("5f success: project's own keys preserved",
                  on_disk["source_lang"] == "zh"
                  and on_disk["target_lang"] == "en"
                  and on_disk["version"] == 8,
                  f"got={ {k: on_disk.get(k) for k in ('source_lang','target_lang','version')} }")
            check("5g success: inline api_key was copied through",
                  on_disk["providers"]["translator"][0].get("api_key")
                  == "sk-secret",
                  "api_key missing from the merged config")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT


def case_6_init_applies_overlay() -> None:
    """cmd_init copies the overlay into a fresh project, and the merged values
    reach the in-memory cfg the seed pass reads below it."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "skill"
        (skill / "assets" / "templates").mkdir(parents=True)
        # init requires at least one shipped template, and reads the
        # catalogues dir only when present (absence just warns).
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
            loaded = config.load_config(project)

            check("6a init: exit 0", rc == 0, f"rc={rc}")
            check("6b init: overlay provider beat the --api-base flag",
                  on_disk["providers"]["translator"][0]["base_url"]
                  == "http://local:9999/v1",
                  f"got={on_disk['providers']['translator'][0]['base_url']}")
            check("6c init: overlay scalar landed and loads",
                  loaded["seed_min_count"] == 11,
                  f"seed_min_count={loaded['seed_min_count']}")
            check("6d init: init's own keys survive the overlay",
                  on_disk["source_lang"] == "zh"
                  and on_disk["target_lang"] == "en",
                  f"langs={on_disk['source_lang']}/{on_disk['target_lang']}")
            check("6e init: version stamped by init, not by the overlay",
                  isinstance(on_disk.get("version"), int)
                  and on_disk["version"] > 0,
                  f"version={on_disk.get('version')!r}")
            check("6f init: manifest still built after the overlay pass",
                  (project / "chapters.json").is_file(),
                  "chapters.json missing")
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

    apply_overlay reads the RAW file and feeds it to _deep_merge, so a
    config.json that is not a JSON object (a bare number, null, an array)
    -- or whose `providers` is not an object -- used to raise
    AttributeError/TypeError. main() catches CliError/ValueError but NOT
    those, so the CLI died with a traceback and exit 1 instead of the
    documented exit 2. The base is now shape-checked up front.
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
                config.apply_overlay(project, {"seed_min_count": 3})
                check(f"8 corrupt ({label}): raises ValueError", False,
                      "returned without raising")
            except ValueError as exc:
                check(f"8 corrupt ({label}): raises ValueError (not TypeError/AttributeError)",
                      True, f"message={exc}")
            except Exception as exc:  # noqa: BLE001 - the regression itself
                check(f"8 corrupt ({label}): raises ValueError (not TypeError/AttributeError)",
                      False, f"raised {type(exc).__name__}: {exc}")
            check(f"8 corrupt ({label}): file left byte-identical",
                  (project / "config.json").read_bytes() == before,
                  "config.json was modified by a refused merge")

    # And the CLI turns it into the documented exit 2, not a traceback.
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
        except Exception as exc:  # noqa: BLE001 - a traceback escaping main()
            check("8 cli: corrupt config.json exits 2 (not a traceback/exit 1)",
                  False, f"escaped main(): {type(exc).__name__}: {exc}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT

    # No false positives: every legitimate shape still merges.
    for label, body in [
        ("plain object", '{"source_lang": "zh", "version": 8}'),
        ("empty object", "{}"),
        ("providers present", '{"providers": {"translator": [{"base_url": "u"}]}}'),
    ]:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "config.json").write_text(body, encoding="utf-8")
            try:
                _changed, lines = config.apply_overlay(
                    project, {"seed_min_count": 3})
                check(f"8 legit ({label}): still merges cleanly",
                      lines and lines[0].startswith("[ok]"), f"lines={lines}")
            except Exception as exc:  # noqa: BLE001
                check(f"8 legit ({label}): still merges cleanly", False,
                      f"{type(exc).__name__}: {exc}")

    # A BOM'd config.json is legitimate and must keep working (utf-8-sig).
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        (project / "config.json").write_text(
            '{"version": 8, "seed_min_count": 1}', encoding="utf-8-sig")
        try:
            _changed, _lines = config.apply_overlay(project, {"seed_min_count": 9})
            check("8 BOM: utf-8-sig config.json still merges",
                  config.load_config(project)["seed_min_count"] == 9,
                  "BOM'd file was not handled")
        except Exception as exc:  # noqa: BLE001
            check("8 BOM: utf-8-sig config.json still merges", False,
                  f"{type(exc).__name__}: {exc}")


def case_9_corrupt_overlay_is_a_clean_error() -> None:
    """A malformed OVERLAY must fail cleanly too.

    Mirror of case 8: the base guard does not cover the overlay, and the
    providers merge special case reaches into `providers` directly, so an
    overlay whose `providers` is not an object raised AttributeError inside
    merge_overlay -- which main() does not catch, so a typo in the user's own
    config.local.json surfaced as a traceback and exit 1 instead of the
    documented exit 2.
    """
    # Rejected at load time, naming the overlay file.
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

    # And apply_overlay refuses it directly, even for a hand-built overlay
    # that never went through load_local_config.
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
            try:
                config.apply_overlay(project, over)
                check(f"9 apply ({label}): raises ValueError", False,
                      "returned without raising")
            except ValueError as exc:
                check(f"9 apply ({label}): raises ValueError", True,
                      f"message={exc}")
            except Exception as exc:  # noqa: BLE001 - the regression itself
                check(f"9 apply ({label}): raises ValueError", False,
                      f"raised {type(exc).__name__}: {exc}")
            check(f"9 apply ({label}): file untouched",
                  (project / "config.json").read_bytes() == before,
                  "config.json modified by a refused merge")

    # CLI level: a bad overlay exits 2 with one [FAIL], no traceback.
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
        except Exception as exc:  # noqa: BLE001 - a traceback escaping main()
            check("9 cli: bad overlay providers exits 2 (no traceback)",
                  False, f"escaped main(): {type(exc).__name__}: {exc}")
        finally:
            translate_mod.SKILL_ROOT = REAL_SKILL_ROOT

    # init must SURVIVE a bad overlay (warn, do not abort).
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
            check("9 init: a bad overlay warns but does NOT abort init",
                  rc == 0 and (project / "config.json").is_file(),
                  f"rc={rc}")
            check("9 init: the flag endpoint survived the refused overlay",
                  read_config(project)["providers"]["translator"][0]["base_url"]
                  == "http://from-flag:1/v1",
                  "overlay corrupted a config it was refused for")
        finally:
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

        # Valid JSON object, and it must pass the REAL reader (shape rules,
        # no `version`, providers must be an object).
        with tempfile.TemporaryDirectory() as td:
            write_overlay(Path(td), path.read_text(encoding="utf-8"))
            try:
                overlay = config.load_local_config(Path(td))
                check(f"10 {name}: passes load_local_config", overlay is not None)
            except Exception as exc:  # noqa: BLE001
                check(f"10 {name}: passes load_local_config", False,
                      f"{type(exc).__name__}: {exc}")
                continue

            # No inline secrets: examples must model the safe form.
            check(f"10 {name}: carries no inline api_key",
                  config.inline_api_keys(overlay) == [],
                  f"found {config.inline_api_keys(overlay)}")
            check(f"10 {name}: uses api_key_env, not api_key",
                  all("api_key_env" in block
                      for _job, block in _effective_blocks(
                          overlay.get("providers", {}))),
                  "a block falls back to an inline key")

            # And it must normalize into a usable project config.
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
            except Exception as exc:  # noqa: BLE001
                check(f"10 {name}: normalizes to a working translator block",
                      False, f"{type(exc).__name__}: {exc}")

        # Every job must be covered. A partial overlay leaves the rest on the
        # hard-coded DEFAULT_BASE_URL (a LAN sglang box), which fails at the
        # first ping with a connection error rather than anything obvious.
        named = set(overlay.get("providers", {}))
        missing = sorted(set(config.PROVIDER_JOBS) - named)
        check(f"10 {name}: covers ALL {len(config.PROVIDER_JOBS)} provider jobs",
              not missing, f"jobs left on the default endpoint: {missing}")

        # Reasoning-budget invariants, each measured live against the APIs.
        #
        #  * MiniMax-M3 has no depth knob: reasoning_effort is silently
        #    IGNORED by it. Left on, it inlines a <think> block into content
        #    and spends the entire cap thinking (4096/4096 tokens,
        #    finish_reason=length, 30s, no answer). The only thing that stops
        #    it is extra_body.thinking.type=disabled, which answers directly
        #    in 42s. So M3 blocks MUST disable thinking.
        #  * MiniMax-M3.1-Flash-Preview rejects thinking:disabled with HTTP 400
        #    ("requires adaptive thinking"), so it must stay ON and have its
        #    depth pinned via reasoning_effort instead. Omitted means the
        #    server default `max`, which is non-terminating on a real chapter:
        #    measured 19m51s and zero content. `high`/`xhigh` complete it.
        #  * Both models also ignore chat_template_kwargs.enable_thinking, so
        #    a block relying on that is unprotected either way.
        jobs = overlay.get("providers", {})

        # Blocks are read in their EFFECTIVE form (see _effective_blocks): a
        # per-job arbitrator may be authored partial and inherit from the
        # global `consensus`, and these checks are about what will run.
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

        # `max` is only safe when the cap can hold the reasoning it produces:
        # at 65536 it does NOT converge (19m51s, finish_reason=length, no
        # content key at all), at 256000 it converged on every measured run.
        # So pinning `max` while leaving the old budget would reproduce the
        # original 20-minute failure -- require the large cap to go with it.
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

        # Since v012 translate_max_output_tokens is a CEILING, not an override:
        # client.chat clamps the sent cap down to each block's own max_tokens,
        # and pipeline packs to min(ceiling, tightest block) so no model in the
        # array truncates its part. So a translator block BELOW the ceiling is
        # a supported configuration, not a defect -- the old
        # `smallest >= max_out` check is now INVERTED, and enforcing it would
        # forbid exactly the shape this change exists to support.
        # What still must hold: the ceiling is a sane int, and no translator
        # block is so small that chapters cannot be packed into it
        # (pipeline.MIN_TRANSLATOR_MAX_TOKENS).
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

        # The consensus merge is the ONE call allowed past its own block's
        # max_tokens: consensus.chat raises it to max(task cap, block cap) so
        # an under-provisioned arbitrator can still combine large candidates.
        # That is safe only when the block declares `max_tokens_limit` -- its
        # provider's real ceiling. Without it, an example whose ceiling sits
        # above the block's own cap is a shipped 400: this is exactly how
        # config.local.example.zai.json reproduced the original bug, since
        # every one of its blocks is Z.AI (wall 131072) and `consensus`
        # inherits translator[0].
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
    """A `consensus_<job>` key behaves like any other provider key under
    `sync-config` (v015) -- which is the point of putting it in `providers`
    rather than in a new top-level section.

    Three paths, all of which the merge already had to handle for every other
    job, so none of them needed special-casing:

    * absent in the project -> the overlay's block is added wholesale
    * present in the project -> the single-dict form merges KEY-WISE into the
      existing block, so a partial overlay still cannot un-point a working
      endpoint (merge_overlay's "must never quietly un-point" rule)
    * a two-block overlay ARRAY -> rejected before anything is written, because
      an arbitrator is exactly one model
    """
    with tempfile.TemporaryDirectory() as td:
        # --- added to a project that has no such key -----------------------
        project = Path(td) / "add"
        project.mkdir()
        write_config(project, {
            "source_lang": "zh", "version": 8,
            "providers": {"translator": [
                {"base_url": "http://project:8888/v1", "model": None}]},
        })
        changed, lines = config.apply_overlay(project, {"providers": {
            "consensus_translator": {"base_url": "http://local:9999/v1",
                                     "model": "translator-arb"}}})
        on_disk = read_config(project)
        check("11a add: a per-job arbitrator in the overlay is added whole",
              on_disk["providers"]["consensus_translator"]
              == {"base_url": "http://local:9999/v1", "model": "translator-arb"},
              f"got={on_disk['providers'].get('consensus_translator')}")
        check("11b add: the changed key names the per-job path",
              any("consensus_translator" in key for key in changed),
              f"changed={changed}")

        # --- merged key-wise into an existing block -------------------------
        project2 = Path(td) / "merge"
        project2.mkdir()
        write_config(project2, {
            "source_lang": "zh", "version": 8,
            "providers": {
                "translator": [{"base_url": "http://project:8888/v1",
                                "model": None}],
                "consensus_translator": {"base_url": "http://project:8888/v1",
                                         "model": "old-arb"},
            },
        })
        config.apply_overlay(project2, {"providers": {
            "consensus_translator": {"model": "new-arb"}}})
        merged = read_config(project2)["providers"]["consensus_translator"]
        check("11c merge: a partial overlay overrides only the key it names "
              "and keeps the working base_url",
              merged == {"base_url": "http://project:8888/v1", "model": "new-arb"},
              f"got={merged}")

        # --- the repair path: a fan-out with no consensus is refused at load
        # (v015), so sync-config must be able to FIX it -- an overlay that adds
        # `consensus` merges into a config that then validates. Without this,
        # the rule would be a lockout: the only way to repair the project would
        # be to edit it by hand.
        project3 = Path(td) / "repair"
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
        check("11f repair: the project is refused before the overlay",
              "providers.consensus is required" in loads, f"got {loads!r}")
        _changed, _lines = config.apply_overlay(project3, {"providers": {
            "consensus": {"base_url": "http://project:8888/v1",
                          "model": "arbiter"}}})
        repaired = config.load_config(project3)
        check("11g repair: an overlay that adds consensus applies cleanly and "
              "the project loads afterwards (no lockout)",
              config.provider(repaired, "consensus")["model"] == "arbiter"
              and len(config.provider_list(repaired, "translator")) == 2,
              f"consensus={config.provider(repaired, 'consensus')!r}")

        # --- an array overlay is rejected before the write ------------------
        before = read_config(project2)
        try:
            config.apply_overlay(project2, {"providers": {
                "consensus_annotator": [{"model": "a"}, {"model": "b"}]}})
            check("11d reject: a two-block per-job arbitrator is rejected",
                  False, "returned without raising")
        except ValueError as exc:
            check("11d reject: a two-block per-job arbitrator is rejected",
                  "consensus_annotator" in str(exc), f"message={exc}")
        check("11e reject: the working config survived the rejected merge",
              read_config(project2) == before,
              "config.json was modified by a failed apply")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_load_absent_and_malformed()
    case_2_merge_semantics()
    case_3_apply_overlay_writes_raw_merged_form()
    case_4_inline_api_key_detection()
    case_5_cmd_sync_config()
    case_6_init_applies_overlay()
    case_7_init_without_overlay_is_unchanged()
    case_8_corrupt_base_config_is_a_clean_error()
    case_9_corrupt_overlay_is_a_clean_error()
    case_10_shipped_examples_are_valid()
    case_11_per_job_consensus_overlay()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())