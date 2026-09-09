"""Tests for project git history: lib/vcs, init's repo scaffold, v003.

Covers vcs.ensure_repo (an empty directory -> ["[git] initialized
repository"] plus a .git directory and a .gitignore whose text equals
vcs.GITIGNORE; a second call returns [] and leaves .gitignore
byte-identical), vcs.commit (a dirty tree returns a truthy short sha and
prints "[git] committed <sha> <subject>", with the subject landing in
`git log --format=%s`; a clean tree, a raw config.json {"git_commits":
false}, and a non-repository directory are all silent None no-ops),
cmd_init end to end (a scaffolded project gets exactly one
"init: scaffold project" commit plus "[git] initialized repository" and
"[git] committed" on stdout, and raw config.json carries
git_commits: true -- and a --force reinitialization keeps the history as
a second "init: reinitialize project" commit), v003.migrate called
directly (materializes the git_commits default and git-inits the project
without committing or stamping the version -- cmd_migrate owns the
post-stamp commit, proven here by a direct commit succeeding on the fresh
repo afterwards; a second identical run is a quiet byte-level no-op), and
v003's dry-run discipline (reports "would initialize the repository" but
creates no .git, no .gitignore, and leaves config.json untouched), and the
never-raise contract (with vcs._run swapped for a raiser, commit() on an
initialized repo returns None printing exactly one "[warn] git failed:
OSError: boom" line, and ensure_repo() on a fresh dir returns that same
line as its only report).

Every git-dependent check degrades gracefully when no git binary is on
PATH (vcs.available(), i.e. shutil.which("git") is None): a note is
printed and the case keeps the exit at 0, while the git-independent
assertions still run (the non-repo commit no-op, and v003's dry-run,
whose only vcs call is the pure path check is_repo). git output is probed
via subprocess, CLI stdout captured with contextlib.redirect_stdout, and
the one piece of module state involved (translate.TEMPLATES_SRC_DIR) is
swapped with orig/restore in try/finally -- no unittest.mock. All
fixtures live in TemporaryDirectory sandboxes; the real skill repo is
never touched.

Self-contained PASS/FAIL script (no pytest). The lib modules and
scripts/translate.py import pyyaml, requests, ebooklib and pillow, so run
via uv (deps declared inline below):

    uv run tests/test_git.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# scripts/ (and therefore lib/ and migrations/) lives at
# novel-translator/scripts relative to this file (CWD-independent);
# translate.py puts it on sys.path itself as well.
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import translate  # noqa: E402
from lib import config, vcs  # noqa: E402
from migrations import v003  # noqa: E402

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


def git_log_subjects(proj: Path) -> tuple[int, list[str]]:
    """`git log --format=%s` inside proj: (returncode, subjects,
    newest first). The returncode is asserted by every caller so a broken
    repo can never masquerade as 'no commits'."""
    proc = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(proj), capture_output=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode, proc.stdout.splitlines()


# ---------------------------------------------------------------------- cases


def case_1_ensure_repo() -> None:
    """An empty directory becomes a repository exactly once: first call
    reports the init line and writes .git + .gitignore; a second call is a
    quiet [] that leaves .gitignore byte-identical."""
    if not vcs.available():
        print("note: git not found on PATH; skipping the ensure_repo checks")
        return
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        lines = vcs.ensure_repo(proj)
        check("1a ensure_repo: first call reports the init line",
              lines == ["[git] initialized repository"], f"lines={lines!r}")
        check("1b ensure_repo: .git created", (proj / ".git").is_dir())
        gitignore = proj / ".gitignore"
        check("1c ensure_repo: .gitignore text equals vcs.GITIGNORE",
              gitignore.is_file() and gitignore.read_text(encoding="utf-8") == vcs.GITIGNORE,
              f"exists={gitignore.is_file()}")
        before = gitignore.read_bytes()
        lines2 = vcs.ensure_repo(proj)
        check("1d ensure_repo: second call is idempotent ([])",
              lines2 == [], f"lines={lines2!r}")
        check("1e ensure_repo: .gitignore byte-identical after the second call",
              gitignore.read_bytes() == before)


def case_2_commit() -> None:
    """commit() fires only for a dirty tree inside an enabled repository:
    dirty -> sha + console line + git-log entry; clean tree, git_commits
    false, and a non-repo directory are all silent None no-ops (the
    non-repo check holds with or without a git binary, so it always runs)."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        if not vcs.available():
            print("note: git not found on PATH; skipping the repo commit checks")
        else:
            vcs.ensure_repo(proj)
            (proj / "hello.txt").write_text("alpha\n", encoding="utf-8", newline="\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sha = vcs.commit(proj, "test: alpha")
            out = buf.getvalue()
            check("2a commit: dirty tree returns a truthy short sha",
                  bool(sha), f"sha={sha!r}")
            check("2b commit: prints [git] committed + subject",
                  "[git] committed" in out and "test: alpha" in out, f"out={out!r}")
            rc, subjects = git_log_subjects(proj)
            check("2c commit: subject listed in git log",
                  rc == 0 and "test: alpha" in subjects, f"rc={rc} subjects={subjects!r}")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sha2 = vcs.commit(proj, "test: nothing to do")
            check("2d commit: clean tree -> silent None (nothing printed)",
                  sha2 is None and buf.getvalue() == "",
                  f"sha={sha2!r} out={buf.getvalue()!r}")

            (proj / "config.json").write_text(
                json.dumps({"git_commits": False}), encoding="utf-8", newline="\n")
            (proj / "more.txt").write_text("beta\n", encoding="utf-8", newline="\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sha3 = vcs.commit(proj, "test: disabled")
            check("2e commit: git_commits false -> silent None even with a dirty tree",
                  sha3 is None and buf.getvalue() == "",
                  f"sha={sha3!r} out={buf.getvalue()!r}")

    # Non-repository directory: a silent None no-op even with files present
    # (the is_repo gate -- true with or without a git binary on PATH).
    with tempfile.TemporaryDirectory() as td:
        bare = Path(td)
        (bare / "x.txt").write_text("present\n", encoding="utf-8", newline="\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(bare, "test: never")
        check("2f commit: non-repo dir -> silent None even with files present",
              sha is None and buf.getvalue() == "",
              f"sha={sha!r} out={buf.getvalue()!r}")


def case_3_cmd_init() -> None:
    """cmd_init end to end: the finished scaffold lands in exactly one
    'init: scaffold project' commit with the repo + commit reported on
    stdout and git_commits defaulted into the raw config.json; --force
    reinitializes in place, keeping the history as a second commit."""
    if not vcs.available():
        print("note: git not found on PATH; skipping the cmd_init git checks")
        return

    def make_source(proj: Path) -> None:
        (proj / "source").mkdir(parents=True)
        (proj / "source" / "Chapter_001.md").write_text(
            "第一章 灵根\n正文第一行\n", encoding="utf-8", newline="\n")
        (proj / "source" / "Chapter_002.md").write_text(
            "第二章 筑基\n正文第一行\n", encoding="utf-8", newline="\n")

    def init_argv(proj: Path, *extra: str) -> list[str]:
        # Only --title/--author are required; --style auto --skip-profile
        # keeps init off the network (no style LLM call).
        return ["init", "--project", str(proj), "--title", "T", "--author", "A",
                "--style", "auto", "--skip-profile", *extra]

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        make_source(proj)
        ns = translate._build_parser().parse_args(init_argv(proj))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = translate.cmd_init(ns, proj)
        out = buf.getvalue()
        check("3a init: cmd_init exits 0", code == 0, f"code={code} out={out}")
        check("3b init: .git exists", vcs.is_repo(proj))
        rc, subjects = git_log_subjects(proj)
        check("3c init: exactly one commit, 'init: scaffold project'",
              rc == 0 and subjects == ["init: scaffold project"],
              f"rc={rc} subjects={subjects!r}")
        check("3d init: stdout reports the repo and the commit",
              "[git] initialized repository" in out and "[git] committed" in out,
              f"out={out!r}")
        raw = json.loads((proj / "config.json").read_text(encoding="utf-8"))
        check("3e init: raw config.json carries git_commits True",
              raw.get("git_commits") is True, f"git_commits={raw.get('git_commits')!r}")
        check("3f init: config.DEFAULTS has the git_commits default",
              config.DEFAULTS.get("git_commits") is True)

        # --force reinitialize: same project, same repo, history kept and
        # the rewrite labeled as its own commit.
        ns_force = translate._build_parser().parse_args(init_argv(proj, "--force"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code2 = translate.cmd_init(ns_force, proj)
        rc2, subjects2 = git_log_subjects(proj)
        check("3g init: --force keeps history (two commits, reinit labeled)",
              code2 == 0 and rc2 == 0
              and subjects2 == ["init: reinitialize project", "init: scaffold project"],
              f"code={code2} rc={rc2} subjects={subjects2!r}")


def case_4_v003_direct() -> None:
    """v003.migrate on a legacy v2 project: materializes git_commits and
    git-inits the project, without committing and without stamping the
    version (cmd_migrate owns both, after each step). A direct commit on
    the fresh repo proves the post-stamp commit story; a second identical
    v003 run is a quiet byte-level no-op."""
    if not vcs.available():
        print("note: git not found on PATH; skipping the v003 git checks")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "ship"
        src.mkdir()  # no shipped *.md: sync_templates stays silent
        proj = root / "proj"
        proj.mkdir()
        (proj / "config.json").write_text(
            json.dumps({"version": 2, "providers": {}}, indent=2) + "\n",
            encoding="utf-8", newline="\n")

        # v003.migrate takes templates_src as a parameter, but the swap
        # mirrors test_migrate: no code path may silently depend on the
        # real skill assets.
        orig_tpl = translate.TEMPLATES_SRC_DIR
        translate.TEMPLATES_SRC_DIR = src
        try:
            lines = v003.migrate(proj, src)
        finally:
            translate.TEMPLATES_SRC_DIR = orig_tpl
        check("4a v003: materialized the config and initialized the repo",
              any(line.startswith("[ok] config: materialized") for line in lines)
              and "[git] initialized repository" in lines, f"lines={lines!r}")
        check("4b v003: .git and .gitignore created",
              vcs.is_repo(proj) and (proj / ".gitignore").is_file())
        disk = json.loads((proj / "config.json").read_text(encoding="utf-8"))
        check("4c v003: git_commits defaulted to True on disk",
              disk.get("git_commits") is True, f"git_commits={disk.get('git_commits')!r}")
        check("4d v003: the step itself never stamps the version",
              disk.get("version") == 2, f"version={disk.get('version')!r}")

        # cmd_migrate commits after stamping each step; the repo it leaves
        # behind must accept that commit (here fired directly).
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(proj, "migrate: v003 git history")
        check("4e v003: a commit lands on the freshly created repo",
              bool(sha) and "[git] committed" in buf.getvalue(),
              f"sha={sha!r} out={buf.getvalue()!r}")

        cfg_before = (proj / "config.json").read_bytes()
        lines2 = v003.migrate(proj, src)
        check("4f v003: second run is idempotent (empty report, no repo-init line)",
              lines2 == [] and "[git] initialized repository" not in lines2,
              f"lines={lines2!r}")
        check("4g v003: second run leaves config.json bytes unchanged",
              (proj / "config.json").read_bytes() == cfg_before)


def case_5_v003_dry_run() -> None:
    """v003's --dry-run discipline: it reports the pending repo init but
    writes nothing at all -- no .git, no .gitignore, no config rewrite
    (git-independent: the dry-run branch only ever calls the pure path
    check is_repo, so this case runs even without a git binary)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "ship"
        src.mkdir()
        proj = root / "proj"
        proj.mkdir()
        (proj / "config.json").write_text(
            json.dumps({"version": 2, "providers": {}}, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        before = (proj / "config.json").read_bytes()

        lines = v003.migrate(proj, src, dry_run=True)
        check("5a v003 dry-run: reports the would-be repo init",
              any("would initialize the repository" in line for line in lines),
              f"lines={lines!r}")
        check("5b v003 dry-run: no .git created", not (proj / ".git").exists())
        check("5c v003 dry-run: no .gitignore created",
              not (proj / ".gitignore").exists())
        check("5d v003 dry-run: config.json bytes unchanged",
              (proj / "config.json").read_bytes() == before)


def case_6_never_raises() -> None:
    """The never-raise contract: with vcs._run swapped for a raiser (the
    file's attribute-swap convention, no unittest.mock), commit() on an
    initialized repo returns None with exactly one "[warn] git failed"
    line on stdout, and ensure_repo() on a fresh dir returns that line as
    its only report -- a vanished binary or dead drive must not abort a
    run. Restore happens in finally."""
    if not vcs.available():
        print("note: git not found on PATH; skipping the never-raise checks")
        return

    def boom(project_dir: Path, argv: list[str]) -> subprocess.CompletedProcess:
        raise OSError("boom")

    orig_run = vcs._run
    try:
        # The repo must exist before the swap: with the raiser active
        # ensure_repo would only report the failure (6c's check) and the
        # commit gates would stop the raise before the guarded body.
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            vcs.ensure_repo(proj)
            vcs._run = boom
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sha = vcs.commit(proj, "test: never lands")
            out = buf.getvalue()
            check("6a commit: _run raising -> None, no exception",
                  sha is None, f"sha={sha!r}")
            check("6b commit: exactly one [warn] git failed line",
                  out == "[warn] git failed: OSError: boom\n", f"out={out!r}")

        with tempfile.TemporaryDirectory() as td:
            fresh = Path(td)
            vcs._run = boom
            lines = vcs.ensure_repo(fresh)
            check("6c ensure_repo: _run raising -> single [warn] line",
                  lines == ["[warn] git failed: OSError: boom"],
                  f"lines={lines!r}")
            check("6d ensure_repo: no .git left by the failed init",
                  not (fresh / ".git").exists())
    finally:
        vcs._run = orig_run


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_ensure_repo()
    case_2_commit()
    case_3_cmd_init()
    case_4_v003_direct()
    case_5_v003_dry_run()
    case_6_never_raises()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
