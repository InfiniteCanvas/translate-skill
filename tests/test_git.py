"""Tests for project git history: lib/vcs, init's repo scaffold, v003.

Covers vcs.ensure_repo (an empty directory -> ["[git] initialized
repository"] plus a .git directory and a .gitignore whose text equals
vcs.GITIGNORE; a second call returns [] and leaves .gitignore
byte-identical), vcs.commit (a dirty tree returns a truthy short sha and
prints "[git] committed <sha> <subject>", with the subject landing in
`git log --format=%s`; a clean tree, a raw config.json {"git_commits":
false}, and a non-repository directory are all silent None no-ops), the
foreign-repository guard (a git repo that is NOT skill-managed is the
user's own repository: managed iff the .git/novel-translator-managed marker
exists -- written by ensure_repo when it CREATES the repo -- or the
.gitignore equals GITIGNORE, the fingerprint of legacy skill repos; a repo
without either is foreign: commit() returns None printing
exactly one "[warn] git: skipping commits - ... foreign repository ..."
line -- the module-level dedup set makes the second call on the same
directory fully silent -- and `git add -A` never runs: the dirty file
stays untracked and no commit lands; run after every earlier case so the
per-process warn dedup starts empty; a different-content .gitignore with no
marker is still foreign, a strict subset of the skill rules DROPPING core
entries (only logs/) is still foreign, a byte-equal .gitignore with no
marker commits as
legacy, and the marker wins over a foreign .gitignore), the same guard under path casing
(the dedup key is os.path.normcase()d, so the same directory passed with
different letter casing to two commit() calls still warns exactly once --
skipped with a note on case-sensitive filesystems), _run's timeout contract
(with subprocess on the vcs module swapped for a scripted fake raising
TimeoutExpired, _run returns the synthetic failed result -- returncode 124,
empty stdout, stderr naming the timeout and the exact argv -- after passing
timeout=300 and a DEVNULL stdin, and commit() on a managed repo surfaces it
as the usual one-line add-failure warn), cmd_init end to end (a scaffolded
project gets exactly two
"init: scaffold project" commits -- the birth record of the bare source
chapters, then the backfilled frontmatter/manifest/cover state -- plus
"[git] initialized repository" and "[git] committed" on stdout, and raw
config.json carries
git_commits: true -- and a --force reinitialization keeps the history,
committing on top as "init: reinitialize project" (the fresh
novel_info.json rewrite drops the placeholder flag the first init
recorded; the planted story_state.json is deleted before any commit and
was never tracked, so its removal is invisible to git; nothing else
tracked changes after that commit, so the trailing reseed is a silent
no-op -- re-scrape a chapter bare and it lands under the distinct
"init: reseed after reinitialize" subject)), v003.migrate called
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
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
    reports the init line and writes .git + .gitignore + the skill-managed
    marker; a second call is a quiet [] that leaves .gitignore
    byte-identical."""
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
        check("1d ensure_repo: skill-managed marker written into .git",
              (proj / ".git" / vcs.MARKER_NAME).is_file(),
              f"marker={(proj / '.git' / vcs.MARKER_NAME).is_file()}")
        check("1e ensure_repo: .gitignore ignores covers/",
              "covers/" in gitignore.read_text(encoding="utf-8"),
              f"text={gitignore.read_text(encoding='utf-8')!r}")
        before = gitignore.read_bytes()
        lines2 = vcs.ensure_repo(proj)
        check("1f ensure_repo: second call is idempotent ([])",
              lines2 == [], f"lines={lines2!r}")
        check("1g ensure_repo: .gitignore byte-identical after the second call",
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
    """cmd_init end to end: the scaffold lands in two commits -- the birth
    record 'init: scaffold project' capturing the BARE source chapters (fired
    right after ensure_repo, before any chapter rewrite), then
    'init: backfill and seed' for the finished state (backfilled frontmatter,
    rebuilt manifest, cover) -- with the repo + commit reported on stdout and
    git_commits
    defaulted into the raw config.json; --force reinitializes in place,
    keeping the history with 'init: reinitialize project' on top (the fresh
    novel_info.json rewrite drops the placeholder flag the first init
    recorded; the planted story_state.json is deleted before any commit and
    was never tracked, so its removal is invisible to git -- nothing else
    tracked changes after that commit, so the trailing reseed stays a
    silent no-op), and a re-scraped bare chapter makes that trailing commit
    land under the distinct 'init: reseed after reinitialize' subject."""
    if not vcs.available():
        print("note: git not found on PATH; skipping the cmd_init git checks")
        return

    def make_source(proj: Path) -> None:
        (proj / "source").mkdir(parents=True)
        (proj / "source" / "CHAPTER_0001.md").write_text(
            "第一章 灵根\n正文第一行\n", encoding="utf-8", newline="\n")
        (proj / "source" / "CHAPTER_0002.md").write_text(
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
        check("3c init: two staged commits, scaffold then backfill/seed",
              rc == 0 and subjects == ["init: backfill and seed",
                                       "init: scaffold project"],
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
        # the rewrite labeled as its own commit. A planted story_state.json
        # must not survive -- the reset wipes it with glossary/history.
        (proj / "story_state.json").write_text(
            '{"chapters": {"CHAPTER_001": {"recap": "stale recap"}}}\n',
            encoding="utf-8", newline="\n")
        ns_force = translate._build_parser().parse_args(init_argv(proj, "--force"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code2 = translate.cmd_init(ns_force, proj)
        rc2, subjects2 = git_log_subjects(proj)
        check("3g init: --force keeps history (reinit commits on top)",
              code2 == 0 and rc2 == 0
              and subjects2 == ["init: reinitialize project",
                                "init: backfill and seed",
                                "init: scaffold project"],
              f"code={code2} rc={rc2} subjects={subjects2!r}")
        check("3h init: --force deletes story_state.json",
              not (proj / "story_state.json").exists(), "")

        # The re-init above landed only ONE new commit: after the reinit
        # commit nothing tracked changes (the chapters are already
        # backfilled and re-seeding restores the same glossary bytes), so
        # the trailing reseed commit is a silent no-op. Make the trailing
        # diff REAL by re-scraping a chapter bare -- its backfill lands
        # under the distinct 'init: reseed after reinitialize' subject,
        # keeping the two re-init commits from sharing one subject.
        (proj / "source" / "CHAPTER_0001.md").write_text(
            "第一章 灵根\n正文第一行\n", encoding="utf-8", newline="\n")
        ns_force2 = translate._build_parser().parse_args(init_argv(proj, "--force"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code3 = translate.cmd_init(ns_force2, proj)
        rc3, subjects3 = git_log_subjects(proj)
        check("3i init: a landing reseed commit carries its own subject",
              code3 == 0 and rc3 == 0
              and subjects3 == ["init: reseed after reinitialize",
                                "init: reinitialize project",
                                "init: reinitialize project",
                                "init: backfill and seed",
                                "init: scaffold project"],
              f"code={code3} rc={rc3} subjects={subjects3!r}")


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


def case_7_foreign_repo() -> None:
    """The foreign-repo guard is an identity check. A repo with neither the
    managed marker nor the skill .gitignore is FOREIGN (the user's own repo):
    commit() is a None no-op printing exactly one warn naming it -- the
    module-level dedup set makes a second call on the same directory fully
    silent (it persists for the process, so this case runs after every
    earlier one used skill-managed repos only) -- and `git add -A` never
    runs: the user's pending file stays untracked and no commit lands. A
    different-content .gitignore without a marker is foreign by the same
    token (existence is not identity); a .gitignore byte-equal GITIGNORE
    without a marker is a legacy skill repo and commits; a strict subset of
    the skill rules DROPPING core entries (only logs/) is foreign too -- the
    legacy test requires the whole core, not mere containment in ours; and
    a marker wins over a foreign .gitignore."""
    if not vcs.available():
        print("note: git not found on PATH; skipping the foreign-repo checks")
        return

    def init_plain(proj: Path) -> bool:
        """git init DIRECTLY: ensure_repo() would write the skill .gitignore
        and the marker, turning the directory into a skill-managed repo."""
        init = subprocess.run(
            ["git", "init"], cwd=str(proj), capture_output=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if init.returncode != 0:
            check("7 setup: git init succeeded", False,
                  f"rc={init.returncode} err={init.stderr!r}")
            return False
        return True

    def set_local_identity(proj: Path) -> None:
        """Local-only identity so the legacy/marked repos can commit without
        a global git config (ensure_repo would normally do this)."""
        for argv in (["config", "user.name", "legacy"],
                     ["config", "user.email", "legacy@localhost"]):
            subprocess.run(["git", *argv], cwd=str(proj), capture_output=True,
                           check=False)

    with tempfile.TemporaryDirectory() as td:
        foreign = Path(td)
        if not init_plain(foreign):
            return
        (foreign / "pending.txt").write_text(
            "the user's uncommitted work\n", encoding="utf-8", newline="\n")
        check("7 setup: repo present, no .gitignore at the project dir",
              vcs.is_repo(foreign) and not (foreign / ".gitignore").exists(),
              f"gitignore={(foreign / '.gitignore').exists()}")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(foreign, "test: foreign repo")
        out = buf.getvalue()
        check("7a foreign: commit() -> None (a skill commit must never sweep "
              "the user's repository)",
              sha is None, f"sha={sha!r}")
        check("7b foreign: exactly one line, the git-skipping foreign-repo warn",
              out.count("[warn] git: skipping commits") == 1
              and "foreign repository" in out and out.count("\n") == 1,
              f"out={out!r}")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(foreign, "test: foreign repo again")
        check("7c foreign: second call on the same dir is silent (warn deduped)",
              sha is None and buf.getvalue() == "",
              f"sha={sha!r} out={buf.getvalue()!r}")

        _rc, subjects = git_log_subjects(foreign)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(foreign),
            capture_output=True, encoding="utf-8", errors="replace",
            check=False,
        )
        check("7d foreign: nothing ever committed (log empty)",
              subjects == [], f"subjects={subjects!r}")
        check("7e foreign: the user's pending file was never staged away",
              status.returncode == 0 and "?? pending.txt" in status.stdout,
              f"rc={status.returncode} out={status.stdout!r}")

    # A .gitignore with DIFFERENT content and no marker is still foreign:
    # the guard compares content, it does not settle for any .gitignore.
    with tempfile.TemporaryDirectory() as td:
        mixed = Path(td)
        if not init_plain(mixed):
            return
        (mixed / ".gitignore").write_text("node_modules/\n*.log\n",
                                          encoding="utf-8", newline="\n")
        (mixed / "pending.txt").write_text(
            "the user's uncommitted work\n", encoding="utf-8", newline="\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(mixed, "test: foreign by gitignore content")
        out = buf.getvalue()
        check("7f mixed: different .gitignore, no marker -> None",
              sha is None, f"sha={sha!r}")
        check("7g mixed: exactly one foreign-repo warn",
              out.count("[warn] git: skipping commits") == 1
              and "foreign repository" in out and out.count("\n") == 1,
              f"out={out!r}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(mixed, "test: foreign, other casing")
        check("7h mixed: second call silent (warn deduped)",
              sha is None and buf.getvalue() == "",
              f"sha={sha!r} out={buf.getvalue()!r}")
        _rc, subjects = git_log_subjects(mixed)
        check("7i mixed: nothing ever committed", subjects == [],
              f"subjects={subjects!r}")

    # Legacy skill repo: .gitignore byte-equal GITIGNORE, no marker (repos
    # created before the marker existed) -> managed, commits land.
    with tempfile.TemporaryDirectory() as td:
        legacy = Path(td)
        if not init_plain(legacy):
            return
        (legacy / ".gitignore").write_text(vcs.GITIGNORE, encoding="utf-8",
                                           newline="\n")
        set_local_identity(legacy)
        (legacy / "pending.txt").write_text(
            "legacy project state\n", encoding="utf-8", newline="\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(legacy, "test: legacy managed repo")
        out = buf.getvalue()
        check("7j legacy: byte-equal .gitignore, no marker -> commits",
              bool(sha) and "[git] committed" in out, f"sha={sha!r} out={out!r}")
        _rc, subjects = git_log_subjects(legacy)
        check("7k legacy: subject listed in git log",
              _rc == 0 and "test: legacy managed repo" in subjects,
              f"rc={_rc} subjects={subjects!r}")
        check("7l legacy: the user's .gitignore was not rewritten",
              (legacy / ".gitignore").read_text(encoding="utf-8") == vcs.GITIGNORE,
              "")

    # A repo created by an OLDER version carries the .gitignore that version
    # wrote: every rule it had, but not the ones added since (covers/). The
    # legacy test is a SUBSET check for exactly this shape -- an equality
    # test would read every pre-marker project as a stranger's repository
    # and silently end its history.
    with tempfile.TemporaryDirectory() as td:
        older = Path(td)
        if not init_plain(older):
            return
        historical = "".join(
            line + "\n" for line in vcs.GITIGNORE.strip().split("\n")
            if not line.strip().startswith("#") and line.strip() != "covers/"
        )
        (older / ".gitignore").write_text(historical, encoding="utf-8", newline="\n")
        set_local_identity(older)
        (older / "pending.txt").write_text(
            "pre-marker project state\n", encoding="utf-8", newline="\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(older, "test: pre-marker repo")
        out = buf.getvalue()
        check("7o legacy-historical: .gitignore missing later rules still managed",
              bool(sha) and "[git] committed" in out
              and "foreign repository" not in out,
              f"sha={sha!r} out={out!r}")
        check("7p legacy-historical: .gitignore left untouched",
              (older / ".gitignore").read_text(encoding="utf-8") == historical,
              "")

    # Strict SUBSET of the skill rules DROPPING core entries (only logs/):
    # the legacy test requires the whole core -- containment in ours alone
    # must not read a stranger's minimal .gitignore as skill-managed, or
    # `git add -A` would sweep their repository into a skill commit.
    with tempfile.TemporaryDirectory() as td:
        subset = Path(td)
        if not init_plain(subset):
            return
        (subset / ".gitignore").write_text("logs/\n", encoding="utf-8",
                                           newline="\n")
        (subset / "pending.txt").write_text(
            "the user's uncommitted work\n", encoding="utf-8", newline="\n")
        check("7q subset: logs/-only .gitignore, no marker -> not managed",
              not vcs._managed(subset),
              f"managed={vcs._managed(subset)}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(subset, "test: foreign subset")
        out = buf.getvalue()
        check("7r subset: commit() -> None with exactly one foreign-repo warn",
              sha is None
              and out.count("[warn] git: skipping commits") == 1
              and "foreign repository" in out and out.count("\n") == 1,
              f"sha={sha!r} out={out!r}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(subset, "test: foreign subset again")
        check("7s subset: second call silent (warn deduped)",
              sha is None and buf.getvalue() == "",
              f"sha={sha!r} out={buf.getvalue()!r}")
        _rc, subjects = git_log_subjects(subset)
        check("7t subset: nothing ever committed", subjects == [],
              f"subjects={subjects!r}")

    # Marker wins: a repo ensure_repo() created carries the marker even when
    # the user later replaced .gitignore with their own content.
    with tempfile.TemporaryDirectory() as td:
        marked = Path(td)
        if not init_plain(marked):
            return
        (marked / ".gitignore").write_text("node_modules/\n", encoding="utf-8",
                                           newline="\n")
        (marked / ".git" / vcs.MARKER_NAME).write_text(
            "managed by novel-translator\n", encoding="utf-8", newline="\n")
        set_local_identity(marked)
        (marked / "pending.txt").write_text(
            "project state\n", encoding="utf-8", newline="\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(marked, "test: marker wins")
        out = buf.getvalue()
        check("7m marked: marker present despite foreign .gitignore -> commits",
              bool(sha) and "[git] committed" in out, f"sha={sha!r} out={out!r}")
        check("7n marked: the user's .gitignore content is preserved",
              (marked / ".gitignore").read_text(encoding="utf-8") == "node_modules/\n",
              "")


def case_8_foreign_warn_casing() -> None:
    """The foreign-repo warn dedup survives path-casing differences: the
    same directory passed with different letter casing to two commit()
    calls (D:\\Proj vs d:\\proj on a case-insensitive filesystem) prints
    the warn exactly once across both -- the dedup key is
    os.path.normcase()d. Runs last-ish with its own fresh repo, so the
    module-level dedup set is uncontaminated; skipped with a note when the
    filesystem treats the cased variants as distinct (case-sensitive)."""
    if not vcs.available():
        print("note: git not found on PATH; skipping the warn-casing checks")
        return
    with tempfile.TemporaryDirectory() as td:
        foreign = Path(td) / "ForeignCase"
        foreign.mkdir()
        # git init DIRECTLY: ensure_repo() would write the skill .gitignore
        # and turn the directory into a skill-managed repo.
        init = subprocess.run(
            ["git", "init"], cwd=str(foreign), capture_output=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if init.returncode != 0:
            check("8 setup: git init succeeded", False,
                  f"rc={init.returncode} err={init.stderr!r}")
            return
        (foreign / "pending.txt").write_text(
            "the user's uncommitted work\n", encoding="utf-8", newline="\n")

        alt = foreign.with_name(foreign.name.swapcase())
        if str(alt) == str(foreign) or not (alt / ".git").exists():
            # Case-sensitive filesystem (or an all-digit temp name): the
            # two casings are different directories, so the normcase dedup
            # cannot be observed here.
            print("note: filesystem is case-sensitive; "
                  "skipping the warn-casing checks")
            return

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sha = vcs.commit(foreign, "test: foreign repo")
        out = buf.getvalue()
        check("8a casing: first call -> None with the foreign-repo warn",
              sha is None and out.count("[warn] git: skipping commits") == 1
              and "foreign repository" in out, f"sha={sha!r} out={out!r}")

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            sha2 = vcs.commit(alt, "test: foreign repo, other casing")
        check("8b casing: differently-cased same dir -> silent (warn once)",
              sha2 is None and buf2.getvalue() == "",
              f"sha={sha2!r} out={buf2.getvalue()!r}")
        check("8c casing: exactly one warn across BOTH calls",
              (out + buf2.getvalue()).count("[warn] git: skipping commits") == 1,
              f"combined={out + buf2.getvalue()!r}")
        check("8d casing: dedup key was normcase()d",
              os.path.normcase(str(foreign)) in vcs._FOREIGN_REPO_WARNED
              and str(foreign) not in vcs._FOREIGN_REPO_WARNED,
              f"warned={vcs._FOREIGN_REPO_WARNED!r}")
        _rc, subjects = git_log_subjects(foreign)
        check("8e casing: nothing ever committed",
              subjects == [], f"subjects={subjects!r}")


def case_9_run_timeout() -> None:
    """_run's timeout contract: with subprocess on the vcs module swapped
    for a fake raising TimeoutExpired (the file's attribute-swap convention),
    _run returns the synthetic failed result -- returncode 124, empty
    stdout, stderr 'git command timed out after 300s: git <argv>' -- and the
    real invocation passes timeout=300 and a DEVNULL stdin (a credential
    prompt must never hang the run). commit() on a managed repo surfaces the
    timeout as its usual one-line add-failure warn, not a hang or a raise;
    that half needs a real repo, the pure _run half does not."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    def swap() -> None:
        vcs.subprocess = SimpleNamespace(
            run=fake_run,
            TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess,
            DEVNULL=subprocess.DEVNULL,
        )

    orig_subprocess = vcs.subprocess
    swap()
    try:
        result = vcs._run(Path("nowhere"), ["status", "--porcelain"])
    finally:
        vcs.subprocess = orig_subprocess
    argv, kwargs = calls[-1]
    check("9a _run: invoked as git status --porcelain",
          argv == ["git", "status", "--porcelain"], f"argv={argv!r}")
    check("9b _run: bounded at 300s with DEVNULL stdin",
          kwargs.get("timeout") == 300 and kwargs.get("stdin") is subprocess.DEVNULL,
          f"kwargs={kwargs!r}")
    check("9c _run: timeout -> synthetic returncode 124, empty stdout",
          result.returncode == 124 and result.stdout == "",
          f"result={result!r}")
    check("9d _run: stderr names the timeout and the exact argv",
          result.stderr == "git command timed out after 300s: git status --porcelain",
          f"stderr={result.stderr!r}")

    if not vcs.available():
        print("note: git not found on PATH; skipping the commit-level timeout check")
        return
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        vcs.ensure_repo(proj)  # before the swap: creates repo, marker, config
        (proj / "hello.txt").write_text("alpha\n", encoding="utf-8", newline="\n")
        swap()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sha = vcs.commit(proj, "test: timed out")
            out = buf.getvalue()
        finally:
            vcs.subprocess = orig_subprocess
        check("9e commit: timed-out add -> None, no exception",
              sha is None, f"sha={sha!r}")
        check("9f commit: exactly the add-failed warn naming the timeout",
              out == "[warn] git add failed: git command timed out after "
                     "300s: git add -A\n", f"out={out!r}")


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
    case_7_foreign_repo()
    case_8_foreign_warn_casing()
    case_9_run_timeout()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
