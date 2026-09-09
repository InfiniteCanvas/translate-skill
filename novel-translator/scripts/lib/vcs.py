"""Project git history: init the repo and commit after every mutating action.

Every command that changes project state calls commit() when it is done, so
`git log` doubles as a free, action-labeled backup of the novel: glossary
growth, chapter translations, review-fix rewrites, migrations. The repo is
created by `init` (and backfilled for existing projects by migrations/v003);
paths that churn without meaning (draft state, llm traces, rebuildable epubs)
are ignored via the .gitignore written here.

Everything degrades quietly: no git binary, no repository (e.g. a test
fixture), or `git_commits: false` in config.json simply turns commit() into a
no-op -- a versioning problem must never break a translation run, mirroring
how epubcheck tolerates a missing docker. All failures surface as one
[warn] line at most.
"""

import json
import shutil
import subprocess
from pathlib import Path

# Ignored inside every scaffolded project: draft/ is transient per-chapter
# state, logs/ churns once per invocation and self-prunes, export/ holds
# epubs rebuildable from translated/, and atomic_write_text leaves brief
# *.tmp siblings.
GITIGNORE = """\
# Transient pipeline state and rebuildable artifacts (skill-managed).
draft/
logs/
export/
*.tmp
"""

# Local-only identity fallback so commits work on machines with no global
# git config; never touches the user's global settings.
GIT_USER_NAME = "novel-translator"
GIT_USER_EMAIL = "novel-translator@localhost"


def available() -> bool:
    """True when a git binary is on PATH."""
    return shutil.which("git") is not None


def is_repo(project_dir: Path) -> bool:
    """True when project_dir is already a git work tree (.git is a directory,
    or a file for worktree/submodule checkouts)."""
    return (Path(project_dir) / ".git").exists()


def _run(project_dir: Path, argv: list[str]) -> subprocess.CompletedProcess:
    """git argv inside the project repo; captures output as UTF-8 text."""
    return subprocess.run(
        ["git", *argv], cwd=project_dir, capture_output=True, check=False,
        encoding="utf-8", errors="replace",
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
            (Path(project_dir) / "config.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return True
    return bool(raw.get("git_commits", True)) if isinstance(raw, dict) else True


def ensure_repo(project_dir: Path) -> list[str]:
    """Turn project_dir into a git repository if it is not one already.

    Writes .gitignore when missing and sets local-only config so commits work
    everywhere: user.name/user.email when no identity is configured, and
    core.autocrlf=false so the repo stores exactly the LF bytes the skill
    writes (no phantom CRLF diffs on Windows). Returns [ok]/[warn] report
    lines; [] when the repo already existed. Never raises."""
    project_dir = Path(project_dir)
    if not available():
        return ["[warn] git not found; project history disabled"]
    if is_repo(project_dir):
        return []
    try:
        init = _run(project_dir, ["init"])
        if init.returncode != 0:
            return [f"[warn] git init failed: {_first_line(init.stderr)}"]
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
    a repository, config.json disables git_commits, or the working tree is
    already clean -- so call sites fire after every mutation unconditionally.
    On success prints `[git] committed <sha> <subject>` and returns the short
    sha. A failed commit prints one [warn] line and never raises."""
    project_dir = Path(project_dir)
    if not available() or not is_repo(project_dir) or not _enabled(project_dir):
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
