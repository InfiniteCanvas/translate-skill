"""Project git history: init the repo and commit after every mutating action.

Every command that changes project state calls commit() when it is done, so
`git log` doubles as a free, action-labeled backup of the novel: glossary
growth, chapter translations, review-fix rewrites, migrations. The repo is
created by `init` (and backfilled for existing projects by migrations/v003);
paths that churn without meaning (draft state, llm traces, rebuildable epubs)
are ignored via the .gitignore written here.

Everything degrades quietly: no git binary, no repository (e.g. a test
fixture), `git_commits: false` in config.json, or a foreign repository
(a pre-existing repo that is not skill-managed) simply turns commit()
into a no-op -- a versioning problem must never break a translation run,
mirroring how epubcheck tolerates a missing docker. All failures surface as
one [warn] line at most.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

# Ignored inside every scaffolded project: draft/ is transient per-chapter
# state, logs/ churns once per invocation and self-prunes, export/ holds
# epubs rebuildable from translated/, covers/ holds the generated cover art
# (rebuilt whenever the image is missing), and atomic_write_text leaves
# brief *.tmp siblings.
GITIGNORE = """\
# Transient pipeline state and rebuildable artifacts (skill-managed).
draft/
logs/
export/
covers/
*.tmp
"""

# Written into .git/ by ensure_repo() when it CREATES the repository; commit()
# treats a repo as skill-managed on this marker's presence, or on a .gitignore
# carrying the skill's own rules (repos created before the marker existed).
# Without either, the repo is the user's own and must never see `git add -A`.
MARKER_NAME = "novel-translator-managed"

# The ignore rules every version of this skill has written. A .gitignore that
# carries all of them, and nothing but them, is one of ours from some version
# -- the marker cannot vouch for repositories created before it existed.
_LEGACY_CORE = frozenset({"draft/", "logs/", "export/", "*.tmp"})

# Local-only identity fallback so commits work on machines with no global
# git config; never touches the user's global settings.
GIT_USER_NAME = "novel-translator"
GIT_USER_EMAIL = "novel-translator@localhost"

# Foreign-repo warnings are deduped per directory: commit() fires after every
# mutation, so without this a run inside a foreign repo would warn per commit.
# Keys are normcase()d so Windows path casing (D:\Proj vs d:\proj) is one key.
_FOREIGN_REPO_WARNED: set[str] = set()


def available() -> bool:
    """True when a git binary is on PATH."""
    return shutil.which("git") is not None


def is_repo(project_dir: Path) -> bool:
    """True when project_dir is already a git work tree (.git is a directory,
    or a file for worktree/submodule checkouts)."""
    return (Path(project_dir) / ".git").exists()


def _run(project_dir: Path, argv: list[str]) -> subprocess.CompletedProcess:
    """git argv inside the project repo; captures output as UTF-8 text.

    Bounded at 300s with stdin detached (DEVNULL -- a credential prompt or a
    sick filesystem must not hang the run): on timeout a synthetic failed
    result is returned, matching the CompletedProcess contract every caller
    consumes (.returncode/.stdout/.stderr)."""
    try:
        return subprocess.run(
            ["git", *argv], cwd=project_dir, capture_output=True, check=False,
            encoding="utf-8", errors="replace", timeout=300,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", *argv], 124, stdout="",
            stderr=f"git command timed out after 300s: git {' '.join(argv)}",
        )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _enabled(project_dir: Path) -> bool:
    """The raw config.json git_commits flag, defaulting to True (a missing or
    unreadable config only exists outside an initialized project, where the
    is_repo gate has already stopped us)."""
    try:
        raw = json.loads(
            (Path(project_dir) / "config.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return True
    return bool(raw.get("git_commits", True)) if isinstance(raw, dict) else True


def _managed(project_dir: Path) -> bool:
    """True when the repository may receive skill commits: either ensure_repo()
    left the managed marker in .git/ (every repo the skill creates itself), or
    the .gitignore carries the skill's rules -- the fingerprint of legacy skill
    repos created before the marker existed. Read after stripping a BOM and
    normalizing CRLF.

    The legacy test accepts HISTORICAL forms of our own ignore file, not just
    today's: the marker exists only in repos this version created, and every
    repository from an older version holds the rules that version wrote, so an
    equality test against GITIGNORE would read each of them as a stranger's
    repository and silently end its history. A file qualifies when it holds
    none but our rules and still carries the original core -- a file with any
    foreign rule, or missing a core rule, is the user's own and `git add -A`
    must not touch their repository."""
    project_dir = Path(project_dir)
    if (project_dir / ".git" / MARKER_NAME).is_file():
        return True
    gitignore = project_dir / ".gitignore"
    if not gitignore.is_file():
        return False
    text = gitignore.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    have = {line.strip() for line in text.split("\n")} - {""}
    ours = {line.strip() for line in GITIGNORE.strip().split("\n")} - {""}
    return _LEGACY_CORE <= have and have <= ours


def ensure_repo(project_dir: Path) -> list[str]:
    """Turn project_dir into a git repository if it is not one already.

    Writes .gitignore when missing, drops the skill-managed marker into .git/
    (commit() only manages repos carrying it), and sets local-only config so
    commits work everywhere: user.name/user.email when no identity is
    configured, and core.autocrlf=false so the repo stores exactly the LF
    bytes the skill writes (no phantom CRLF diffs on Windows). Returns
    [ok]/[warn] report lines; [] when the repo already existed. Never raises."""
    project_dir = Path(project_dir)
    if not available():
        return ["[warn] git not found; project history disabled"]
    if is_repo(project_dir):
        return []
    try:
        init = _run(project_dir, ["init"])
        if init.returncode != 0:
            return [f"[warn] git init failed: {_first_line(init.stderr)}"]
        # Identity, not content: this marker (not the .gitignore) is what
        # later tells commit() the skill created this repository -- an
        # already-existing .gitignore of the user's must not be overwritten,
        # and must not make the repo ours either.
        (project_dir / ".git" / MARKER_NAME).write_text(
            "managed by novel-translator\n", encoding="utf-8", newline="\n")
        gitignore = project_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE, encoding="utf-8", newline="\n")
        email = _run(project_dir, ["config", "user.email"])
        if email.returncode != 0 or not email.stdout.strip():
            _run(project_dir, ["config", "user.name", GIT_USER_NAME])
            _run(project_dir, ["config", "user.email", GIT_USER_EMAIL])
        autocrlf = _run(project_dir, ["config", "core.autocrlf"])
        if autocrlf.returncode != 0 or not autocrlf.stdout.strip():
            _run(project_dir, ["config", "core.autocrlf", "false"])
        return ["[git] initialized repository"]
    except Exception as exc:  # noqa: BLE001 - versioning must never break a run
        return [f"[warn] git failed: {type(exc).__name__}: {exc}"]


def commit(project_dir: Path, subject: str) -> str | None:
    """Stage every change and commit it as `subject`; print on progress.

    Silent no-op (returns None) when git is unavailable, the directory is not
    a repository, config.json disables git_commits, the repository is foreign
    (not skill-managed: no managed marker and no skill .gitignore), or the
    working tree is already clean -- so call sites fire after every mutation
    unconditionally. On success prints `[git] committed <sha> <subject>` and
    returns the short sha. A failed commit prints one [warn] line and never
    raises."""
    project_dir = Path(project_dir)
    if not available() or not is_repo(project_dir) or not _enabled(project_dir):
        return None
    # A repository that is not skill-managed (neither the ensure_repo marker
    # nor the skill .gitignore) is the user's own repository, and `git add -A`
    # would sweep their pending changes into a skill-labeled commit.
    if not _managed(project_dir):
        key = os.path.normcase(str(project_dir))
        if key not in _FOREIGN_REPO_WARNED:
            _FOREIGN_REPO_WARNED.add(key)
            print(
                f"[warn] git: skipping commits - {project_dir} looks like a "
                "foreign repository (no skill .gitignore); add the skill "
                ".gitignore to let the skill manage it, or set "
                "git_commits: false"
            )
        return None
    try:
        added = _run(project_dir, ["add", "-A"])
        if added.returncode != 0:
            print(f"[warn] git add failed: {_first_line(added.stderr)}")
            return None
        status = _run(project_dir, ["status", "--porcelain"])
        if status.returncode != 0 or not status.stdout.strip():
            return None
        done = _run(project_dir, ["commit", "-m", subject])
        if done.returncode != 0:
            print(f"[warn] git commit failed: {_first_line(done.stderr)}")
            return None
        head = _run(project_dir, ["rev-parse", "--short", "HEAD"])
        sha = head.stdout.strip() if head.returncode == 0 else ""
        print(f"[git] committed {sha} {subject}".rstrip())
        return sha or None
    except Exception as exc:  # noqa: BLE001 - versioning must never break a run
        print(f"[warn] git failed: {type(exc).__name__}: {exc}")
        return None
