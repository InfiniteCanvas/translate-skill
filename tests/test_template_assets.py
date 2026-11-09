# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0", "openai>=1.60", "anthropic>=1.0"]
# ///
import hashlib
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import pipeline

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


def case_every_shipped_template_records_its_current_revision() -> None:
    """Every shipped template's CURRENT bytes must be one of the revisions
    assets/templates/.history.json records under that template's name.

    Both readers of the history -- pipeline._shipped_revisions, which decides
    whether a project copy falls through to the skill's, and migration v017,
    which deletes it outright -- match on sha256 alone. A maintainer who edits
    a shipped template and forgets to append its new hash leaves the file
    self-classifying as an override: v017 stops deleting the leftover, so the
    project's stale copy keeps winning forever, and nothing anywhere fails.
    That is why AGENTS.md makes an edit to .history.json a migration-grade
    change; this is what keeps that rule honest.

    The templates are enumerated from the directory, so a template added later
    is checked without anything here being updated."""
    templates_dir = pipeline._SKILL_TEMPLATES
    history_path = templates_dir / pipeline.HISTORY_FILE
    shipped = sorted(templates_dir.glob("*.md"))

    check("1a shipped: assets/templates holds .md prompt templates",
          bool(shipped), f"templates_dir={templates_dir}")
    check("1b history: .history.json is present", history_path.is_file(),
          f"path={history_path}")

    try:
        raw = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        check("1c history: .history.json parses as JSON", False, f"{type(exc).__name__}: {exc}")
        return
    check("1c history: .history.json parses as JSON", True)
    check("1d history: .history.json is a mapping of name -> revisions",
          isinstance(raw, dict)
          and all(isinstance(v, list) for v in raw.values()),
          f"type={type(raw).__name__}")

    missing_key: list[str] = []
    unrecorded: list[str] = []
    unread: list[str] = []
    for path in shipped:
        if path.name not in raw:
            missing_key.append(path.name)
            check(f"2-{path.name}: it has an entry in .history.json",
                  False, "no entry under this name")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest not in {str(rev) for rev in raw[path.name]}:
            unrecorded.append(path.name)
        if digest not in pipeline._shipped_revisions(path.name):
            unread.append(path.name)
        check(f"2-{path.name}: its current sha256 is recorded in .history.json",
              digest in {str(rev) for rev in raw[path.name]},
              f"digest={digest}")

    check("3a every shipped template has an entry in .history.json",
          not missing_key, f"missing={missing_key}")
    check("3b no shipped template's current bytes are unrecorded",
          not unrecorded, f"unrecorded={unrecorded}")
    check("3c pipeline._shipped_revisions -- the reader _load_template and "
          "migration v017 both rely on -- agrees for every shipped template",
          not unread, f"unread={unread}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_every_shipped_template_records_its_current_revision()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())