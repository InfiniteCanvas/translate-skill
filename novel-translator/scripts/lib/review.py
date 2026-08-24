"""Advisory glossary quality review: deterministic heuristic checks plus a
model pass over batched entries, merged into one findings list. Report-only
by design -- apply_fixes exists solely for the CLI's opt-in --fix."""

from __future__ import annotations

import json
import re
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
                },
                "required": ["source", "kind", "severity", "reason", "suggestion"],
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
                findings.append({
                    "source": source,
                    "kind": kind,
                    "severity": severity,
                    "reason": reason,
                    "suggestion": suggestion if isinstance(suggestion, str) else "",
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
        elif not h.get("suggestion") and mf.get("suggestion"):
            # Heuristic won the reporting slot, but the fix is model-sourced:
            # mark it so --fix may still apply it (apply_fixes gates on that).
            h["suggestion"] = mf["suggestion"]
            h["fixable"] = True
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
