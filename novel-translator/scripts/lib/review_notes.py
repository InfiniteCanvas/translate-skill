"""Advisory quality review of EXISTING translator's notes (`review notes`).

The glossary review tier (lib/review.py) audits glossary entries; this
module audits the other hand-reachable artifact, the per-chapter notes
sidecars (notes/<stem>.json). The goal of the whole notes system is notes
that add context or explain context lost in translation -- so the audit
flags notes that fail to earn their place: ones that restate the
translation, explain common knowledge, misexplain the source term, or ride
the wrong line.

Two tiers, merged into one findings list:

- A deterministic tier with no model call: every note whose anchor no
  longer resolves to any translated line (same re-resolution rules as the
  epub builder) becomes a `misanchored` warn on the spot.
- A model tier: notes are flattened across chapters in manifest order into
  review units (note + its translated line + the line-aligned source line
  + the +-2-line target context window) and judged in
  `review_batch_size` batches by the `reviewer` provider through
  templates/notes_review.md (closed judgment vocabulary: restates /
  overexplains / wrong / misanchored).

Everything is advisory: the report carries NO `- Command:` bullets (the
closed command vocabulary stays glossary-only) -- fixes are hand edits to
notes/<stem>.json, which the `tn` re-check command overwrites when it
regenerates a chapter's notes (the report footer says so). Exit code 0
regardless of finding count.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import client, config, logger, pipeline, project, tn
from lib.pipeline import LANG_NAMES, fill

# Fallback template source: the skill's shipped assets. Projects initialized
# before a template was introduced lack a copy in their templates/ dir.
_SKILL_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "assets" / "templates"

KINDS = ("restates", "overexplains", "wrong", "misanchored")
SEVERITIES = ("warn", "info")

# NO additionalProperties inside items -- strict nested schemas truncated
# sglang guided decoding historically (same constraint as review.py's
# REVIEW_SCHEMA).
FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "reason": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["idx", "kind", "severity", "reason", "suggestion"],
            },
        }
    },
    "required": ["findings"],
}


def _lang_name(code: object) -> str:
    return LANG_NAMES.get(str(code).strip().lower(), str(code))


def _kind_counts(findings: list[dict]) -> dict[str, int]:
    """Per-kind tallies in KINDS order (report frontmatter + console line)."""
    counts = {kind: 0 for kind in KINDS}
    for f in findings:
        kind = f.get("kind")
        if kind in counts:
            counts[kind] += 1
    return counts


def _resolve_line(note: dict, body_lines: list[str]) -> int | None:
    """Anchor re-resolution, same rules as the epub builder's sidecar path:
    the stored `line` index wins when that translated line still starts
    with the stored `anchor`; otherwise the first line starting with the
    anchor; otherwise None (unresolvable)."""
    line_idx = note.get("line")
    anchor = note.get("anchor")
    anchor = anchor.strip() if isinstance(anchor, str) else ""
    if (
        isinstance(line_idx, int) and not isinstance(line_idx, bool)
        and 0 <= line_idx < len(body_lines)
        and (not anchor or body_lines[line_idx].strip().startswith(anchor))
    ):
        return line_idx
    if anchor:
        return next(
            (i for i, line in enumerate(body_lines) if line.strip().startswith(anchor)),
            None,
        )
    return None


def _selected_files(project_dir: Path, manifest: list[dict],
                    chapters: str | None) -> list[str]:
    """Chapters to audit, in manifest order: the --chapters spec (same
    parser as `tn`) when given, else every manifest chapter that HAS a
    notes sidecar (chapters without one are skipped silently -- there is
    nothing to audit)."""
    if chapters is not None:
        return pipeline.parse_range(chapters, manifest)
    ordered = sorted(manifest, key=lambda e: int(e.get("order", 0)))
    return [
        str(entry["file"]) for entry in ordered
        if entry.get("file") and tn.notes_path(project_dir, str(entry["file"])).is_file()
    ]


def audit_notes(project_dir: Path, cfg: dict, chapters: str | None = None,
                batch_size: int | None = None) -> dict:
    """Collect review units + deterministic findings, run the model tier.

    Returns {"chapters": [files that contributed], "units": int, "findings":
    [finding], "batches": int, "batch_errors": [str], "skipped": [files]}.
    Selection problems propagate (parse_range's PipelineError; main() maps
    it to exit 2); per-chapter problems (missing/unreadable translated or
    source file) warn and skip, never abort the run."""
    if batch_size is None:
        batch_size = int(cfg.get("review_batch_size",
                                 config.DEFAULTS["review_batch_size"]))
    if batch_size < 1:
        batch_size = int(config.DEFAULTS["review_batch_size"])

    paths = project.paths(project_dir)
    manifest = project.load_manifest(project_dir)
    files = _selected_files(project_dir, manifest, chapters)

    units: list[dict] = []
    findings: list[dict] = []
    scanned: list[str] = []
    skipped: list[str] = []

    for file in files:
        notes = tn.load_notes(project_dir, file)
        if not notes:
            # No sidecar (or an unreadable one, already warned by load_notes)
            # or an empty note list: nothing to audit.
            continue
        translated_path = paths["translated"] / file
        if not translated_path.is_file():
            print(f"[warn] {file}: translated chapter missing - skipped")
            skipped.append(file)
            continue
        source_path = paths["source"] / file
        if not source_path.is_file():
            print(f"[warn] {file}: source chapter missing - skipped")
            skipped.append(file)
            continue
        try:
            _fm, body = project.read_chapter(translated_path)
            fm_s, src_body = project.read_chapter(source_path)
        except (ValueError, OSError) as exc:
            print(f"[notes] {file}: [warn] cannot read chapter - skipped: {exc}")
            skipped.append(file)
            continue

        # Sidecar indexes refer to the clean body: strip stray legacy [^N]
        # markers per line exactly like the epub builder's sidecar path
        # (line indexes preserved; only the marker text is removed).
        body_lines = [tn._MARKER_RE.sub("", line) for line in body.split("\n")]
        # Source pairing: VALIDATE line-aligned the two bodies; the same
        # leading-title drop as the pipeline keeps the alignment (a body
        # line repeating the frontmatter chapter_title was consumed by the
        # title field).
        source_lines = src_body.split("\n")
        first = source_lines[0].strip().strip("\u3000 ") if source_lines else ""
        if first and first == str(fm_s.get("chapter_title", "")).strip().strip("\u3000 "):
            source_lines = source_lines[1:]

        scanned.append(file)
        for note in notes:
            # idx is assigned to EVERY note (unit or deterministic finding)
            # in manifest/note order, so findings sort back into reading
            # order by idx alone.
            idx = len(units) + len(findings)
            term = note.get("term") if isinstance(note.get("term"), str) else ""
            resolved = _resolve_line(note, body_lines)
            if resolved is None:
                findings.append({
                    "chapter": file,
                    "term": term,
                    "note": note.get("note", ""),
                    "idx": idx,
                    "kind": "misanchored",
                    "severity": "warn",
                    "reason": "anchor no longer matches any translated line",
                    "suggestion": "re-attach or delete the note",
                    "origin": "deterministic",
                })
                continue
            units.append({
                "idx": idx,
                "chapter": file,
                "line": resolved,
                "term": term,
                "note": note.get("note", ""),
                "category": note.get("category"),
                "translated_line": body_lines[resolved],
                # Source body may be shorter (hand-edited translation); an
                # empty pairing is still reviewable.
                "source_line": (source_lines[resolved]
                                if resolved < len(source_lines) else ""),
                "context_before": "\n".join(body_lines[max(0, resolved - 2):resolved]),
                "context_after": "\n".join(body_lines[resolved + 1:resolved + 3]),
            })

    model, batches, batch_errors = _model_findings(project_dir, cfg, units, batch_size)
    findings.extend(model)
    # Warns first, then reading order (idx is monotonic in chapter/note
    # order) -- same severity-first sort as review.py.
    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["idx"]))
    return {
        "chapters": scanned,
        "units": len(units),
        "findings": findings,
        "batches": batches,
        "batch_errors": batch_errors,
        "skipped": skipped,
    }


def _model_findings(
    project_dir: Path, cfg: dict, units: list[dict], batch_size: int
) -> tuple[list[dict], int, list[str]]:
    """Model review in manifest-order batches; returns (findings, batches,
    errors). A single failed batch is reported and skipped, never raised.
    Rows failing the closed vocabulary (unknown idx / kind / severity,
    empty reason) are dropped with a [warn]."""
    findings: list[dict] = []
    errors: list[str] = []
    batches = [units[i:i + batch_size] for i in range(0, len(units), batch_size)]
    n = len(batches)
    for i, batch in enumerate(batches, 1):
        print(f"[notes] reviewing batch {i}/{n}")  # LLM calls are slow; show life
        try:
            lines = "\n".join(json.dumps(unit, ensure_ascii=False) for unit in batch)
            templates_dir = project.paths(project_dir)["templates"]
            tpl_path = templates_dir / "notes_review.md"
            if not tpl_path.is_file():
                tpl_path = _SKILL_TEMPLATES / "notes_review.md"
            if not tpl_path.is_file():
                raise FileNotFoundError(
                    f"missing template: notes_review.md "
                    f"(looked in {templates_dir} and {_SKILL_TEMPLATES})"
                )
            prompt = fill(
                # utf-8-sig: the project's templates dir is a user-editable
                # copy, so tolerate a BOM on the template read.
                tpl_path.read_text(encoding="utf-8-sig"),
                {
                    "source_lang": _lang_name(cfg.get("source_lang")),
                    "target_lang": _lang_name(cfg.get("target_lang")),
                    "entries": lines,
                },
                "notes_review.md",
            )

            def hook(meta: dict) -> None:
                if bool(cfg.get("log_llm", True)):
                    logger.log_event(project_dir, {"job": "reviewer", **meta})

            resp = client.chat(
                config.provider(cfg, "reviewer"), prompt,
                json_schema=FINDINGS_SCHEMA, meta_hook=hook,
            )
            data = client.extract_json(resp)
            if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
                raise ValueError(
                    "notes review response is not a JSON object with a findings list"
                )
            by_idx = {unit["idx"]: unit for unit in batch}
            for row in data["findings"]:
                if not isinstance(row, dict):
                    continue
                idx = row.get("idx")
                if (isinstance(idx, bool) or not isinstance(idx, int)
                        or idx not in by_idx):
                    print(f"[notes] warn dropped finding: idx {idx!r} not in batch")
                    continue
                kind = row.get("kind")
                if kind not in KINDS:
                    print(f"[notes] warn dropped finding for idx {idx}: "
                          f"unknown kind {kind!r}")
                    continue
                severity = row.get("severity")
                if severity not in SEVERITIES:
                    print(f"[notes] warn dropped finding for idx {idx}: "
                          f"unknown severity {severity!r}")
                    continue
                reason = row.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    print(f"[notes] warn dropped finding for idx {idx}: empty reason")
                    continue
                suggestion = row.get("suggestion")
                unit = by_idx[idx]
                findings.append({
                    "chapter": unit["chapter"],
                    "term": unit["term"],
                    "note": unit["note"],
                    "line": unit["line"],
                    "idx": idx,
                    "kind": kind,
                    "severity": severity,
                    "reason": reason,
                    "suggestion": suggestion if isinstance(suggestion, str) else "",
                    "origin": "model",
                })
        except Exception as exc:  # one bad batch must not kill the whole review
            errors.append(f"batch {i}/{n}: {exc}")
            print(f"[notes] warn batch {i}/{n} review failed - {exc}")
    return findings, n, errors


def write_report(project_dir: Path, *, result: dict, cfg: dict) -> Path:
    """Write the frontmatter-annotated advisory report to the review report
    path (`review_report_path`, default review-report.md -- same file the
    glossary tier writes, with the same per-run overwrite semantics: a
    notes run REPLACES a previous glossary report and vice versa).

    Frontmatter mirrors the glossary report's style plus `tier: notes` and
    per-kind counts; the body is one `## Notes findings` section with
    `### [N] severity / kind / chapter / term` headings (warn before info,
    reading order after) carrying Reason / Suggestion / Tier bullets -- and
    deliberately NO `- Command:` bullets: findings are advisory-only, the
    closed command vocabulary stays glossary-only. The footer states the
    hand-edit workflow and the `tn` re-check overwrite caveat. Returns the
    report path."""
    findings = result["findings"]
    n_warn = sum(1 for f in findings if f["severity"] == "warn")
    n_info = len(findings) - n_warn
    kinds = _kind_counts(findings)
    kind_bits = ", ".join(f"{kind} {kinds[kind]}" for kind in KINDS)
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report_name = cfg.get("review_report_path",
                          config.DEFAULTS["review_report_path"])

    lines: list[str] = [
        "---",
        "report_type: notes-review",
        "tier: notes",
        f"generated: {generated}",
        "generated_by: review notes",
        f"source_lang: {cfg.get('source_lang', '')}",
        f"target_lang: {cfg.get('target_lang', '')}",
        f"chapters_reviewed: {len(result['chapters'])}",
        f"notes_reviewed: {result['units']}",
        f"batch_errors: {len(result['batch_errors'])}",
        "outcome:",
        f"  warn: {n_warn}",
        f"  info: {n_info}",
        "kinds:",
        *[f"  {kind}: {kinds[kind]}" for kind in KINDS],
        "---",
        "",
        "# Notes Review Report",
        "",
        f"- Generated: {generated}",
        "- Generated by: `review notes`",
        f"- Languages: {_lang_name(cfg.get('source_lang'))} -> "
        f"{_lang_name(cfg.get('target_lang'))}",
        f"- Chapters reviewed: {len(result['chapters'])} "
        f"({result['units']} note(s), {result['batches']} model batch(es)"
        + (f", {len(result['batch_errors'])} batch error(s) -- findings from "
           "failed batches are missing" if result["batch_errors"] else "") + ")",
        f"- Outcome: {n_warn} warn / {n_info} info findings"
        + (f" ({kind_bits})" if findings else " -- every reviewed note earns its place"),
        "",
    ]

    if not findings:
        lines += ["No findings -- every reviewed note earns its place.", ""]
    else:
        lines += ["## Notes findings", ""]
        for i, f in enumerate(findings, 1):
            term = f.get("term") or "?"
            lines += [
                f"### [{i}] {f['severity']} / {f['kind']} / {f['chapter']} / {term}",
                "",
                f"- Note: {f.get('note') or '(see sidecar)'}",
            ]
            if f.get("line") is not None:
                lines.append(f"- Line: {f['line']}")
            lines.append(f"- Reason: {f['reason']}")
            if f.get("suggestion"):
                lines.append(f"- Suggestion: {f['suggestion']}")
            lines += [f"- Tier: {f.get('origin', 'model')}", ""]

    lines += [
        "## Next steps",
        "",
        "- Advisory only: this tier has no machine-applicable commands -- fix findings by hand in `notes/<stem>.json` (delete the note, reword it, or re-attach it to the right line).",
        "- Hand-edited sidecars are overwritten if the `tn` re-check command later regenerates that chapter's notes.",
        "- Re-run `review notes` to confirm the report comes back clean.",
        "",
    ]

    path = Path(project_dir) / report_name
    # review_report_path may name a subdirectory; the write side creates it
    # on demand (same as the glossary report writer).
    path.parent.mkdir(parents=True, exist_ok=True)
    project.atomic_write_text(path, "\n".join(lines), newline="\n")
    return path


def review_notes(project_dir: Path, cfg: dict, chapters: str | None = None,
                 batch_size: int | None = None) -> int:
    """Full `review notes` run: audit, report, trace event, console summary.

    Returns the finding count (the CLI exits 0 regardless -- this tier is
    advisory, there is no fix path). A run with no reviewable notes prints
    one [ok] line and writes nothing, mirroring how `review glossary`
    treats an empty glossary."""
    result = audit_notes(project_dir, cfg, chapters=chapters, batch_size=batch_size)
    findings = result["findings"]
    logger.log_event(project_dir, {"event": "notes_review", **result})
    if not result["chapters"]:
        print("[ok] no chapter notes found - nothing to review")
        return 0
    report_path = write_report(project_dir, result=result, cfg=cfg)
    kinds = _kind_counts(findings)
    kind_bits = ", ".join(f"{kind} {kinds[kind]}" for kind in KINDS)
    print(
        f"[ok] review notes: {len(findings)} findings ({kind_bits}) -> {report_path}"
    )
    if findings:
        print("[warn] review notes: fixes are hand edits to notes/<stem>.json "
              "(delete, reword, or re-attach the flagged note)")
    return len(findings)
