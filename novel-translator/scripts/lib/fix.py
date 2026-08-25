"""Post-hoc fixer driver: parse review-report.md and run each - Command:
bullet through the skill's CLI as a subprocess. Supports two parser modes:

  1. Explicit: extract every "- Command: <cli-line>" line and shlex.split() it.
  2. Legacy synthesis: when zero explicit Command lines are present, walk the
     finding blocks (### [N] warn|info / kind / source) and synthesize commands
     via review.command_for_finding() — kind from heading, source from heading,
     suggestion from - Suggestion: bullet, plus the two exact heuristic reason
     templates for variant_to_remove / merge_with.

The driver never parses - Action: prose; the writer decides machine-actionability
through one shared mapping function (review.command_for_finding).

Exit codes follow the CLI convention: 0 ok/no-op, 1 any command failed,
2 usage or setup error (missing report, zero commands parseable or
synthesizable).
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import review


# Subset of glossary verbs the parser accepts from a "- Command:" bullet.
# Any other verb (incl. the legacy header "- Command: `review glossary`")
# is ignored with a warning, never executed.
_SUPPORTED_VERBS = {"replace", "set", "merge", "retire"}

# Substrings in a subcommand's stdout/stderr that signal "no change was made".
# Used to bucket a successful exit into applied vs noop.
_NOOP_SIGNALS = (
    "already up-to-date",
    "nothing to do",
    "nothing to apply",
    "no-op",
    "already retired",
    "already translates as",
    "merge: ",  # "glossary merge: 'X' already retired" style no-op line
)

# Exact, full-line templates for heuristic reason lines. Anchored, no
# free-form prose is matched anywhere — the parser refuses to interpret
# model-written reasons.
_HEURISTIC_DUP_RE = re.compile(r"^(source|variant) '(.+?)' also belongs to entry '(.+?)'$")
_HEURISTIC_VARIANT_RE = re.compile(r"^variant '(.+?)' contains no CJK characters$")

# Finding-block heading pattern: "### [N] severity / kind / source".
_HEADING_RE = re.compile(r"^### \[(\d+)\] (\w+) / (\w+) / (.+?)\s*$")

# Command-line bullet pattern: "- Command: <text>".
_COMMAND_RE = re.compile(r"^- Command: (.+?)\s*$")


class FixError(Exception):
    """Raised by parse_report() when the report carries zero
    machine-applicable commands. cmd_review_fix translates this into
    CLI exit code 2."""


@dataclass
class CommandSpec:
    """A single - Command: line (or synthesized equivalent) ready to run."""
    raw: str                  # exact text after "- Command: " ("" when synthesized)
    argv: list[str] = field(default_factory=list)
    line_no: int = 0          # 1-based line number of the bullet (0 for synthesized)
    finding: dict = field(default_factory=dict)  # minimal finding for logging


def parse_report(path: Path) -> tuple[list[CommandSpec], int]:
    """Parse every - Command: bullet; when none exist, synthesize commands
    from finding blocks via review.command_for_finding().

    Returns (specs, total_findings_count). Raises FixError when zero
    commands are extracted in either mode (so the CLI exits 2 with a
    clear message).
    """
    text = path.read_text(encoding="utf-8")
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
    by regexing the two exact reason templates — never by parsing
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


def run_commands(
    project_dir: Path,
    script_path: Path,
    specs: Iterable[CommandSpec],
    *,
    exit_on_error: bool = False,
    dry_run: bool = False,
) -> dict:
    """Execute each spec via the skill's CLI as a subprocess.

    Appends --no-build to every `glossary replace` argv when absent so the
    executor can run a single batch-wide epub build at the end. Captures
    stdout/stderr per call, returns counts.
    """
    specs = list(specs)
    applied = 0
    noop = 0
    failed = 0
    changed_chapters = False
    per_spec: list[tuple[CommandSpec, str, int, str]] = []
    # per_spec holds (spec, action, exit_code, combined_output) for logging.

    for i, spec in enumerate(specs, 1):
        argv = _prepare_argv(spec.argv)
        if dry_run:
            per_spec.append((spec, "dry-run", 0, ""))
            continue
        # --project is the GLOBAL flag (registered on the top-level parser
        # with dest="project_global"); argparse only accepts it BEFORE the
        # subcommand, matching how scripts/lib/autobuild.py invokes
        # build-epub. Putting it after the subcommand's args is a parse error.
        full_argv = [
            sys.executable, str(script_path),
            "--project", str(project_dir),
            *argv,
        ]
        proc = subprocess.run(full_argv, capture_output=True, text=True, check=False)
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            failed += 1
            action = "failed"
            print(f"[review fix] failed at [{i}]: {' '.join(shlex.quote(t) for t in argv)}")
            if out.strip():
                # Surface the last useful line of output for diagnostics.
                tail = out.strip().splitlines()[-1]
                print(f"[review fix] {tail}")
            per_spec.append((spec, action, proc.returncode, out))
            if exit_on_error:
                break
            continue
        if _looks_like_noop(out):
            noop += 1
            action = "noop"
        else:
            applied += 1
            action = "applied"
        if argv[:2] == ["glossary", "replace"] and _signals_chapter_change(out):
            changed_chapters = True
        per_spec.append((spec, action, proc.returncode, out))

    return {
        "applied": applied,
        "noop": noop,
        "failed": failed,
        "changed_chapters": changed_chapters,
        "specs_run": len(per_spec) if not dry_run else len(specs),
        "needs_decision": 0,  # filled in by the caller (needs the total findings count)
        "per_spec": per_spec,
    }


def _prepare_argv(argv: list[str]) -> list[str]:
    """Defensively append --no-build to `glossary replace` only when absent
    so the executor can run a single batch-wide epub build at the end."""
    if len(argv) >= 2 and argv[0] == "glossary" and argv[1] == "replace":
        if "--no-build" not in argv:
            return [*argv, "--no-build"]
    return list(argv)


def _looks_like_noop(out: str) -> bool:
    """Heuristic: a successful exit that prints a known no-op marker is
    treated as a no-op (so re-runs of `review fix` report zero applied)."""
    lower = out.lower()
    return any(sig in lower for sig in _NOOP_SIGNALS)


def _signals_chapter_change(out: str) -> bool:
    """True when a successful `glossary replace` rewrote at least one
    chapter. The existing console line is "[replace] Chapter_NNNN.md: N
    occurrence(s)"; the summary "[ok] replaced X occurrence(s) in Y
    chapter(s)" carries the Y count."""
    if "replaced 0 occurrence" in out.lower():
        return False
    return "occurrence" in out.lower() or "chapter" in out.lower()


def format_command_line(argv: list[str]) -> str:
    """Render an argv back to a copy-paste-able CLI line (used by dry-run
    output and trace events)."""
    return " ".join(shlex.quote(t) for t in argv)
