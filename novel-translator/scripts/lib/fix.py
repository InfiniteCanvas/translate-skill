"""Post-hoc fixer driver: parse the review report (filename: config
review_report_path, default review-report.md) and run each - Command:
bullet through the skill's CLI as a subprocess. Supports two parser modes:

  1. Explicit: extract every "- Command: <cli-line>" line and shlex.split() it.
  2. Legacy synthesis: when zero explicit Command lines are present, walk the
     finding blocks (### [N] warn|info / kind / source) and synthesize commands
     via review.command_for_finding() -- kind from heading, source from heading,
     suggestion from - Suggestion: bullet, plus the two exact heuristic reason
     templates for variant_to_remove / merge_with.

The driver never parses - Action: prose; the writer decides machine-actionability
through one shared mapping function (review.command_for_finding).

Exit codes follow the CLI convention: 0 ok/no-op, 1 any command failed,
2 usage or setup error (missing report, zero commands parseable or
synthesizable).

report_is_stale() is the staleness half of the report contract: it
compares the writer's glossary_digest frontmatter anchor (see
review.write_report) against the current glossary.json so a report is
never replayed over glossary data that changed after generation.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import balance, glossary, review


_SUPPORTED_VERBS = {"replace", "set", "merge", "retire"}

_NOOP_MARKER = "[glossary] noop:"

_HEURISTIC_DUP_RE = re.compile(r"^(source|variant) '(.+?)' also belongs to entry '(.+?)'$")
_HEURISTIC_VARIANT_RE = re.compile(r"^variant '(.+?)' contains no CJK characters$")

_HEADING_RE = re.compile(r"^### \[(\d+)\] (\w+) / (\w+) / (.+?)\s*$")

_COMMAND_RE = re.compile(r"^- Command: (.+?)\s*$")


class FixError(Exception):
    """Raised by parse_report() when the report carries zero
    machine-applicable commands. cmd_review_fix translates this into
    CLI exit code 2."""


@dataclass
class CommandSpec:
    """A single - Command: line (or synthesized equivalent) ready to run."""
    raw: str
    argv: list[str] = field(default_factory=list)
    line_no: int = 0
    finding: dict = field(default_factory=dict)


def parse_report(path: Path) -> tuple[list[CommandSpec], int]:
    """Parse every - Command: bullet; when none exist, synthesize commands
    from finding blocks via review.command_for_finding().

    Returns (specs, total_findings_count). Raises FixError when zero
    commands are extracted in either mode (so the CLI exits 2 with a
    clear message).
    """
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    findings_count = sum(1 for ln in lines if _HEADING_RE.match(ln))

    specs = _parse_explicit(lines)
    if not specs:
        specs = _parse_legacy_synthesis(lines)

    if not specs:
        raise FixError(
            "report has no machine-applicable commands"
            + (f" ({findings_count} finding(s) found, all need a human decision)"
               if findings_count else "")
        )
    return specs, findings_count


def _report_frontmatter(path: Path) -> dict[str, str]:
    """Flat `key: value` pairs from a report's YAML frontmatter block (the
    first line through the closing `---`), read utf-8-sig like
    parse_report so a BOM cannot hide the opening fence.

    Only top-level scalars are returned -- indented (nested) lines such as
    the `outcome:` children are skipped. Empty dict when the file is
    unreadable or has no frontmatter block."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    frontmatter: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        frontmatter[key.strip()] = value.strip()
    return frontmatter


def report_is_stale(project_dir: Path, report_path: Path) -> bool:
    """True when the report was generated against a glossary.json that has
    since changed: its `glossary_digest` frontmatter field (stamped by
    review.write_report) no longer matches sha256 of the current
    glossary.json bytes, first 12 hex chars.

    cmd_review_fix uses this to refuse replaying suggestions over newer
    data (hand edits, a later --fix). Absent or `null` digest -> False, so
    old or hand-written reports stay runnable -- only writer-generated
    reports carry the field. A missing glossary.json hashes b"" for
    stability; an unreadable report -> False (nothing proves staleness).
    """
    digest = _report_frontmatter(report_path).get("glossary_digest", "")
    if not digest or digest == "null":
        return False
    glossary_path = Path(project_dir) / "glossary.json"
    current = glossary_path.read_bytes() if glossary_path.is_file() else b""
    return hashlib.sha256(current).hexdigest()[:12] != digest


def _parse_explicit(lines: list[str]) -> list[CommandSpec]:
    """Pull every - Command: bullet whose argv starts with a supported
    glossary verb. Other lines (incl. the legacy header bullet
    "- Command: `review glossary`") are ignored with a console warning."""
    specs: list[CommandSpec] = []
    for idx, line in enumerate(lines, 1):
        m = _COMMAND_RE.match(line)
        if not m:
            continue
        raw = m.group(1).strip()
        try:
            argv = shlex.split(raw, posix=True)
        except ValueError as exc:
            print(f"[review fix] skipped line {idx}: malformed quoting - {exc}")
            continue
        if len(argv) < 2 or argv[0] != "glossary" or argv[1] not in _SUPPORTED_VERBS:
            print(
                f"[review fix] skipped line {idx}: unsupported verb"
                f" (expected glossary <{'|'.join(sorted(_SUPPORTED_VERBS))}>)"
            )
            continue
        specs.append(CommandSpec(raw=raw, argv=argv, line_no=idx, finding={}))
    return specs


def _parse_legacy_synthesis(lines: list[str]) -> list[CommandSpec]:
    """Walk finding blocks and synthesize commands through the same
    mapping the writer uses. Heuristic structured fields are recovered
    by regexing the two exact reason templates -- never by parsing
    model prose."""
    specs: list[CommandSpec] = []
    current: dict | None = None

    def _finalize(block: dict | None) -> None:
        if block is None:
            return
        spec = review.command_for_finding(block)
        if spec is None:
            return
        argv = review._command_argv(spec)
        raw = " ".join(shlex.quote(t) for t in argv)
        specs.append(CommandSpec(
            raw=raw,
            argv=argv,
            line_no=0,
            finding=dict(block),
        ))

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            _finalize(current)
            current = {
                "index": int(m.group(1)),
                "severity": m.group(2),
                "kind": m.group(3),
                "source": m.group(4),
                "reason": "",
                "suggestion": "",
                "origin": "model",
                "variant_to_remove": None,
                "merge_with": None,
            }
            continue
        if current is None:
            continue
        if line.startswith("- Reason:"):
            current["reason"] = line[len("- Reason:"):].strip()
            dup = _HEURISTIC_DUP_RE.match(current["reason"])
            if dup and current["kind"] == "duplicate":
                current["merge_with"] = dup.group(3)
            var = _HEURISTIC_VARIANT_RE.match(current["reason"])
            if var and current["kind"] == "variant":
                current["variant_to_remove"] = var.group(1)
        elif line.startswith("- Suggestion:"):
            current["suggestion"] = line[len("- Suggestion:"):].strip()
        elif line.startswith("- Tier:"):
            tier = line[len("- Tier:"):].strip()
            current["origin"] = tier or "model"
    _finalize(current)
    return specs


def _argv_value(argv: list[str], flag: str) -> str | None:
    """Value of a `--flag X` / `--flag=X` option anywhere in argv, else None
    (both forms are argparse-legal and report writers may use either)."""
    for i, token in enumerate(argv):
        if token == flag:
            return argv[i + 1] if i + 1 < len(argv) else None
        if token.startswith(flag + "="):
            return token[len(flag) + 1:]
    return None


def invalid_translation_reason(argv: list[str], g: dict) -> str | None:
    """Why this `glossary replace` / `glossary set` argv would write a
    source-script suggestion into a CJK entry's translation, else None.

    review.apply_fixes() already refuses such suggestions ("suggestion not
    in target language"), but `review fix` shells the report's commands
    out to the CLI where nothing re-checked them -- a wrong-language model
    suggestion would land in glossary.json and be rewritten across every
    translated chapter. Mirrors the review check (CJK source AND CJK
    suggested value) on balance.is_cjk so every module shares one range.

    None (guard not applicable) when: not a replace/set command, no
    --translation flag (definitions/categories may legitimately quote
    source text), no --source, or no matching entry -- the subprocess
    reports those cases itself.
    """
    if argv[:2] not in (["glossary", "replace"], ["glossary", "set"]):
        return None
    value = _argv_value(argv, "--translation")
    if value is None:
        return None
    value = value.strip()
    if not value:
        return "empty translation"
    source = _argv_value(argv, "--source")
    if not source:
        return None
    entry = glossary.find(g, source)
    if entry is None:
        return None
    entry_source = entry.get("source")
    if (
        isinstance(entry_source, str)
        and balance.is_cjk(entry_source)
        and balance.is_cjk(value)
    ):
        return "suggestion not in target language"
    return None


def _invalid_category_reason(argv: list[str]) -> str | None:
    """Why this `glossary set` argv would exit 2 on an off-vocabulary
    --category, else None.

    parse_report's explicit mode only checks the verb allowlist, so a
    hand-edited Command bullet can carry any category string -- the child
    would reject it (glossary.py's CATEGORIES check), count as failed, and
    under --exit-on-error abort every later finding. The model-driven path
    (command_for_finding) already refuses off-vocabulary categories, so
    only the hand-edit vector needs guarding. Mirrors glossary.py's
    validation: case-sensitive membership, value stripped first. None for
    other verbs (only `set` accepts --category), a missing flag, or a
    blank value -- the subprocess reports those itself."""
    if argv[:2] != ["glossary", "set"]:
        return None
    value = (_argv_value(argv, "--category") or "").strip()
    if value and value not in glossary.CATEGORIES:
        return (
            f"unknown category '{value}' "
            f"(must be one of: {', '.join(glossary.CATEGORIES)})"
        )
    return None


def static_skip_reason(argv: list[str], g: dict | None) -> str | None:
    """Static per-spec skip reason applied before any subprocess runs:
    invalid_translation_reason (needs a readable glossary) plus the
    --category vocabulary check (glossary-independent, so it also fires
    when glossary.json is unreadable)."""
    if g is not None:
        reason = invalid_translation_reason(argv, g)
        if reason is not None:
            return reason
    return _invalid_category_reason(argv)


def conflict_keys(argv: list[str], g: dict | None) -> set[tuple]:
    """Public view of _conflict_keys for dry-run listings: the resolved
    key set of one command, or empty when the glossary is unreadable
    (None) or the command writes nothing the conflict guard tracks."""
    if g is None:
        return set()
    return _conflict_keys(argv, g)


def _resolve_source(g: dict, value: str | None) -> str | None:
    """Canonical source for a user-supplied lookup value: the matched
    entry's source when glossary.find() resolves it (source or variants),
    else the literal value itself (no entry yet -- the subprocess reports
    that). None only when value is None."""
    if value is None:
        return None
    entry = glossary.find(g, value)
    if entry is not None:
        source = entry.get("source")
        if isinstance(source, str) and source:
            return source
    return value


_SET_FIELD_FLAGS = ("--definition", "--category", "--translation")


def _conflict_keys(argv: list[str], g: dict) -> set[tuple]:
    """(edited field, resolved source) identities of one Command spec -- the
    SET of fields it writes, empty when the command has no glossary target
    the conflict guard tracks.

    Keys name the FIELDS a command writes, not the verb spelling it:
    `replace` edits the translation, so it shares `set --translation`'s key
    and the two collide -- two commands writing one field of one entry is
    the exact pair that double-applies (the second rewriting every chapter
    back). A multi-field `set` occupies EVERY field key it writes -- keying
    only the first present flag let `set --translation B --definition D`
    slip past a queued translation edit and rewrite the field back, so a
    spec conflicts when ANY of its keys is already queued. Variant/alt
    edits name their own string and stay untracked. merge keys the resolved
    (--keep, --remove) pair -- one report legitimately emits two merges into
    one keeper. retire keys the resolved source. Sources resolve through
    glossary.find() so a command naming a variant and one naming the canonical
    source collide, mirroring the in-review path's skip on (entry, field)."""
    if len(argv) < 2 or argv[0] != "glossary":
        return set()
    verb = argv[1]
    if verb == "replace":
        source = _resolve_source(g, _argv_value(argv, "--source"))
        if source is None:
            return set()
        return {("set:translation", source)}
    if verb == "retire":
        source = _resolve_source(g, _argv_value(argv, "--source"))
        if source is None:
            return set()
        return {("retire", source)}
    if verb == "set":
        source = _resolve_source(g, _argv_value(argv, "--source"))
        if source is None:
            return set()
        return {
            (f"set:{flag[2:]}", source)
            for flag in _SET_FIELD_FLAGS
            if _argv_value(argv, flag) is not None
        }
    if verb == "merge":
        keep = _resolve_source(g, _argv_value(argv, "--keep"))
        remove = _resolve_source(g, _argv_value(argv, "--remove"))
        if keep is None or remove is None:
            return set()
        return {("merge", keep, remove)}
    return set()


def run_commands(
    project_dir: Path,
    script_path: Path,
    specs: Iterable[CommandSpec],
    *,
    exit_on_error: bool = False,
) -> dict:
    """Execute each spec via the skill's CLI as a subprocess.

    Appends --no-build to every `glossary replace` argv when absent so the
    executor can run a single batch-wide epub build at the end. Captures
    stdout/stderr per call, returns counts. Every spec is first checked
    against a freshly loaded glossary so wrong-language suggestions and
    off-vocabulary categories are skipped in-process (see
    static_skip_reason), and its per-written-field key set against the keys
    already queued in THIS call so a second command for the same target is
    skipped as a conflict -- the in-review path (review.apply_fixes) refuses
    same-(entry, field) duplicates the same way (see _conflict_keys).
    """
    specs = list(specs)
    applied = 0
    noop = 0
    failed = 0
    specs_run = 0
    skipped_invalid = 0
    skipped_conflict = 0
    seen_keys: set[tuple] = set()
    changed_chapters = False

    for i, spec in enumerate(specs, 1):
        if any(
            tok == "--project" or tok.startswith("--project=")
            for tok in spec.argv
        ):
            skipped_invalid += 1
            print(
                f"[review fix] skipped [{i}]: command overrides --project"
                f" ({' '.join(shlex.quote(t) for t in spec.argv)})"
            )
            continue
        try:
            g = glossary.load(project_dir)
        except ValueError:
            g = None
        reason = static_skip_reason(spec.argv, g)
        if reason is not None:
            skipped_invalid += 1
            print(
                f"[review fix] skipped [{i}]: {reason}"
                f" ({' '.join(shlex.quote(t) for t in spec.argv)})"
            )
            continue
        keys = conflict_keys(spec.argv, g)
        if keys and not keys.isdisjoint(seen_keys):
            skipped_conflict += 1
            print(
                f"[review fix] skipped [{i}]: conflicting command for "
                f"'{sorted(keys)[0][1]}' (already queued)"
            )
            continue
        seen_keys |= keys
        argv = _prepare_argv(spec.argv)
        full_argv = [
            sys.executable, str(script_path),
            "--project", str(project_dir),
            *argv,
        ]
        try:
            proc = subprocess.run(
                full_argv, capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace", timeout=1800,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired:
            specs_run += 1
            failed += 1
            print(
                "[review fix] command timed out after 1800s: "
                + " ".join(shlex.quote(t) for t in argv)
            )
            if exit_on_error:
                break
            continue
        specs_run += 1
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            failed += 1
            print(f"[review fix] failed at [{i}]: {' '.join(shlex.quote(t) for t in argv)}")
            if out.strip():
                tail = out.strip().splitlines()[-1]
                print(f"[review fix] {tail}")
            if exit_on_error:
                break
            continue
        if _looks_like_noop(out):
            noop += 1
        else:
            applied += 1
        if argv[:2] == ["glossary", "replace"] and _signals_chapter_change(out):
            changed_chapters = True

    return {
        "applied": applied,
        "noop": noop,
        "failed": failed,
        "changed_chapters": changed_chapters,
        "specs_run": specs_run,
        "skipped_invalid": skipped_invalid,
        "skipped_conflict": skipped_conflict,
    }


def _prepare_argv(argv: list[str]) -> list[str]:
    """Defensively append --no-build to `glossary replace` only when absent
    so the executor can run a single batch-wide epub build at the end."""
    if len(argv) >= 2 and argv[0] == "glossary" and argv[1] == "replace":
        if "--no-build" not in argv:
            return [*argv, "--no-build"]
    return list(argv)


def _looks_like_noop(out: str) -> bool:
    """True when the child printed the machine no-op marker on any output
    line -- the CLI starts a `[glossary] noop: ...` line in every
    nothing-changed path, and a successful exit carrying it buckets as a
    no-op (so re-runs of `review fix` report zero applied). Only the
    marker counts: prose like "nothing to do" inside a SUCCESS line (a
    model-written definition echoed verbatim) must classify as applied."""
    return any(
        line.strip().startswith(_NOOP_MARKER)
        for line in out.splitlines()
    )


def _signals_chapter_change(out: str) -> bool:
    """True when a successful `glossary replace` rewrote at least one
    chapter. The existing console line is "[replace] Chapter_NNNN.md: N
    occurrence(s)"; the summary "[ok] replaced X occurrence(s) in Y
    chapter(s)" carries the Y count. The zero-match warning ("no
    occurrences of ... chapter(s) scanned") mentions both words while
    nothing was rewritten, so it is excluded explicitly."""
    lower = out.lower()
    if "replaced 0 occurrence" in lower or "no occurrences of" in lower:
        return False
    return "occurrence" in lower or "chapter" in lower
