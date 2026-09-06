"""Tests for the migrate command and the migrations/ script chain.

Covers v001's behavior end to end against fixture templates (missing ->
copied, identical -> untouched, differing -> warned and kept, --force ->
refreshed byte-equal to the shipped copy; DEFAULTS materialized on disk
while user values survive; version stamped to 1; a second run is a
byte-level no-op; --dry-run writes and stamps nothing), the chain walker
with fake steps (ascending application order, version stamped after EACH
step so a crash mid-chain resumes at the failed step, a step's exception
propagates instead of being swallowed, an already-current project runs
nothing, a project newer than the chain is a CliError, dry-run stays
read-only), the missing-project CliError, and the real package's chain()
== [v001] with current_version() == 1.

cmd_migrate reads translate.TEMPLATES_SRC_DIR and migrations.chain at call
time (module-global lookups), so both are monkeypatched by attribute swap
with manual orig/restore in try/finally. All fixtures live in
TemporaryDirectory sandboxes; repo assets are never touched.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_migrate.py
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import types
from pathlib import Path

# scripts/ (and therefore lib/ and migrations/) lives at
# novel-translator/scripts relative to this file (CWD-independent);
# translate.py puts it on sys.path itself as well.
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config  # noqa: E402
import migrations  # noqa: E402
import translate  # noqa: E402

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


def run_migrate(ns: argparse.Namespace, project_dir: Path):
    """translate.cmd_migrate with stdout captured; returns (code, output,
    exc) -- code is None when the call raised (the caller asserts on exc)."""
    buf = io.StringIO()
    code: int | None = None
    exc: Exception | None = None
    try:
        with contextlib.redirect_stdout(buf):
            code = translate.cmd_migrate(ns, project_dir)
    except Exception as caught:  # noqa: BLE001 - the caller asserts on it
        exc = caught
    return code, buf.getvalue(), exc


def snapshot(root: Path) -> dict[str, bytes]:
    """Byte-exact contents of every file under root, keyed by relative path
    (proves a no-op run wrote nothing -- stronger than mtime on Windows)."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# A config written by a pre-versioning init: user-tuned tn_gap_chapters and
# a custom translator endpoint, but none of today's DEFAULTS keys and no
# other provider jobs.
FIXTURE_CFG = {
    "source_lang": "zh",
    "target_lang": "en",
    "tn_gap_chapters": 5,
    "providers": {"translator": {"base_url": "http://mine:9999/v1", "model": "m1"}},
}


def case_1_v001() -> None:
    """v001 against fixture templates: copy missing, keep identical, warn on
    differing (overwrite only with --force); materialize DEFAULTS preserving
    user values; stamp version; second run no-op; --dry-run writes nothing."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "ship"
        src.mkdir()
        for name, body in (("a.md", "alpha\n"), ("b.md", "bravo\n"), ("c.md", "charlie\n")):
            (src / name).write_text(body, encoding="utf-8", newline="\n")

        def make_project(name: str) -> Path:
            proj = root / name
            (proj / "templates").mkdir(parents=True)
            (proj / "templates" / "a.md").write_text("alpha\n", encoding="utf-8", newline="\n")
            (proj / "templates" / "b.md").write_text("user-edited b\n", encoding="utf-8", newline="\n")
            (proj / "config.json").write_text(
                json.dumps(FIXTURE_CFG, indent=2) + "\n", encoding="utf-8"
            )
            return proj

        ns = argparse.Namespace(dry_run=False, force=False)
        orig_tpl = translate.TEMPLATES_SRC_DIR
        translate.TEMPLATES_SRC_DIR = src  # cmd_migrate reads it at call time
        try:
            proj = make_project("main")
            code, out, exc = run_migrate(ns, proj)
            check("1a v001: first run exits 0 without error",
                  code == 0 and exc is None, f"code={code} exc={exc!r}")
            check("1b v001: missing c.md copied",
                  (proj / "templates" / "c.md").read_text(encoding="utf-8") == "charlie\n"
                  and "[ok] templates + c.md (new)" in out, f"out={out}")
            check("1c v001: identical a.md untouched",
                  (proj / "templates" / "a.md").read_text(encoding="utf-8") == "alpha\n")
            check("1d v001: differing b.md kept without --force",
                  (proj / "templates" / "b.md").read_text(encoding="utf-8") == "user-edited b\n")
            check("1e v001: differing b.md reported as a warning",
                  "[warn] templates ~ b.md differs from shipped" in out, f"out={out}")
            disk = json.loads((proj / "config.json").read_text(encoding="utf-8"))
            missing = [k for k in config.DEFAULTS if k not in disk]
            check("1f v001: every config.DEFAULTS key materialized on disk",
                  not missing, f"missing={missing}")
            check("1g v001: user value tn_gap_chapters preserved",
                  disk.get("tn_gap_chapters") == 5, f"tn_gap_chapters={disk.get('tn_gap_chapters')}")
            check("1h v001: user provider endpoint preserved",
                  disk["providers"]["translator"]["base_url"] == "http://mine:9999/v1"
                  and disk["providers"]["translator"]["model"] == "m1")
            check("1i v001: all five provider jobs filled",
                  sorted(disk["providers"]) == sorted(config.PROVIDER_JOBS),
                  f"jobs={sorted(disk['providers'])}")
            check("1j v001: version stamped to 1",
                  disk.get("version") == 1, f"version={disk.get('version')!r}")

            before = snapshot(proj)
            code2, out2, exc2 = run_migrate(ns, proj)
            check("1k v001: second run is a no-op already at version 1",
                  code2 == 0 and exc2 is None and "already at version 1" in out2,
                  f"code={code2} out={out2}")
            check("1l v001: second run wrote nothing (byte snapshot equal)",
                  snapshot(proj) == before)

            # Fresh version-0 project: the main project is already stamped,
            # so the version gate would (correctly) skip v001 and --force
            # would never reach the template refresh.
            force = make_project("force")
            code3, out3, exc3 = run_migrate(
                argparse.Namespace(dry_run=False, force=True), force)
            check("1m v001: --force run exits 0", code3 == 0 and exc3 is None,
                  f"code={code3} exc={exc3!r}")
            check("1n v001: --force overwrites b.md byte-equal to the shipped copy",
                  (force / "templates" / "b.md").read_bytes() == (src / "b.md").read_bytes()
                  and "[ok] templates ~ b.md refreshed" in out3, f"out={out3}")

            dry = make_project("dry")
            before_dry = snapshot(dry)
            code4, out4, exc4 = run_migrate(argparse.Namespace(dry_run=True, force=False), dry)
            check("1o v001: --dry-run reports the would-be changes",
                  code4 == 0 and exc4 is None and "c.md (new)" in out4
                  and "config: materialized" in out4, f"code={code4} out={out4}")
            check("1o2 v001: --dry-run output is dry-run-marked",
                  "[dry-run] [ok] templates + c.md" in out4
                  and "would migrate project: version 0 -> 1" in out4
                  and "applying" not in out4, f"out={out4}")
            dry_disk = json.loads((dry / "config.json").read_text(encoding="utf-8"))
            check("1p v001: --dry-run copies nothing and stamps nothing",
                  snapshot(dry) == before_dry and "version" not in dry_disk,
                  f"version={dry_disk.get('version')!r}")

            # A project with no templates/ dir at all: --dry-run must not
            # even create it; a real run creates it lazily on first copy.
            bare = root / "bare"
            bare.mkdir()
            (bare / "config.json").write_text(
                json.dumps(FIXTURE_CFG, indent=2) + "\n", encoding="utf-8"
            )
            run_migrate(argparse.Namespace(dry_run=True, force=False), bare)
            check("1q v001: --dry-run leaves a nonexistent templates/ uncreated",
                  not (bare / "templates").exists())
            code5, out5, exc5 = run_migrate(
                argparse.Namespace(dry_run=False, force=False), bare)
            check("1r v001: real run creates templates/ and copies into it",
                  code5 == 0 and exc5 is None
                  and sorted(p.name for p in (bare / "templates").iterdir())
                  == ["a.md", "b.md", "c.md"],
                  f"code={code5} exc={exc5!r}")
        finally:
            translate.TEMPLATES_SRC_DIR = orig_tpl


def case_2_walker() -> None:
    """The chain walker with fake steps: ascending order, per-step version
    stamping, crash propagation, already-current, newer-than-chain, dry-run.

    cmd_migrate must require nothing on the namespace beyond dry_run/force
    (the real parser adds --project, but the command itself cannot depend on
    it), and a step's arbitrary exception must propagate: main() maps
    CliError to exit 2, but swallowing a step crash would fake a migration
    that never completed."""
    def make_step(version: int, fail: bool = False):
        def migrate(project_dir, templates_src, dry_run=False, force=False):
            if not dry_run:
                with (project_dir / "marker.txt").open("a", encoding="utf-8") as fh:
                    fh.write(f"v{version} ")
            if fail:
                raise RuntimeError(f"boom v{version}")
            return [f"[ok] fake step v{version}"]
        return types.SimpleNamespace(
            VERSION=version, DESCRIPTION=f"fake v{version}", migrate=migrate)

    def make_project(td: str, version: int | None = None) -> Path:
        root = Path(td)
        cfg = {"source_lang": "zh", "target_lang": "en", "providers": {}}
        if version is not None:
            cfg["version"] = version
        (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return root

    def with_chain(steps, ns, root):
        orig = migrations.chain  # current_version() follows the swap too
        migrations.chain = lambda: list(steps)
        try:
            return run_migrate(ns, root)
        finally:
            migrations.chain = orig

    ns = argparse.Namespace(dry_run=False, force=False)

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        code, out, exc = with_chain([make_step(2), make_step(3)], ns, root)
        disk = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("2a walker: both fake steps applied, exit 0",
              code == 0 and exc is None, f"code={code} exc={exc!r}")
        check("2b walker: steps run in ascending version order",
              (root / "marker.txt").read_text(encoding="utf-8") == "v2 v3 ")
        check("2c walker: version stamped to the last applied step",
              disk.get("version") == 3, f"version={disk.get('version')!r}")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        code, out, exc = with_chain([make_step(2), make_step(3, fail=True)], ns, root)
        disk = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("2d walker: a failing step's exception propagates (not swallowed)",
              isinstance(exc, RuntimeError) and "boom v3" in str(exc), f"exc={exc!r}")
        check("2e walker: crash mid-chain leaves version at the last COMPLETED step",
              disk.get("version") == 2, f"version={disk.get('version')!r}")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, version=3)
        code, out, exc = with_chain([make_step(2), make_step(3)], ns, root)
        check("2f walker: already-current project runs nothing",
              code == 0 and exc is None and not (root / "marker.txt").exists()
              and "already at version 3" in out, f"code={code} out={out}")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td, version=4)
        code, out, exc = with_chain([make_step(2), make_step(3)], ns, root)
        check("2g walker: project newer than the chain -> CliError",
              isinstance(exc, translate.CliError) and "newer" in str(exc), f"exc={exc!r}")

    with tempfile.TemporaryDirectory() as td:
        root = make_project(td)
        code, out, exc = with_chain(
            [make_step(2), make_step(3)], argparse.Namespace(dry_run=True, force=False), root)
        disk = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("2h walker: dry-run stays read-only (steps ran, nothing written)",
              code == 0 and exc is None and "would apply v002" in out
              and "[dry-run] [ok] fake step v2" in out
              and not (root / "marker.txt").exists() and "version" not in disk,
              f"code={code} out={out} disk={disk}")


def case_3_not_a_project() -> None:
    """migrate on a directory without config.json -> CliError (exit 2)."""
    with tempfile.TemporaryDirectory() as td:
        code, out, exc = run_migrate(
            argparse.Namespace(dry_run=False, force=False), Path(td))
        check("3a no project: CliError raised",
              isinstance(exc, translate.CliError), f"exc={exc!r}")
        check("3b no project: message points at init",
              exc is not None and "init" in str(exc), f"exc={exc}")


def case_4_real_chain() -> None:
    """The real migrations package: exactly v001, VERSION 1, head version 1."""
    steps = migrations.chain()
    check("4a real chain: exactly one step, v001, VERSION 1",
          len(steps) == 1 and steps[0].VERSION == 1
          and steps[0].__name__.endswith("v001"),
          f"steps={[getattr(s, '__name__', s) for s in steps]}")
    check("4b real chain: v001 exposes DESCRIPTION and callable migrate",
          bool(steps) and isinstance(steps[0].DESCRIPTION, str)
          and callable(steps[0].migrate))
    check("4c real chain: current_version() == 1", migrations.current_version() == 1)


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_v001()
    case_2_walker()
    case_3_not_a_project()
    case_4_real_chain()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
