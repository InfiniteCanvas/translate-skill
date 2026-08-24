"""Advisory glossary quality review: deterministic heuristic checks plus a
model pass over batched entries, merged into one findings list. Report-only
by design -- apply_fixes exists solely for the CLI's opt-in --fix."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import client, config, glossary, logger, project
from lib.pipeline import LANG_NAMES, fill

# Fallback template source: the skill's shipped assets. Projects initialized
# before a template was introduced lack a copy in their templates/ dir.
_SKILL_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "assets" / "templates"

KINDS = ("mistranslation", "wrong_language", "definition", "category",
         "variant", "duplicate", "collision", "other")
SEVERITIES = ("warn", "info")
DEFAULT_BATCH_SIZE = 40

# NO additionalProperties inside items -- strict nested schemas truncated
# sglang guided decoding historically (same constraint as CLEANUP_SCHEMA).
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "reason": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["source", "kind", "severity", "reason", "suggestion", "action"],
            },
        }
    },
    "required": ["findings"],
}

# Same CJK range as the pipeline's output estimator.
_CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")

# Model kinds apply_fixes may act on; duplicate/collision/variant/other are
# report-only.
_FIELD_BY_KIND = {
    "mistranslation": "translation",
    "wrong_language": "translation",
    "definition": "definition",
    "category": "category",
}


def _lang_name(code: object) -> str:
    return LANG_NAMES.get(str(code).strip().lower(), str(code))


def _finding(entry: dict, kind: str, severity: str, reason: str) -> dict:
    source = entry.get("source")
    return {
        "source": source if isinstance(source, str) else "",
        "kind": kind,
        "severity": severity,
        "reason": reason,
        "suggestion": "",  # heuristic tier never proposes fixes
        "action": "",
        "origin": "heuristic",
    }


def _heuristic_findings(g: dict) -> list[dict]:
    """Deterministic local checks (no model, no I/O)."""
    entries = g.get("terms", [])
    findings: list[dict] = []

    # duplicate: one string owned by 2+ entries (as source or variant).
    owners: dict[str, list[dict]] = {}
    for entry in entries:
        for s in [entry.get("source")] + list(entry.get("variants") or []):
            if not s:
                continue
            owner_list = owners.setdefault(s, [])
            if not any(existing is entry for existing in owner_list):
                owner_list.append(entry)
    for s, owner_list in owners.items():
        if len(owner_list) < 2:
            continue
        first_source = owner_list[0].get("source", "")
        for entry in owner_list[1:]:
            role = "source" if entry.get("source") == s else "variant"
            findings.append(_finding(
                entry, "duplicate", "warn",
                f"{role} '{s}' also belongs to entry '{first_source}'",
            ))

    # collision: distinct entries sharing one translation (case-insensitive).
    first_by_translation: dict[str, dict] = {}
    for entry in entries:
        translation = entry.get("translation")
        if not isinstance(translation, str) or not translation.strip():
            continue
        key = translation.strip().lower()
        first = first_by_translation.get(key)
        if first is None:
            first_by_translation[key] = entry
        elif first.get("source") != entry.get("source"):
            findings.append(_finding(
                entry, "collision", "info",
                f"translation '{translation.strip()}' is also used by entry "
                f"'{first.get('source', '')}'",
            ))

    for entry in entries:
        source = entry.get("source")
        if not isinstance(source, str):
            continue
        translation = entry.get("translation")
        cjk_source = bool(_CJK_RE.search(source))
        if isinstance(translation, str) and translation.strip():
            if cjk_source and _CJK_RE.search(translation):
                findings.append(_finding(
                    entry, "wrong_language", "warn",
                    f"translation '{translation.strip()}' contains CJK characters",
                ))
            elif translation.strip().lower() == source.strip().lower():
                findings.append(_finding(
                    entry, "wrong_language", "warn",
                    f"translation '{translation.strip()}' is identical to the source term",
                ))
        category = entry.get("category")
        if category is None:
            # Legal for minimal hand-added entries (source + translation);
            # only the invalid-string case is a real quality problem.
            findings.append(_finding(
                entry, "category", "info",
                "category is missing (other stages treat it as 'other')",
            ))
        elif category not in glossary.CATEGORIES:
            findings.append(_finding(
                entry, "category", "warn",
                f"category '{category}' is not one of the known categories",
            ))
        if cjk_source:
            for variant in entry.get("variants") or []:
                if isinstance(variant, str) and not _CJK_RE.search(variant):
                    findings.append(_finding(
                        entry, "variant", "info",
                        f"variant '{variant}' contains no CJK characters",
                    ))
    return findings


def _model_findings(
    project_dir: Path, cfg: dict, entries: list[dict], batch_size: int
) -> tuple[list[dict], int, list[str]]:
    """Model review in glossary-order batches; returns (findings, batches,
    errors). A single failed batch is reported and skipped, never raised."""
    findings: list[dict] = []
    errors: list[str] = []
    batches = [entries[i:i + batch_size] for i in range(0, len(entries), batch_size)]
    n = len(batches)
    for i, batch in enumerate(batches, 1):
        print(f"[glossary] reviewing batch {i}/{n}")  # LLM calls are slow; show life
        try:
            lines = "\n".join(
                json.dumps(
                    {k: entry.get(k) for k in (
                        "source", "variants", "translation", "alt_translations",
                        "definition", "category", "origin", "first_seen_chapter",
                    )},
                    ensure_ascii=False,
                )
                for entry in batch
            )
            templates_dir = project.paths(project_dir)["templates"]
            tpl_path = templates_dir / "glossary_review.md"
            if not tpl_path.is_file():
                tpl_path = _SKILL_TEMPLATES / "glossary_review.md"
            if not tpl_path.is_file():
                raise FileNotFoundError(
                    f"missing template: glossary_review.md "
                    f"(looked in {templates_dir} and {_SKILL_TEMPLATES})"
                )
            prompt = fill(
                tpl_path.read_text(encoding="utf-8"),
                {
                    "source_lang": _lang_name(cfg.get("source_lang")),
                    "target_lang": _lang_name(cfg.get("target_lang")),
                    "entries": lines,
                },
                "glossary_review.md",
            )

            def hook(meta: dict) -> None:
                if bool(cfg.get("log_llm", True)):
                    logger.log_event(project_dir, {"job": "glossary", **meta})

            resp = client.chat(
                config.provider(cfg, "glossary"), prompt,
                json_schema=REVIEW_SCHEMA, meta_hook=hook,
            )
            data = client.extract_json(resp)
            if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
                raise ValueError("review response is not a JSON object with a findings list")
            batch_sources = {entry.get("source") for entry in batch}
            for row in data["findings"]:
                if not isinstance(row, dict):
                    continue
                source = row.get("source")
                if not isinstance(source, str) or source not in batch_sources:
                    continue
                kind = row.get("kind")
                if kind not in KINDS:
                    kind = "other"
                severity = row.get("severity")
                if severity not in SEVERITIES:
                    severity = "info"
                reason = row.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    continue
                suggestion = row.get("suggestion")
                action = row.get("action")
                findings.append({
                    "source": source,
                    "kind": kind,
                    "severity": severity,
                    "reason": reason,
                    "suggestion": suggestion if isinstance(suggestion, str) else "",
                    "action": action if isinstance(action, str) else "",
                    "origin": "model",
                })
        except Exception as exc:  # one bad batch must not kill the whole review
            errors.append(f"batch {i}/{n}: {exc}")
            print(f"[glossary] warn batch {i}/{n} review failed - {exc}")
    return findings, n, errors


def review_glossary(
    project_dir: Path, cfg: dict, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict:
    """Load glossary.json, run both tiers, return
    {"entries": int, "findings": [finding], "batches": int, "batch_errors": [str]}.

    Heuristic findings win over same-(source, kind) model findings, whose
    suggestion is still copied in when the heuristic has none; findings sort
    warn-first then source asc. Per-batch model failures land in batch_errors
    instead of raising; genuinely unexpected errors propagate (main() already
    maps PipelineError/OSError to exit 2)."""
    if batch_size < 1:
        batch_size = DEFAULT_BATCH_SIZE
    g = glossary.load(project_dir)
    entries = g.get("terms", [])
    heuristic = _heuristic_findings(g)
    model, batches, batch_errors = _model_findings(project_dir, cfg, entries, batch_size)

    findings = list(heuristic)
    by_key: dict[tuple[str, str], dict] = {}
    for f in heuristic:
        by_key.setdefault((f["source"], f["kind"]), f)
    for mf in model:
        h = by_key.get((mf["source"], mf["kind"]))
        if h is None:
            findings.append(mf)
            continue
        if not h.get("suggestion") and mf.get("suggestion"):
            # Heuristic won the reporting slot, but the fix is model-sourced:
            # mark it so --fix may still apply it (apply_fixes gates on that).
            h["suggestion"] = mf["suggestion"]
            h["fixable"] = True
        if not h.get("action") and mf.get("action"):
            h["action"] = mf["action"]
    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["source"]))
    return {
        "entries": len(entries),
        "findings": findings,
        "batches": batches,
        "batch_errors": batch_errors,
    }


def field_for_kind(kind: str) -> str | None:
    """The glossary field a fixable kind amends (None = report-only kind)."""
    return _FIELD_BY_KIND.get(kind)


REPORT_NAME = "review-report.md"


def _action_text(finding: dict, source_name: str, target_name: str) -> str:
    """Deterministic per-kind fix instruction for the report's Action line."""
    kind = finding.get("kind", "other")
    suggestion = str(finding.get("suggestion") or "").strip()
    if kind in ("mistranslation", "wrong_language"):
        if suggestion:
            return f'Set the `translation` field of this entry to "{suggestion}".'
        return (f"Decide the correct {target_name} rendering and update the "
                "`translation` field.")
    if kind == "definition":
        if suggestion:
            return f'Set `definition` to "{suggestion}".'
        return "Rewrite `definition` so it accurately describes the term in one sentence."
    if kind == "category":
        if suggestion:
            return f'Set `category` to "{suggestion}".'
        return f"Set `category` to one of: {', '.join(glossary.CATEGORIES)}."
    if kind == "variant":
        return ("Remove the flagged string from `variants` (variants are "
                f"{source_name}-script spellings of the source); move it to "
                "`alt_translations` only if it is an alternative translation.")
    if kind == "duplicate":
        return ("Merge this entry with the other owner of the same string: combine "
                "`variants`, keep one entry, delete the other, and add the deleted "
                'source to the top-level "retired" list so it is not re-added.')
    if kind == "collision":
        return ("Give this entry a `translation` distinct from the other entry's, "
                "or move the shared rendering to `alt_translations`.")
    return "Review this entry and correct it as judged."


def write_report(
    project_dir: Path, *, findings: list[dict], terms: list[dict],
    applied: list[dict], skipped: list[dict], ran_fix: bool,
    batches: int, batch_errors: list[str], cfg: dict,
) -> Path:
    """Write the indexed, agent-actionable <project>/review-report.md.

    Findings whose kind-field was fixed this run (same (source, field) as an
    applied fix) are excluded from the numbering and listed under "Fixed
    automatically" instead — indices only cover outstanding work. Returns the
    report path."""
    source_name = _lang_name(cfg.get("source_lang"))
    target_name = _lang_name(cfg.get("target_lang"))
    by_source: dict[str, dict] = {}
    for entry in terms:
        src = entry.get("source")
        if isinstance(src, str) and src not in by_source:
            by_source[src] = entry

    resolved = {(a["source"], a["field"]) for a in applied}

    def outstanding(f: dict) -> bool:
        field = _FIELD_BY_KIND.get(f.get("kind"))
        return field is None or (f.get("source"), field) not in resolved

    open_findings = [f for f in findings if outstanding(f)]
    n_warn = sum(1 for f in open_findings if f["severity"] == "warn")
    n_info = len(open_findings) - n_warn

    lines: list[str] = [
        "# Glossary Review Report",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- Command: `review glossary`" + (" `--fix`" if ran_fix else ""),
        f"- Languages: {source_name} -> {target_name}",
        f"- Entries reviewed: {len(terms)} ({batches} model batch(es)"
        + (f", {len(batch_errors)} batch error(s) — findings from failed batches are missing"
           if batch_errors else "") + ")",
        f"- Outcome: {n_warn} warn / {n_info} info outstanding"
        + (f", {len(applied)} fixed automatically" if applied else ""),
        "",
    ]

    if not open_findings:
        lines += ["No outstanding findings — the glossary is clean.", ""]
    else:
        idx = 0
        for severity, title in (
            ("warn", "Warnings (fix before translating further)"),
            ("info", "Info (optional improvements)"),
        ):
            group = [f for f in open_findings if f["severity"] == severity]
            if not group:
                continue
            lines += [f"## {title}", ""]
            for f in group:
                idx += 1
                entry = by_source.get(f["source"])
                lines += [f"### [{idx}] {f['severity']} / {f['kind']} / {f['source']}", ""]
                lines.append(f"- Reason: {f['reason']}")
                if f.get("suggestion"):
                    lines.append(f"- Suggestion: {f['suggestion']}")
                lines.append(f"- Tier: {f.get('origin', 'model')}")
                if entry is not None:
                    lines += [
                        "- Entry:",
                        "",
                        "```json",
                        json.dumps(entry, ensure_ascii=False, indent=2),
                        "```",
                    ]
                # Model-written instruction preferred; the per-kind template
                # guarantees a baseline when the model sent none.
                action = str(f.get("action") or "").strip() or _action_text(f, source_name, target_name)
                lines += [f"- Action: {action}", ""]

    if applied:
        lines += ["## Fixed automatically (--fix)", ""]
        lines += [f"- `{a['source']}`: {a['field']} '{a['old']}' -> '{a['new']}'"
                  for a in applied]
        lines.append("")
    if skipped:
        lines += ["## Fixes skipped (need a decision)", ""]
        lines += [f"- `{s['source']}` ({s['field']}): {s['reason']}" for s in skipped]
        lines.append("")

    lines += [
        "## Next steps",
        "",
        "- Fix indexed findings by editing glossary.json (hand edits are safe — it is re-read before every chapter).",
        '- Delete junk entries outright and add their source to the top-level "retired" list so seed/GLOSSARY_EXPAND will not re-add them.',
        "- Re-run `review glossary` to confirm the report comes back clean (exit 0).",
        "- If chapters were already translated with a wrong rendering, re-run `retry --chapters N` after fixing.",
        "",
    ]

    path = Path(project_dir) / REPORT_NAME
    project.atomic_write_text(path, "\n".join(lines), newline="\n")
    return path


def apply_fixes(project_dir: Path, findings: list[dict]) -> dict:
    """Apply guarded model-suggested fixes to glossary.json.

    Only warn findings carrying a model-sourced suggestion are considered —
    origin "model", or a heuristic finding flagged "fixable" by the merge
    (the suggestion was borrowed from a model finding) — and only kinds with
    a target field (_FIELD_BY_KIND); conflicts on the same entry field skip
    every side. Returns
    {"applied": [{"source","field","kind","old","new"}],
     "skipped": [{"source","field","reason"}]}; saves only when something
    applied. Never prints -- the CLI prints from the return value."""
    g = glossary.load(project_dir)
    eligible: list[tuple[str, str, str, str]] = []  # (source, field, kind, suggestion)
    for f in findings:
        if f.get("severity") != "warn":
            continue
        if f.get("origin") != "model" and not f.get("fixable"):
            continue
        field = _FIELD_BY_KIND.get(f.get("kind"))
        if field is None:
            continue
        source = f.get("source")
        suggestion = f.get("suggestion")
        if (
            not isinstance(source, str)
            or not isinstance(suggestion, str)
            or not suggestion.strip()
        ):
            continue
        eligible.append((source, field, f["kind"], suggestion))

    # Resolve targets up front so conflicts on the same (entry source, field)
    # are caught before any individual validation.
    resolved: list[tuple[tuple[str, str], str, str, str, str, dict | None]] = []
    for source, field, kind, suggestion in eligible:
        entry = glossary.find(g, source)
        key = ((entry.get("source") or source) if entry is not None else source, field)
        resolved.append((key, source, field, kind, suggestion, entry))
    counts: dict[tuple[str, str], int] = {}
    for key, _source, _field, _kind, _suggestion, _entry in resolved:
        counts[key] = counts.get(key, 0) + 1

    applied: list[dict] = []
    skipped: list[dict] = []
    for key, source, field, kind, suggestion, entry in resolved:
        def skip(reason: str) -> None:
            skipped.append({"source": source, "field": field, "reason": reason})

        if counts[key] > 1:
            skip("conflicting suggestions")
            continue
        if entry is None:
            skip("entry not found")
            continue
        value = suggestion.strip()
        if not value:
            skip("empty suggestion")
            continue
        if field == "category" and value not in glossary.CATEGORIES:
            skip("invalid category")
            continue
        if (
            field == "translation"
            and isinstance(entry.get("source"), str)
            and _CJK_RE.search(entry["source"])
            and _CJK_RE.search(value)
        ):
            skip("suggestion not in target language")
            continue
        old = entry.get(field)
        entry[field] = value  # mutate in place; keep every other field
        applied.append({"source": source, "field": field, "kind": kind,
                        "old": old, "new": value})
    if applied:
        glossary.save(project_dir, g)
    return {"applied": applied, "skipped": skipped}
