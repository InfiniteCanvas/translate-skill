"""Per-version migration scripts for projects initialized by older skills.

Each sibling vNNN.py module is one project-schema version and exposes:
  VERSION      int, must equal the NNN in its filename
  DESCRIPTION  one-line summary printed while the step is applied
  migrate(project_dir, templates_src, dry_run=False, force=False) -> list[str]
               report lines (with [ok]/[warn] prefixes); never prints itself

chain() discovers the scripts, imports each as a submodule of this package,
validates the VERSION/filename match (a misnamed script must fail loudly at
dispatch time, not silently skip or re-run projects), and returns them in
application order -- sorted by the filename's number, which equals plain
name order under the zero-padded vNNN convention but stays correct if the
padding ever widens past v999. Discovery is a function, not import-time
state, so tests can swap migrations.chain for fake steps; current_version()
resolves chain() through the module-global lookup and follows the swap too.

The chain is never empty in a shipped skill (v001.py exists); an empty
discovery is a packaging bug and current_version() refuses to guess 0.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import ModuleType

_SCRIPT_RE = re.compile(r"^v(\d+)\.py$")
_PACKAGE_DIR = Path(__file__).resolve().parent


def chain() -> list[ModuleType]:
    """All version scripts, imported and validated, in application order."""
    entries: list[tuple[int, Path]] = []
    for path in _PACKAGE_DIR.glob("v*.py"):
        match = _SCRIPT_RE.match(path.name)
        if match is None or not path.is_file():
            continue  # not a version script (common.py, stray directories)
        entries.append((int(match.group(1)), path))
    entries.sort()

    steps: list[ModuleType] = []
    for number, path in entries:
        module = importlib.import_module(f".{path.stem}", __name__)
        version = getattr(module, "VERSION", None)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError(
                f"migrations/{path.name}: VERSION must be an int, got {version!r}"
            )
        if version != number:
            raise ValueError(
                f"migrations/{path.name}: VERSION is {version}, "
                f"but the filename says v{number:03d}"
            )
        if not callable(getattr(module, "migrate", None)):
            raise ValueError(
                f"migrations/{path.name}: missing a migrate(project_dir, "
                "templates_src, dry_run, force) function"
            )
        steps.append(module)
    return steps


def current_version() -> int:
    """The chain's head version (what init stamps into fresh projects)."""
    steps = chain()
    if not steps:
        raise ValueError("migration chain is empty: no migrations/vNNN.py scripts found")
    return steps[-1].VERSION
