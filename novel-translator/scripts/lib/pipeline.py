"""Staged, resumable translation pipeline for novel chapters.

Per-chapter stages: TRANSLATE -> VALIDATE -> BALANCE -> FAITH ->
GLOSSARY_EXPAND -> TN_GENERATE -> TN_DEDUP -> ASSEMBLE. Glossary expansion
runs only after the faithfulness gate accepts the translation, so terms from
rejected attempts never enter the glossary; drift-signal retirements decided
in BALANCE are likewise applied only on the accepted attempt.

Progress is persisted in draft/<stem>.state.json after every stage, so an
interrupted chapter resumes at the failed stage instead of restarting, and a
failed attempt re-runs TRANSLATE with the accumulated reviewer feedback.
Inside TRANSLATE the persistence is per chunk: validated part translations
append to the state's "chunks" list as they complete (deterministic
token-budget packing keeps the bounds stable across resumes), so a crash or
Ctrl-C loses at most the in-flight part.

Cross-chapter context beyond the glossary: a rolling story-so-far recap
(story_state.json, one <= 120-word entry per chapter maintained by lib/story)
rides the [Background Information] frame into every prompt that fills
{{background_section}} -- TRANSLATE, FAITH, and TN_GENERATE.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lib import assemble, autobuild, balance, client, config, consensus, glossary, logger, project, story, styles, tn, vcs
from lib.errors import PipelineError as _PipelineError

STAGES = (
    "TRANSLATE",
    "VALIDATE",
    "BALANCE",
    "FAITH",
    "GLOSSARY_EXPAND",
    "TN_GENERATE",
    "TN_DEDUP",
    "ASSEMBLE",
)

# State-file format version. v1 (unmarked) predates the FAITH/GLOSSARY_EXPAND
# reorder; its "GLOSSARY_EXPAND"/"FAITH" stages both mean FAITH had not
# finished, so they remap to FAITH on load (see run_chapter).
STATE_VERSION = 2

TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        # Numbered line protocol: each translated line echoes the 1-based
        # index of its source line. Explicit indices make dropped/merged
        # lines structurally detectable instead of off-by-one guesswork.
        # Deliberately NO nested "additionalProperties": false -- the strict
        # grammar intermittently truncates sglang's guided decoding on
        # longer generations (verified empirically); the pipeline validates
        # shape and index coverage itself.
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "t": {"type": "string"}},
                "required": ["i", "t"],
            },
        },
    },
    "required": ["title", "lines"],
}

TERMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "variants": {"type": "array", "items": {"type": "string"}},
                    "translation": {"type": "string"},
                    "definition": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["source", "variants", "translation", "definition", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["terms"],
    "additionalProperties": False,
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["SUCCESS", "FAILURE"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "reasons"],
    "additionalProperties": False,
}

NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "term": {"type": "string"},
                    "note": {"type": "string"},
                    # What kind of context the note carries (glossary-aware
                    # TN annotation); OPTIONAL -- models may omit it and
                    # tn.process silently defaults to "other".
                    "category": {"type": "string", "enum": list(tn.NOTE_CATEGORIES)},
                    # Optional self-assessed comprehension threshold
                    # (Hy-MT2's cultural-adaptation pattern); tn.process
                    # discards "low" entries. Missing = keep.
                    "threshold": {"type": "string", "enum": ["high", "low"]},
                },
                "required": ["line", "term", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["notes"],
    "additionalProperties": False,
}

MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "translation": {"type": "string"},
        "definition": {"type": "string"},
        "category": {"type": "string"},
    },
    "required": ["source", "translation", "definition", "category"],
    "additionalProperties": False,
}

# Glossary-cleanup verdicts on balance drift signals. NO
# additionalProperties inside items -- strict nested schemas truncated
# sglang guided decoding historically.
CLEANUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "keep": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["source", "keep", "reason"],
            },
        }
    },
    "required": ["decisions"],
}

_STAGE_IDX = {name: i for i, name in enumerate(STAGES)}
_KEY_RE = re.compile(r"\{\{([^{}]+)\}\}")
_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")

# Language codes -> full names. Hy-MT2's model card: prompts must use full
# language names ("Chinese"/"English"), never raw codes ("zh"/"en"). Codes
# outside the table pass through unchanged.
LANG_NAMES: dict[str, str] = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ja": "Japanese",
    "ko": "Korean", "es": "Spanish", "fr": "French", "de": "German",
    "ru": "Russian", "pt": "Portuguese", "it": "Italian", "ar": "Arabic",
    "hi": "Hindi", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai",
    "tr": "Turkish", "ms": "Malay", "km": "Khmer", "lo": "Lao", "my": "Burmese",
    "tl": "Tagalog", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
    "sv": "Swedish", "da": "Danish", "fi": "Finnish", "no": "Norwegian",
    "cs": "Czech", "el": "Greek", "he": "Hebrew", "hu": "Hungarian",
    "ro": "Romanian", "bg": "Bulgarian", "sr": "Serbian", "hr": "Croatian",
    "sk": "Slovak", "sl": "Slovenian",
}


def _lang_name(code: object) -> str:
    return LANG_NAMES.get(str(code).strip().lower(), str(code))


# The class itself lives in `lib.errors` so `client.LLMFatal` can inherit it
# without an import cycle (`pipeline` imports `client`, never the reverse).
# ALIASED, not subclassed: `client.LLMFatal(LLMError, PipelineError)` must be a
# subtype of what every `except PipelineError: raise` guard below matches on,
# and a subclass here would invert that relationship and route fatal provider
# failures straight into the broad handlers.
PipelineError = _PipelineError


# --------------------------------------------------------------------------
# template filling
# --------------------------------------------------------------------------


def fill(template_text: str, mapping: dict, template_name: str = "template") -> str:
    """Replace every "{{key}}" in template_text with str(mapping[key]).

    Raises PipelineError when the TEMPLATE itself contains a {{key}} missing
    from the mapping, so template typos fail fast instead of silently leaking
    into a prompt. The substituted output is deliberately NOT re-scanned: a
    literal {{...}} inside substituted values (glossary definitions, source
    lines) must pass through untouched.
    """
    missing = sorted(
        {
            key
            for key in (m.group(1) for m in _KEY_RE.finditer(template_text))
            if key not in mapping and key.strip() not in mapping
        }
    )
    if missing:
        raise PipelineError(
            f"template {template_name}: unfilled placeholder(s): "
            + ", ".join("{{" + key + "}}" for key in missing)
        )

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in mapping:
            return str(mapping[key])
        return str(mapping[key.strip()])

    return _KEY_RE.sub(_replace, template_text)


# --------------------------------------------------------------------------
# chapter spec parsing
# --------------------------------------------------------------------------


def _entry_number(entry: dict) -> int:
    return int(entry["number"])


def _entry_keys(entry: dict) -> set[str]:
    """Acceptable normalized name tokens for a manifest entry.

    CHAPTER_NNNN.md has no suffix any more, so there is nothing to strip here.
    The keys stay lowercase because _name_matches lowercases the spec first,
    which is what lets a user type "chapter_7" for CHAPTER_0007.md.
    _name_matches also tolerates a trailing ".xxx" on the SPEC side (a typed
    "CHAPTER_0007.zh.md" from the retired convention), which is a user-typing
    convenience and costs nothing -- but the manifest field it used to read
    from is gone."""
    file_l = str(entry.get("file", "")).strip().lower()
    core = file_l[:-3] if file_l.endswith(".md") else file_l
    num = _entry_number(entry)
    return {
        file_l,
        core,
        f"chapter_{num:04d}",
        f"chapter_{num}",
        str(num),
        f"{num:04d}",
    }


def _name_matches(item: str, entry: dict) -> bool:
    it = item.strip().lower().replace("\\", "/").split("/")[-1]
    it = re.sub(r"\.md$", "", it)
    it_nosuf = re.sub(r"\.[^.]+$", "", it)
    keys = _entry_keys(entry)
    return it in keys or it_nosuf in keys


def parse_range(spec: str, manifest: list[dict]) -> list[str]:
    """Resolve a spec ("1,3-5,CHAPTER_0007.md") to file names in manifest order.

    Items may be chapter numbers ("7"), inclusive ranges ("3-5"), or exact
    file names (an item like "CHAPTER_0007.zh.md" -- a retired source-naming
    convention -- still
    matches the manifest's "CHAPTER_0007.md"). Raises PipelineError when
    nothing matches an item.
    """
    if not manifest:
        raise PipelineError("manifest is empty - run 'init' first")
    entries = sorted(manifest, key=lambda e: int(e.get("order", 0)))
    picked: set[int] = set()
    unknown: list[str] = []

    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        m = _RANGE_RE.match(item)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise PipelineError(f"invalid chapter range '{item}': lower bound above upper bound")
            hits = {i for i, e in enumerate(entries) if lo <= _entry_number(e) <= hi}
        elif item.isdigit():
            n = int(item)
            hits = {i for i, e in enumerate(entries) if _entry_number(e) == n}
        else:
            hits = {i for i, e in enumerate(entries) if _name_matches(item, e)}
        if hits:
            picked.update(hits)
        else:
            unknown.append(item)

    if unknown:
        lo = min(_entry_number(e) for e in entries)
        hi = max(_entry_number(e) for e in entries)
        example = entries[0].get("file", "")
        raise PipelineError(
            f"no chapters match: {', '.join(unknown)} "
            f"(valid chapters: {lo}-{hi}, or file names like '{example}')"
        )
    return [entries[i]["file"] for i in sorted(picked)]


# --------------------------------------------------------------------------
# per-chapter state (draft/<stem>.state.json)
# --------------------------------------------------------------------------


def _state_path(draft_dir: Path, file: str) -> Path:
    return draft_dir / f"{Path(file).stem}.state.json"


def load_state(draft_dir: Path, file: str) -> dict | None:
    path = _state_path(draft_dir, file)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        reason = type(exc).__name__
    else:
        reason = None if isinstance(data, dict) else type(data).__name__
    if reason is not None:
        print(f"[warn] {path.name} unreadable ({reason}) - restarting chapter state")
        return None
    return data


def save_state(draft_dir: Path, file: str, state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    project.atomic_write_text(
        _state_path(draft_dir, file),
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _cfg_value(cfg: dict, key: str) -> Any:
    """Resolve key from cfg with the skill defaults as fallback (a missing
    key raises PipelineError) and require a number-compatible value: the
    numeric keys are int()/float()-coerced at the call sites, which would
    turn a JSON null or a free-text value into a bare TypeError, so those
    map to a clean PipelineError instead. Bool-only keys
    (glossary_auto_cleanup, tn_keep_low_confidence, auto_build_epub) read
    through here too -- including from tn_recheck -- so unlike
    config.get_number a bool passes."""
    if key in cfg:
        value: object = cfg[key]
    elif key in config.DEFAULTS:
        value = config.DEFAULTS[key]
    else:
        raise PipelineError(f"missing config key: {key}")
    if isinstance(value, bool):
        return value
    try:
        return config._as_number(key, value)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def _feedback_section(feedback: list[str], rejected_lines: list[str] | None,
                      lo: int = 0, hi: int | None = None) -> str:
    """Render the {{feedback_section}} block of the TRANSLATE prompt.

    Bullets-only when no gate-rejected translation exists (first attempt,
    or the failed gate was TRANSLATE itself with no earlier judged
    attempt). With one, the rejected translation is appended in the same
    numbered-line protocol as the source data, sliced to the chunk's
    half-open source range [lo, hi) so a chunked retry sees exactly the
    rejected lines corresponding to its input.
    """
    if not feedback:
        return ""
    bullets = "\n".join(f"- {item}" for item in feedback)
    if not rejected_lines:
        return (
            "NOTE: A previous translation attempt was rejected. "
            "Address every issue below and translate the entire chapter "
            "again from scratch:\n" + bullets
        )
    chunk = rejected_lines[lo:hi]
    numbered = json.dumps(
        [{"i": lo + j + 1, "t": ln} for j, ln in enumerate(chunk)],
        ensure_ascii=False,
    )
    return (
        "NOTE: A previous translation attempt was rejected. Address every "
        "issue below; your rejected attempt is reproduced after the feedback "
        "(same line numbers as the source data) - fix the flagged problems "
        "in it and return the full corrected translation:\n"
        + bullets
        + "\n[Rejected Previous Attempt]\n" + numbered
    )


def background_section(*parts: str) -> str:
    """Render the Hy-MT2 [Background Information] frame; empty when there
    is nothing to say (keeps templates clean for unprofiled projects).
    Parts order: novel background, rolling story recap, then the caller's
    extra (the chunk-tail continuity note), so the recap reaches every
    prompt that fills {{background_section}}. Parts are used verbatim --
    nothing is stripped here (story.py returns recaps unstripped, and a
    .strip() inside would change prompt bytes); empty parts drop out."""
    kept = [p for p in parts if p]
    if not kept:
        return ""
    return "[Background Information]\n" + "\n".join(kept) + "\n"


# Fallback template source: the skill's shipped assets. Projects initialized
# before a template was introduced lack a copy in their templates/ dir.
_SKILL_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "assets" / "templates"


def _load_template(templates_dir: Path, name: str, *,
                   error: Callable[[str], Exception] = PipelineError) -> str:
    path = templates_dir / name
    if not path.is_file():
        path = _SKILL_TEMPLATES / name
    if not path.is_file():
        raise error(f"missing template: {name} (looked in {templates_dir} and {_SKILL_TEMPLATES})")
    # utf-8-sig: the project's templates dir is a user-editable copy, so
    # tolerate a BOM on the template read.
    return path.read_text(encoding="utf-8-sig")


# Joined source-chapter bodies per project dir, built once per process for
# the GLOSSARY_EXPAND significance gate (glossary.count_in_text counts each
# candidate against this text). The cache is EXACT, not approximate: source
# files don't change during a run, and term counts are pure lookups against
# static text. Only successful reads are cached -- an unreadable corpus
# stays uncached, so every chapter retries it and fails open exactly as it
# would without the cache.
_GATE_CORPUS: dict[Path, str] = {}


def _gate_corpus(project_dir: Path) -> str:
    """All discovered source-chapter bodies joined on newlines (cached)."""
    key = Path(project_dir).resolve()
    if key not in _GATE_CORPUS:
        _GATE_CORPUS[key] = "\n".join(
            project.read_chapter(c.path)[1]
            for c in project.discover(project_dir)
        )
    return _GATE_CORPUS[key]


def _apply_glossary_proposal(
    g: dict, proposal: Any, chapter_order: int, cfg: dict, merge_template: str,
    tag: str, project_dir: Path,
    corpus: str = "", min_occurrences: int = 0,
    chapter: str | None = None,
) -> None:
    """Apply one glossary proposal: add, merge, absorb, or silently skip.

    Every proposal whose source does not match an existing entry -- nickname
    absorption included -- is gated on novel-wide significance: when
    min_occurrences > 0 it is added (or absorbed as a variant) only if the
    term occurs at least that many times in `corpus`. The source of a
    matched entry is never re-gated and real merges of a matched entry stay
    ungated, but any NEW variant a re-proposal carries is gated like a
    brand-new term (same corpus, same threshold), so a one-occurrence
    string can never become a permanently matchable variant through the
    back door. Raises on malformed proposals or merge failures; callers
    treat those as non-fatal and skip the proposal.
    """
    if not isinstance(proposal, dict):
        raise ValueError(f"proposal is {type(proposal).__name__}, expected an object")
    src = proposal.get("source")
    tr = proposal.get("translation")
    df = proposal.get("definition")
    cat = proposal.get("category")
    for value in (src, tr, df, cat):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("proposal fields source/translation/definition/category must be non-empty strings")
    raw_variants = proposal.get("variants")
    variants = (
        [v.strip() for v in raw_variants if isinstance(v, str) and v.strip()]
        if isinstance(raw_variants, list)
        else []
    )
    if src in glossary.retired_sources(g):
        print(f"{tag} [glossary] skip re-adding retired term '{src}'")
        return

    def union_variants(entry: dict, add: list[str]) -> None:
        current = [v for v in (entry.get("variants") or []) if isinstance(v, str) and v]
        new = [
            v for v in add
            if v not in current and v != str(entry.get("source", ""))
        ]
        if new:
            entry["variants"] = current + new
            print(f"{tag} [ok] glossary ~ '{entry.get('source', '')}' +variant(s) {', '.join(new)}")

    existing = glossary.find(g, src)
    if existing is None:
        # Significance gate FIRST, for every source that is not an existing
        # entry: a below-threshold proposal is dropped before the nickname
        # loop, so a one-occurrence string can never become a permanently
        # matchable variant.
        if min_occurrences > 0:
            seen = glossary.count_in_text({"source": src, "variants": variants}, corpus)
            if seen < min_occurrences:
                print(f"{tag} [glossary] skip '{src}' - {seen} occurrence(s) across the novel (min {min_occurrences})")
                return
        # A proposal whose source is contained in (or contains) a known term's
        # source, with the same translation, is a nickname/short form: absorb
        # it as a variant instead of creating a double-counting entry. Only
        # gate-passing proposals get here, so an absorbed variant is
        # novel-wide significant too.
        for term in g.get("terms", []):
            esrc = str(term.get("source", ""))
            etr = str(term.get("translation", "")).strip().lower()
            if etr == tr.strip().lower() and esrc and (src in esrc or esrc in src):
                union_variants(term, [src] + variants)
                return
        # Category coercion, proposal-side only (upsert stores verbatim):
        # a model-sourced category outside glossary.CATEGORIES lands as
        # "other" with a warn -- only when the term actually lands, i.e.
        # after the significance/nickname returns above.
        if cat not in glossary.CATEGORIES:
            print(f"{tag} [glossary] warn unknown category '{cat}' for '{src}' - coerced to 'other'")
            cat = "other"
        if cat == "unit" and tr.strip():
            # A 'unit' entry is a rendering guide only: balance.check skips
            # the category entirely, so a stored translation would never be
            # counted or enforced -- say so instead of storing it silently.
            print(f"{tag} [warn] {glossary.unit_translation_warning(src, tr)}")
        glossary.upsert(
            g,
            {
                "source": src,
                "variants": variants,
                "translation": tr,
                "definition": df,
                "category": cat,
                "origin": "model",
                "first_seen_chapter": chapter_order,
            },
        )
        print(f"{tag} [ok] glossary + '{src}' -> '{tr}'")
        return

    # Newly proposed variants belong on the existing entry regardless of
    # whether the translation matches -- but a NEW variant (not already on
    # the entry, not the source itself) is gated exactly like a brand-new
    # term, so a one-occurrence string can never become a permanently
    # matchable variant through the back door.
    addable = variants
    if min_occurrences > 0:
        current = [v for v in (existing.get("variants") or [])
                   if isinstance(v, str) and v]
        source_str = str(existing.get("source", ""))
        addable = []
        for v in variants:
            if v in current or v == source_str:
                # Already-present variants are never re-gated (a re-proposal
                # restating one stays a silent no-op, mirroring
                # union_variants' dedupe) and a variant equal to the entry's
                # source is dropped by union_variants anyway.
                addable.append(v)
                continue
            seen = glossary.count_in_text({"source": v, "variants": []}, corpus)
            if seen < min_occurrences:
                print(f"{tag} [glossary] skip variant '{v}' - {seen} occurrence(s) across the novel (min {min_occurrences})")
            else:
                addable.append(v)
    union_variants(existing, addable)

    if str(existing.get("translation", "")).strip().lower() == tr.strip().lower():
        return  # already known under the same translation

    prompt = fill(
        merge_template,
        {
            "existing_json": json.dumps(
                {k: existing.get(k) for k in ("source", "translation", "definition", "category")},
                ensure_ascii=False,
            ),
            "proposed_json": json.dumps(proposal, ensure_ascii=False),
            # convenience keys in case the template references them directly:
            "source": src,
            "translation": tr,
            "definition": df,
            "category": cat,
            "current_translation": str(existing.get("translation", "")),
            "current_definition": str(existing.get("definition", "")),
            "current_category": str(existing.get("category", "")),
        },
        "glossary_merge.md",
    )
    resp = _chat(project_dir, cfg, "glossary", prompt, json_schema=MERGE_SCHEMA,
                 chapter=chapter)
    merged = client.extract_json(resp)
    if not isinstance(merged, dict):
        raise ValueError("merge response is not a JSON object")

    updated = dict(existing)
    for key in ("translation", "definition", "category"):
        new_value = merged.get(key)
        if isinstance(new_value, str) and new_value.strip():
            if key == "category" and new_value not in glossary.CATEGORIES:
                # Same coercion as the new-term path, applied ONLY when the
                # model echoes a category: a merge that omits the field (or
                # returns garbage) falls through to the keep-existing branch
                # below and never warns, and a hand-edited illegal category
                # already on the entry is left untouched. Warns name the
                # PROPOSAL's source (the [ok] line below prints the same),
                # not the existing entry's (find() matches variants).
                print(f"{tag} [glossary] warn unknown category '{new_value}' for '{src}' - coerced to 'other'")
                updated["category"] = "other"
            else:
                updated[key] = new_value
        # absent (or garbage) values keep the existing entry's value

    # Same guide-only advisory as the new-term path, applied ONLY when the
    # merge actually lands: a 'unit' entry with a surviving translation
    # would never be counted or enforced by balance.check.
    if updated.get("category") == "unit":
        text = glossary.unit_translation_warning(src, updated.get("translation"))
        if text:
            print(f"{tag} [warn] {text}")

    terms = g.setdefault("terms", [])
    for idx, term in enumerate(terms):
        if term is existing or term == existing:
            terms[idx] = updated
            print(f"{tag} [ok] glossary ~ '{src}' -> '{updated.get('translation', '')}'")
            return
    glossary.upsert(g, updated)  # defensive: existing entry was not found in the list


def _cleanup_drift_signals(project_dir: Path, cfg: dict, tpl: str,
                           signals: list[dict], source_body: str,
                           tag: str, chapter: str | None = None
                           ) -> tuple[list[dict], dict | None]:
    """Ask the glossary job whether balance drift-signal terms deserve
    glossary entries; the mundane ones are slated for retirement. Runs on
    advisory drift signals, not gate failures. Returns (signals_to_keep,
    pending_cleanup): pending_cleanup is None when nothing is to be retired,
    else {"retirements": [{source, reason}], "kept_sources": [str]}. The
    CALLER applies the retirements only after the translation is accepted --
    retiring on a rejected attempt could delete good terms based on a bad
    translation. On any error keeps every signal, retires nothing."""
    try:
        term_list = "\n".join(
            f"- {s['source']} translates to \"{s['translation']}\" — source "
            f"{s['src_count']}x, canonical rendering {s['tgt_count']}x"
            for s in signals
        )
        sample_lines: list[str] = []
        seen: set[str] = set()
        for s in signals:
            taken = 0
            for line in source_body.split("\n"):
                if taken >= 2 or len(sample_lines) >= 12:
                    break
                stripped = line.strip()
                if stripped and s["source"] in line and stripped not in seen:
                    seen.add(stripped)
                    sample_lines.append(stripped)
                    taken += 1
        ctx = {
            "source_lang": _lang_name(cfg.get("source_lang", "")),
            "target_lang": _lang_name(cfg.get("target_lang", "")),
            "term_list": term_list,
            "sample_lines": "\n".join(sample_lines) if sample_lines else "(none)",
        }
        prompt = fill(tpl, ctx, "glossary_cleanup.md")
        resp = _chat(project_dir, cfg, "glossary", prompt, json_schema=CLEANUP_SCHEMA,
                     chapter=chapter)
        data = client.extract_json(resp)
        decisions = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(decisions, list):
            raise ValueError("expected a 'decisions' array")
        signal_sources = {s["source"] for s in signals}
        retirements: list[dict[str, str]] = []
        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("keep") is not False:
                continue
            src = decision.get("source")
            if not (isinstance(src, str) and src in signal_sources):
                continue
            if any(r["source"] == src for r in retirements):
                continue  # duplicate decision for one source
            reason = decision.get("reason")
            retirements.append({
                "source": src,
                "reason": reason if isinstance(reason, str) and reason.strip() else "mundane term",
            })
        if not retirements:
            return signals, None
        removed_set = {r["source"] for r in retirements}
        kept = [s for s in signals if s["source"] not in removed_set]
        return kept, {
            "retirements": retirements,
            "kept_sources": [s["source"] for s in signals],
        }
    except client.LLMFatal:
        # Irrecoverable provider code: no amount of keeping every signal makes
        # a run that cannot reach its provider succeed. The remaining `except`
        # keeps the fail-safe for shape errors and exhausted-but-retryable
        # calls, which are genuinely recoverable and still emit `degraded`.
        raise
    except Exception as exc:  # noqa: BLE001 - fail-safe: keep every signal
        logger.log_event(project_dir, {
            "event": "degraded", "chapter": chapter, "where": "glossary_cleanup",
            "reason": f"{type(exc).__name__}: {exc} - keeping all signals, "
                      "retiring nothing",
        })
        print(f"{tag} [warn] glossary cleanup failed - keeping all signals: {exc}")
        return signals, None


def _apply_pending_cleanup(project_dir: Path, pending: dict, chapter: str, tag: str) -> bool:
    """Apply retirement decisions deferred from BALANCE. Runs only after the
    FAITH gate accepted the translation; a rejected attempt's decisions are
    dropped, never written. Returns True when glossary.json changed (callers
    must reload their in-memory copy so a later save cannot resurrect the
    retired terms)."""
    removed = glossary.retire(project_dir, [r["source"] for r in pending["retirements"]])
    removed_set = set(removed)
    event: dict[str, Any] = {"event": "glossary_cleanup"}
    if chapter:
        event["chapter"] = chapter
    event["removed"] = [
        {"source": r["source"], "reason": r["reason"]}
        for r in pending["retirements"] if r["source"] in removed_set
    ]
    event["kept"] = [s for s in pending["kept_sources"] if s not in removed_set]
    logger.log_event(project_dir, event)
    for r in event["removed"]:
        print(f"{tag} [glossary] retired mundane term '{r['source']}' ({r['reason']})")
    return bool(removed)


def _line_output_cost(line: str) -> int:
    """Per-line TRANSLATE output cost: CJK chars x1.0, other chars /4, plus
    the numbered-JSON wrapper (~10 tokens per line). The chunk packer sums
    these; the +256 per-chunk overhead is added once per chunk.

    Conservative on purpose: English renderings run ~0.7 tokens per CJK
    char, so 1.0 keeps the cost above reality. Shares balance.CJK_RE with
    the balance checks so the chunker and the counters speak the same CJK
    class.
    """
    cjk = len(balance.CJK_RE.findall(line))
    return int(cjk * 1.0 + (len(line) - cjk) / 4) + 10


# One-shot config-note state for _note_token_cap: a multi-chapter run shares
# one config, so the ceiling note prints once per process.
_TOKEN_CAP_WARNED = False

# A translator block whose own max_tokens is below this cannot be packed
# into: room = floor(0.8 * cap) - 256 turns negative under 320, which closes a
# chunk on every line, and past (256 + per-line cost) _pack_chunks rejects the
# line outright and the chapter cannot translate at all.
MIN_TRANSLATOR_MAX_TOKENS = 8192


def _escalated_cap(pack_cap: int, provider_max: int, wire_cap: int) -> int:
    """Escalated cap for a truncating chunk's corrective retry.

    Three inputs, because the pipeline now keeps two caps apart:
    `pack_cap` sizes chunks, `wire_cap` is the ceiling handed to _chat (which
    client.chat lowers per block), and `provider_max` is the tightest block's
    own limit.

    The retry must never go BELOW the first attempt's cap -- retrying a
    truncated chunk at a smaller cap guarantees the same truncation and burns
    the attempt. The guard is keyed on `wire_cap`, the value the first attempt
    actually sent, NOT on `pack_cap`: when the ceiling binds (top level above
    the tightest block) those two differ, and keying on pack_cap turns a ~1.5x
    escalation into a halving. `provider_max` still bounds how far the
    escalation can reach on its own.

    Note that when provider_max <= wire_cap the escalation is a no-op: there is
    nothing above the ceiling to escalate into, which is correct -- the chunk
    already fits what every block can return.
    """
    return max(wire_cap, min(int(round(pack_cap * 1.5)), provider_max))


def _note_token_cap(provider_max: int, wire_cap: int, pack_cap: int) -> None:
    """Note once per run that the translator ceiling is bound by a block.

    A translator `max_tokens` below `translate_max_output_tokens` is no longer
    a misconfiguration: the block's own limit simply wins, and chunks are
    packed to fit it. But the user set the top-level number deliberately, so
    say once that it is not the number actually in force, and what is.
    """
    global _TOKEN_CAP_WARNED
    if _TOKEN_CAP_WARNED or provider_max >= wire_cap:
        return
    _TOKEN_CAP_WARNED = True
    print(f"[info] config: translation output cap is "
          f"{provider_max}, not translate_max_output_tokens ({wire_cap}) - "
          f"the tightest providers.translator block caps it there; chapters "
          f"pack to {pack_cap}")


def _check_translator_caps(blocks: list, wire_cap: int) -> None:
    """Warn once per run about two translator caps that cost output quality.

    1. A block below MIN_TRANSLATOR_MAX_TOKENS cannot be packed into at all
       -- _pack_chunks would fragment the chapter into one line per chunk and
       then reject lines outright. That is a hard error, not a tuning choice.

    2. A REASONING block that the ceiling lowers below its own declared
       max_tokens may stop converging. `reasoning_effort` draws from the same
       budget as the answer, so capping a `high`/`max` model too low can burn
       the whole cap thinking and return finish_reason=length with no content.
       This is measured, not hypothetical: Flash-Preview at `max` under a
       64k cap ran ~19m51s and returned nothing.

    Two numbers per block, deliberately. `declared` is what the block ASKS for
    and is the reference check 2 compares against -- the warning is about a
    reasoning model losing the room it declared. `cap` (config.block_cap) is
    what it will actually get once any max_tokens_limit binds, and it is what
    decides check 1: a chapter can only be packed into what the provider
    really returns.
    """
    low: list[str] = []
    squeezed: list[str] = []
    for b in blocks:
        declared = int(b.get("max_tokens") or config.DEFAULT_MAX_TOKENS)
        cap = config.block_cap(b)
        label = str(b.get("model") or b.get("base_url") or "?")
        if cap < MIN_TRANSLATOR_MAX_TOKENS:
            low.append(f"{label} @ {cap}")
        effort = (b.get("extra_body") or {}).get("reasoning_effort")
        effective = min(wire_cap, cap)
        # Compared against `declared`, NOT `cap`: with max_tokens_limit in play
        # cap IS the limit, so comparing to it would make the check self-
        # defeating and leave the worst case (a reasoning model pinned below
        # the budget it was measured to need) undiagnosed.
        if effort in ("high", "xhigh", "max") and effective < declared:
            squeezed.append(f"{label} ({effort}) @ {effective} of {declared}")
    if low:
        print(f"[warn] config: providers.translator output cap below "
              f"{MIN_TRANSLATOR_MAX_TOKENS} - chapters cannot be packed into "
              f"them: {', '.join(low)}")
    if squeezed:
        print(f"[warn] config: the output cap squeezes a reasoning translator "
              f"below its own max_tokens (via translate_max_output_tokens or "
              f"max_tokens_limit), which can stop it converging (reasoning "
              f"draws from the same budget as the answer): "
              f"{', '.join(squeezed)}")


def _pack_chunks(source_lines: list[str], pack_cap: int, escalated: int,
                 wire_cap: int) -> list[tuple[int, int, int]]:
    """Greedy per-line token-budget packing of the chapter into chunks.

    Chunks are sized by _line_output_cost (the per-line output cost),
    against budget = floor(0.8 * pack_cap) -- the 0.8 headroom absorbs
    estimate error -- minus the 256 per-chunk overhead: a chunk closes when
    the next line would overflow that room, and always takes >= 1 line (no
    empty chunks). `pack_cap` is min(translate_max_output_tokens, the
    tightest translator block's max_tokens), so a chunk is sized to what
    EVERY block in the array can return.

    A single line whose cost exceeds the whole budget cannot share a chunk
    with anything (guaranteed truncation), so it is isolated as a singleton
    called directly at the escalated cap.

    `pack_cap` and `wire_cap` are deliberately separate arguments: `pack_cap`
    is the SIZING budget, while the third tuple element -- the cap actually
    sent -- comes from `wire_cap`, the shared ceiling that client.chat then
    lowers per block. Collapsing them into one number is what let a chapter
    be packed for 256k and sent to a provider that can only return 128k.

    Deterministic given (source_lines, pack_cap, escalated, wire_cap): the
    same source and config always reproduce identical bounds, which the
    feedback slicing (_feedback_section's [lo, hi)) and crash resume rely on.

    Returns [(lo, hi, first_call_max_tokens), ...] with half-open line
    bounds. Raises ValueError naming the offending line when one line's
    estimated output cannot fit even the escalated cap (the caller turns it
    into normal TRANSLATE attempt feedback -- no LLM call is burned).
    """
    budget = pack_cap * 4 // 5  # floor(0.8 * pack_cap) headroom
    room = budget - 256
    plan: list[tuple[int, int, int]] = []
    lo = 0
    cost = 0
    for i, line in enumerate(source_lines):
        c = _line_output_cost(line)
        if c + 256 > escalated:
            raise ValueError(
                f"source line {i + 1} alone exceeds the output budget "
                f"(estimated {c} tokens > {escalated} cap); "
                "split or shorten the line manually"
            )
        if i > lo and cost + c > room:
            plan.append((lo, i, escalated if cost > budget else wire_cap))
            lo = i
            cost = c
        else:
            cost += c
    if lo < len(source_lines):
        plan.append((lo, len(source_lines),
                     escalated if cost > budget else wire_cap))
    return plan


def _response_cut(resp: str) -> bool:
    """Does an unparseable response look truncated (opened JSON, never
    closed it)? Drives the escalating corrective retry: a cut response gets
    one retry at a bumped max_tokens, while other shape problems retry at
    the same cap."""
    s = resp.strip()
    return bool(s) and s[-1] not in "}]"


def _chat(project_dir: Path, cfg: dict, job: str, prompt: str,
          json_schema: dict | None = None,
          max_tokens: int | None = None,
          chapter: str | None = None) -> str:
    """client.chat with per-project trace logging of the full exchange,
    fanned out over the job's provider blocks and merged by the consensus
    provider when there is more than one -- consensus.chat owns the trace
    logging and the fan-out.

    `chapter` is the chapter's file stem (or None); it is threaded explicitly
    so the exchange lands in that chapter's tier-2 bucket."""
    return consensus.chat(project_dir, cfg, job, prompt,
                          json_schema=json_schema, max_tokens=max_tokens,
                          chapter=chapter)


def _notes_report_line(tag: str, stem: str, kept: list[dict],
                       dropped: list[dict]) -> str:
    """TN_DEDUP summary line: kept notes broken down by category, drops by
    reason, and -- when anything was dropped -- a pointer to the
    notes/<stem>.dropped.json review artifact.

    Exact shapes: categories count non-zero entries in NOTE_CATEGORIES
    order ("wordplay 1, idiom 2, ..."; all-"other" keeps show "other K");
    a zero-kept chapter omits the parenthetical entirely. Drop reasons
    count in the fixed order low_threshold -> overflow -> invalid, labeled
    low-confidence / overflow / invalid ("3 low-confidence, 1 overflow").
    """
    counts = {category: 0 for category in tn.NOTE_CATEGORIES}
    for note in kept:
        category = note.get("category")
        counts[category if category in counts else "other"] += 1
    line = f"{tag} [ok] notes: {len(kept)} kept"
    if kept:
        line += " (" + ", ".join(
            f"{category} {count}"
            for category, count in counts.items() if count
        ) + ")"
    if dropped:
        labels = {"low_threshold": "low-confidence", "overflow": "overflow",
                  "invalid": "invalid"}
        reasons = {reason: 0 for reason in tn.DROP_REASONS}
        for entry in dropped:
            reason = entry.get("reason")
            if reason in reasons:
                reasons[reason] += 1
        line += (
            f"; {len(dropped)} dropped ("
            + ", ".join(
                f"{reasons[reason]} {labels[reason]}"
                for reason in tn.DROP_REASONS if reasons[reason]
            )
            + f") -> notes/{stem}.dropped.json"
        )
    return line


# --------------------------------------------------------------------------
# pipeline runner
# --------------------------------------------------------------------------


def _log_chapter_start(project_dir: Path, file: str, entry: dict,
                       force: bool, state: dict, lines: int) -> None:
    """Open the chapter's orchestration timeline, once the resume state and
    line count are known."""
    logger.log_event(project_dir, {
        "event": "chapter_start", "chapter": file,
        "number": int(entry.get("number", 0)),
        "lines": lines,
        "resume_stage": state.get("stage"),
        "resume_chunks": len(state.get("chunks") or []),
        "force": force,
    })


def _log_chapter_end(project_dir: Path, file: str, entry: dict, outcome: str,
                     state: dict, stages_run: list[str], attempts: int,
                     error: BaseException | None = None) -> None:
    """Close the chapter's timeline and index.jsonl, on the normal paths and
    on the crash path alike.

    calls/tokens/elapsed_s come from the logger's per-chapter counters, which
    accumulate before gating -- so they are non-zero even with
    log_orchestration off. A crash may have cut aggregation short; the index
    close line, not these numbers, is the reliable crash signal."""
    stats = logger.take_chapter_stats(project_dir, file)
    logger.log_event(project_dir, {
        "event": "chapter_end", "chapter": file,
        "number": int(entry.get("number", 0)),
        "outcome": outcome,
        "attempts": attempts,
        "stages_run": list(stages_run),
        "calls": stats["calls"],
        "tokens": stats["tokens"],
        "elapsed_s": stats["elapsed_s"],
        **({"error": f"{type(error).__name__}: {error}"} if error else {}),
    })
    logger.index_line(project_dir, file, "close", outcome=outcome,
                      attempts=attempts,
                      stages=len(stages_run), calls=stats["calls"],
                      tokens=stats["tokens"], elapsed_s=stats["elapsed_s"])
    # Metadata-only report, bounded to this run. Bodies only on demand, via
    # `translate logs --report --io`.
    from lib import logreport  # local: logreport imports logger, not pipeline
    logreport.write_run_report(project_dir, file,
                               int(entry.get("number", 0)),
                               logger.current_run_id(project_dir) or "")


def run_chapter(project_dir: Path, file: str, cfg: dict, force: bool = False) -> str:
    """Run the staged pipeline for one chapter.

    Returns "translated", "needs-review", or "skipped". Prints progress lines
    prefixed with "[CHAPTER_NNNN] ".

    The lifecycle wrapper: it resolves the manifest entry, takes the skip
    decision, opens chapter_start, and closes chapter_end on every exit path
    including a raise (outcome "crashed"), so a chapter never leaves an
    unclosed index line behind."""
    paths = project.paths(project_dir)
    manifest = project.load_manifest(project_dir)
    entry = project.find_entry(manifest, file)
    if entry is None:
        raise PipelineError(f"{file}: no manifest entry (run 'init' or 'status' first)")
    tag = f"[CHAPTER_{int(entry['number']):04d}]"

    if entry.get("status") == "translated" and not force:
        print(f"{tag} [ok] already translated - skipped")
        return "skipped"

    # The index open goes in BEFORE the worker runs: index.jsonl is the
    # crash signal, so it must exist even when the worker dies before it can
    # report a line count. The tier-1 chapter_start follows inside the worker,
    # where the resume state and line count are known.
    logger.index_line(project_dir, file, "open", command="translate",
                      file=file, number=int(entry.get("number", 0)))
    logger.note_chapter_start(project_dir, file)
    try:
        outcome, state, stages_run, attempts_run = _run_chapter(
            project_dir, file, cfg, force, paths, manifest, entry, tag)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
        _log_chapter_end(project_dir, file, entry, "crashed", {}, [], 0, exc)
        raise
    _log_chapter_end(project_dir, file, entry, outcome, state, stages_run,
                     attempts_run)
    return outcome


def _run_chapter(project_dir: Path, file: str, cfg: dict, force: bool,
                 paths: dict, manifest: list[dict], entry: dict,
                 tag: str) -> tuple[str, dict, list[str]]:
    """The staged pipeline proper. Returns (outcome, state, stages_run) so
    the lifecycle wrapper can report them; run_chapter owns the logging."""
    stem = Path(file).stem
    # Stages this invocation actually entered, in order. A resumed chapter
    # enters fewer than the full set, and the log says so.
    stages_run: list[str] = []
    # Attempts this invocation actually ran, counted at the top of the loop.
    # Derived from state["attempt"] instead it would be wrong: that counter
    # increments only on FAILURE, so it reads 0 after a clean first pass and
    # 3 after three failures -- neither is the number of attempts run.
    attempts_run = 0

    if force:
        for artifact in (f"{stem}.state.json", f"{stem}.md", f"{stem}.lines.json"):
            (paths["draft"] / artifact).unlink(missing_ok=True)
        (paths["translated"] / file).unlink(missing_ok=True)
        (tn.notes_path(project_dir, file)).unlink(missing_ok=True)
        (tn.dropped_path(project_dir, file)).unlink(missing_ok=True)
        print(f"{tag} [init] force: removed previous draft and translated artifacts")

    state = load_state(paths["draft"], file)
    if state is None:
        state = {
            "stage": "TRANSLATE",
            "attempt": 0,
            "feedback": [],
            "title": None,
            "lines": None,
            # Per-chunk TRANSLATE persistence: completed chunk translations
            # (list of line lists) while TRANSLATE is in flight; popped once
            # the full lines list lands (see the TRANSLATE stage).
            "chunks": None,
            "notes": None,
            "rejected": None,
            "updated_at": "",
            "pipeline": STATE_VERSION,
        }
    state.setdefault("stage", "TRANSLATE")
    state.setdefault("attempt", 0)
    state.setdefault("feedback", [])
    state.setdefault("title", None)
    state.setdefault("lines", None)
    state.setdefault("chunks", None)
    state.setdefault("notes", None)
    state.setdefault("rejected", None)

    if state["stage"] not in STAGES:
        state["stage"] = "TRANSLATE"
    if state.get("pipeline") != STATE_VERSION:
        # v1 state written before the FAITH/GLOSSARY_EXPAND reorder: a
        # persisted GLOSSARY_EXPAND or FAITH stage means the faithfulness
        # gate had not completed. Remap to FAITH so the resumed draft is
        # always faith-checked (expand re-running afterwards is harmless).
        if state["stage"] in ("GLOSSARY_EXPAND", "FAITH"):
            state["stage"] = "FAITH"
        state["pipeline"] = STATE_VERSION
    if _STAGE_IDX[state["stage"]] > _STAGE_IDX["TRANSLATE"] and (
        state["lines"] is None or state["title"] is None
    ):
        # A stage past TRANSLATE needs the previous translation; without one
        # there is nothing to resume - start over from TRANSLATE.
        state["stage"] = "TRANSLATE"
    if _STAGE_IDX[state["stage"]] > _STAGE_IDX["TRANSLATE"]:
        print(f"{tag} [init] resuming at stage {state['stage']} (attempt {state['attempt']})")
    else:
        state["lines"] = None
        # Keep the title stashed alongside resumable chunks (a crash resume
        # mid-TRANSLATE reuses it); without chunks the attempt starts clean.
        if not (isinstance(state.get("chunks"), list) and state["chunks"]):
            state["title"] = None

    project.set_status(manifest, file, "in-progress")
    project.save_manifest(project_dir, manifest)

    source_path = paths["source"] / file
    if not source_path.is_file():
        raise PipelineError(f"source chapter missing: {source_path}")
    fm, body = project.read_chapter(source_path)
    source_lines = body.split("\n")
    # When the body opens by repeating the frontmatter chapter_title, models
    # see the title twice (title instruction + body line 1) and emit an empty
    # or dropped first line. Drop the redundant body copy; the translated
    # title lives in the frontmatter "title" field.
    source_lines, dropped_title = project.drop_leading_chapter_title(source_lines, fm)
    if dropped_title:
        print(f"{tag} [init] leading chapter-title line handled via the title field")
    # A content-free source would flow through chunking (no chunks), skip
    # every LLM call, and assemble a structurally valid but empty
    # translation: mark it needs-review instead of translating nothing.
    if not any(line.strip() for line in source_lines):
        print(f"{tag} [warn] {file}: source chapter has no content - marked needs-review")
        project.set_status(manifest, file, "needs-review")
        project.save_manifest(project_dir, manifest)
        return "needs-review", state, stages_run, attempts_run
    body_for_counts = "\n".join(source_lines)
    chapter_order = int(entry.get("order", 0))
    _log_chapter_start(project_dir, file, entry, force, state, len(source_lines))

    templates_dir = paths["templates"]
    tpl_translation = _load_template(templates_dir, "translation.md")
    tpl_glossary_expand = _load_template(templates_dir, "glossary_expand.md")
    tpl_glossary_merge = _load_template(templates_dir, "glossary_merge.md")
    tpl_glossary_cleanup = _load_template(templates_dir, "glossary_cleanup.md")
    tpl_faithfulness = _load_template(templates_dir, "faithfulness.md")
    tpl_tn_generate = _load_template(templates_dir, "tn_generate.md")

    max_attempts = int(_cfg_value(cfg, "max_attempts"))
    g = glossary.load(project_dir)

    # Novel-level context: the project's style.md (copied from a preset at
    # init, hand-editable) wins; the legacy style_profile from --style auto
    # follows; generic default last. Background likewise prefers the
    # top-level novel_info field over the legacy profile field.
    novel_info = project.load_novel_info(project_dir)
    style_summary, _style_tier = styles.resolve_style(project_dir, novel_info)
    style_profile = novel_info.get("style_profile")
    style_profile = style_profile if isinstance(style_profile, dict) else {}
    novel_background = str(
        novel_info.get("background") or style_profile.get("background") or ""
    ).strip()

    # Rolling story recap (advisory cross-chapter plot context): resolve the
    # recap to inject BEFORE the attempt loop -- a state hit costs no LLM
    # call, and the one-call backfill self-heals a predecessor translated
    # before the feature existed. Any failure degrades to "" (no recap).
    prev_recap = story.ensure_recap(project_dir, cfg, manifest, file, tag)
    recap_section = story.story_part(prev_recap)

    def advance(next_stage: str) -> None:
        state["stage"] = next_stage
        save_state(paths["draft"], file, state)

    def stage_enter(stage: str) -> float:
        """Record a stage transition and start its clock. Returns the start
        time for stage_exit.

        Called just inside the stage's resume guard, so a resumed chapter
        enters only the stages it actually reaches and emits no TRANSLATE
        event."""
        if stage not in stages_run:
            stages_run.append(stage)
        started = time.monotonic()
        logger.log_event(project_dir, {
            "event": "stage", "chapter": file, "stage": stage, "phase": "begin",
            "attempt": int(state.get("attempt", 0) or 0) + 1})
        return started

    def stage_exit(stage: str, started: float) -> None:
        """Close a stage transition. Reached only when the stage's body ran
        to completion -- a stage that raises out of the pipeline deliberately
        (PipelineError) leaves no end event, because it did not end, and the
        crash is recorded by chapter_end and the index close line."""
        logger.log_event(project_dir, {
            "event": "stage", "chapter": file, "stage": stage, "phase": "end",
            "attempt": int(state.get("attempt", 0) or 0) + 1,
            "elapsed_s": round(time.monotonic() - started, 2)})

    def log_gate(stage: str, verdict: str, reasons: list[str]) -> None:
        """A gate verdict with the COMPLETE reason list -- attempt_failed
        truncates feedback to the last three, so without this the earlier
        reasons exist nowhere in the log."""
        logger.log_event(project_dir, {
            "event": "gate", "chapter": file, "stage": stage,
            "verdict": verdict, "reasons": list(reasons)})

    def log_result(kind: str, **fields: Any) -> None:
        """The pipeline's reading of ONE model output: the verdict it
        accepted, the terms it proposed, the notes it kept. Tier 2, and
        unconditional -- this is what you want when model logging is off."""
        logger.log_event(project_dir, {"event": "result", "chapter": file,
                                       "kind": kind, **fields})

    def log_chunk(k: int, total: int, lo: int, hi: int, max_tokens: int,
                  attempt: int, outcome: str, problem: str = "") -> None:
        """Per-chunk bounds and outcome, so a multi-part chapter's packing
        and each part's corrective retry are readable without diffing
        prompts."""
        logger.log_event(project_dir, {
            "event": "chunk", "chapter": file, "index": k + 1, "total": total,
            "lo": lo + 1, "hi": hi, "max_tokens": max_tokens,
            "attempt": attempt, "outcome": outcome,
            **({"problem": problem} if problem else {}),
        })

    def log_feedback(stage: str) -> None:
        """The COMPLETE accumulated feedback history for this attempt, not
        the last three entries attempt_failed carries."""
        logger.log_event(project_dir, {
            "event": "feedback", "chapter": file, "stage": stage,
            "reasons": list(state["feedback"])})

    def balance_signals_section() -> str:
        """Render kept balance drift signals for the FAITH reviewer; empty
        when the counter found nothing worth flagging."""
        if not balance_signals:
            return ""
        return (
            "[Term Consistency Signals]\n"
            "Heuristic flags from the glossary balance script — the canonical "
            "rendering below was not found in the translation:\n"
            + "\n".join(f"- {item}" for item in balance_signals)
        )

    def build_ctx(translation_lines: list[str], feedback: str = "",
                  extra: dict[str, str] | None = None) -> dict[str, str]:
        ctx: dict[str, str] = {
            "source_lang": _lang_name(cfg.get("source_lang", "")),
            "target_lang": _lang_name(cfg.get("target_lang", "")),
            "chapter_title": str(fm.get("chapter_title", "")),
            "chapter_file": file,
            "line_count": str(len(source_lines)),
            "source_lines": json.dumps(source_lines, ensure_ascii=False),
            "translation_lines": json.dumps(translation_lines, ensure_ascii=False),
            "glossary": glossary_str,
            "feedback_section": feedback,
            "balance_signals_section": "",
            "style": style_summary,
            "background_section": background_section(novel_background, recap_section),
        }
        if extra:
            ctx.update(extra)
        return ctx

    while True:
        attempts_run += 1
        # Recomputed every attempt so retries benefit from the expanded glossary.
        pairs = glossary.contextual(g, body_for_counts, int(_cfg_value(cfg, "contextual_glossary_cap")))
        glossary_str = glossary.render_contextual(pairs)
        failed_stage: str | None = None
        # Resume point for this attempt: skip stages a previous run already
        # completed before the persisted stage (crash resume). After a failed
        # gate the failure handler resets the stage to TRANSLATE, so in-process
        # retries always restart at TRANSLATE with the accumulated feedback.
        start_idx = _STAGE_IDX.get(state["stage"], _STAGE_IDX["TRANSLATE"])

        # ---------------- TRANSLATE ----------------
        if start_idx <= _STAGE_IDX["TRANSLATE"]:
            _t0 = stage_enter("TRANSLATE")
            try:
                print(f"{tag} [init] TRANSLATE (attempt {state['attempt'] + 1})")
                logger.log_event(project_dir, {"event": "attempt", "chapter": file,
                                    "attempt": state["attempt"] + 1})
                # Whole-chapter translation by default: the model sees the
                # novel's full context, which beats fragmenting it. Only the
                # OUTPUT is constrained: when the expected translated output
                # exceeds the packing budget, the chapter splits into
                # token-budget-packed parts (each still carrying style
                # background and the previous part's tail; the numbered-line
                # protocol and the corrective retry keep the line contract
                # either way).
                #
                # Two caps, not one. `translate_max_output_tokens` is a
                # CEILING the pipeline asks for; a provider block's own
                # max_tokens is a CEILING the provider accepts, and it wins
                # where it is lower (client.chat clamps per block). So:
                #   pack_cap  = what EVERY block can return -> sizing
                #   wire_cap  = the shared ceiling -> sent, then lowered per block
                max_out = int(_cfg_value(cfg, "translate_max_output_tokens"))
                translator_blocks = config.provider_list(cfg, "translator")
                # Model arrays: the first block no longer speaks for the
                # whole job -- a block's own cap is a hard limit the
                # pipeline must not raise, so pack against the SMALLEST across
                # blocks. A block that omits max_tokens contributes
                # DEFAULT_MAX_TOKENS, which is deliberate: an omission is
                # "unset", not "unlimited", and silently treating it as
                # unlimited would let one forgotten key size the whole job.
                # block_cap() folds in any max_tokens_limit, so a block whose
                # provider refuses above N packs to N even when the block
                # asked for more -- otherwise parts would be sized for a
                # response the provider will truncate.
                provider_max = min(
                    config.block_cap(b) for b in translator_blocks
                )
                _check_translator_caps(translator_blocks, wire_cap=max_out)
                pack_cap = min(max_out, provider_max)
                _note_token_cap(provider_max, max_out, pack_cap)
                escalated = _escalated_cap(pack_cap, provider_max, max_out)
                plan = _pack_chunks(source_lines, pack_cap, escalated,
                                    max_out)
                n_chunks = len(plan)
                src_total = len(source_lines)
                # Per-chunk persistence (crash resume): validated chunk
                # translations were appended to state["chunks"] as they
                # completed. Packing is deterministic, so the recomputed
                # bounds line up with the saved ones -- unless the source or
                # config changed between runs, in which case the saved
                # chunks are unusable and the chapter restarts from scratch.
                completed = [c for c in (state.get("chunks") or [])
                             if isinstance(c, list)]
                if (len(completed) > n_chunks
                        or any(len(completed[i]) != plan[i][1] - plan[i][0]
                               or not all(isinstance(ln, str) for ln in completed[i])
                               for i in range(len(completed)))):
                    print(f"{tag} [warn] saved chunks do not match the current packing "
                          "(source or config changed?) - retranslating from scratch")
                    completed = []
                tlines: list[str] = [ln for c in completed for ln in c]
                title: str | None = None
                if completed:
                    # The title stashed by earlier chunks survives the resume.
                    prev_title = state.get("title")
                    if isinstance(prev_title, str) and prev_title:
                        title = prev_title
                if 0 < len(completed) < n_chunks:
                    print(f"{tag} [init] resuming translation at part "
                          f"{len(completed) + 1}/{n_chunks} "
                          f"({len(tlines)} lines already done)")
                for k in range(len(completed), n_chunks):
                    lo, hi, call_max_tokens = plan[k]
                    chunk = source_lines[lo:hi]
                    expected = list(range(lo + 1, hi + 1))
                    if n_chunks > 1:
                        print(f"{tag} [init] translating part {k + 1}/{n_chunks} (lines {lo + 1}-{hi})")
                    numbered = [{"i": lo + j + 1, "t": ln} for j, ln in enumerate(chunk)]
                    if k == 0 or not tlines:
                        chunk_background = background_section(novel_background, recap_section)
                    else:
                        tail = [ln for ln in tlines if ln.strip()][-2:]
                        continuity = (
                            "The final lines of the previous part, already translated "
                            "(for continuity only - do NOT retranslate them):\n"
                            + "\n".join(tail)
                        )
                        chunk_background = background_section(
                            novel_background, recap_section, continuity.strip()
                        )
                    chunk_ctx = build_ctx(
                        [],
                        feedback=_feedback_section(state["feedback"], state["rejected"], lo, hi),
                        extra={
                            "source_lines": json.dumps(numbered, ensure_ascii=False),
                            "line_count": str(len(chunk)),
                            "background_section": chunk_background,
                            "chunk_info": (
                                f"You are translating part {k + 1} of {n_chunks} of one chapter "
                                f"(source lines {lo + 1}-{hi} of {src_total}). Other parts are "
                                "translated separately: translate ONLY the lines below - no "
                                "recap of earlier parts, no continuation into later parts."
                                if n_chunks > 1
                                else ""
                            ),
                        },
                    )
                    prompt = fill(tpl_translation, chunk_ctx, "translation.md")
                    clines: list[str] | None = None
                    for chunk_attempt in (1, 2):  # one corrective retry per chunk
                        resp = _chat(
                            project_dir, cfg, "translator", prompt,
                            json_schema=TRANSLATION_SCHEMA,
                            max_tokens=call_max_tokens,
                            chapter=file,
                        )
                        parse_note = ""
                        try:
                            data = client.extract_json(resp)
                        except client.LLMError as exc:
                            data = None
                            parse_note = f" ({exc}; raw response starts: {resp[:120]!r})"
                        got = data.get("lines") if isinstance(data, dict) else None
                        ctitle = data.get("title") if isinstance(data, dict) else None
                        problem = ""
                        # Truncation signature (drives the escalating retry):
                        # missing line indices, or a response cut mid-JSON.
                        truncated = data is None and _response_cut(resp)
                        if not isinstance(ctitle, str) or not isinstance(got, list):
                            problem = (
                                "response was not a JSON object with a 'title' string and a "
                                "'lines' array" + parse_note
                            )
                        elif all(isinstance(x, dict) and isinstance(x.get("i"), int)
                                 and not isinstance(x.get("i"), bool)
                                 and isinstance(x.get("t"), str) for x in got):
                            by_idx: dict[int, list[str]] = {}
                            for x in got:
                                by_idx.setdefault(x["i"], []).append(x["t"])
                            present = set(by_idx)
                            missing = [i for i in expected if i not in present]
                            dupes = sorted(str(i) for i, v in by_idx.items() if len(v) > 1)
                            outside = sorted(str(i) for i in present - set(expected))
                            if not missing and not dupes and not outside:
                                clines = [by_idx[i][0] for i in expected]
                            else:
                                bits = []
                                if missing:
                                    truncated = True
                                    bits.append("missing line(s) " + ", ".join(map(str, missing)))
                                if dupes:
                                    bits.append("duplicated line index(es) " + ", ".join(dupes))
                                if outside:
                                    bits.append("out-of-range index(es) " + ", ".join(outside))
                                problem = "; ".join(bits)
                        elif all(isinstance(x, str) for x in got):
                            if len(got) == len(chunk):
                                clines = list(got)  # plain-string response, aligned
                            else:
                                problem = (
                                    f"expected {len(chunk)} lines, the response had {len(got)}"
                                )
                        else:
                            problem = (
                                'each lines entry must be {"i": <source line number>, '
                                '"t": "<translation>"}'
                            )
                        if clines is not None:
                            if title is None:
                                title = ctitle
                            break
                        problem = (
                            f"part {k + 1} of {n_chunks} covers source lines {lo + 1}-{hi}: "
                            + problem
                            + '. Return exactly one {"i", "t"} object per input line, '
                            "echoing each input line's i."
                        )
                        if chunk_attempt == 1:
                            if truncated:
                                # The response looks cut off: retry once at
                                # the escalated cap (>= the first attempt's).
                                call_max_tokens = escalated
                            retry_ctx = dict(chunk_ctx)
                            retry_ctx["feedback_section"] = (
                                "NOTE: the previous response for this part was rejected. Fix "
                                "the issue and translate these lines again from scratch:\n- "
                                + problem
                            )
                            prompt = fill(tpl_translation, retry_ctx, "translation.md")
                        else:
                            log_chunk(k, n_chunks, lo, hi, call_max_tokens,
                                      chunk_attempt, "failed", problem)
                            raise ValueError(f"TRANSLATE {problem}")
                    log_chunk(k, n_chunks, lo, hi, call_max_tokens,
                              chunk_attempt, "ok")
                    tlines.extend(clines)
                    # Persist the validated chunk immediately: a crash or
                    # Ctrl-C loses at most the in-flight chunk. The title
                    # rides along so a resume never re-burns a call for it.
                    completed.append(clines)
                    state["chunks"] = completed
                    if title is not None:
                        state["title"] = title
                    save_state(paths["draft"], file, state)
                state.pop("chunks", None)  # the full lines list is authoritative
                state["title"] = title or ""
                state["lines"] = tlines
                project.write_chapter(
                    paths["draft"] / f"{stem}.md", {**fm, "title": title}, "\n".join(tlines)
                )
                (paths["draft"] / f"{stem}.lines.json").write_text(
                    json.dumps({"title": title, "lines": tlines}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n",
                )
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - becomes retry feedback
                state["feedback"].append(f"TRANSLATE failed: {type(exc).__name__}: {exc}")
                failed_stage = "TRANSLATE"
            stage_exit("TRANSLATE", _t0)

        lines: list[str] = []
        # Kept balance drift signals for the FAITH reviewer; resets every
        # attempt and stays empty when resuming past BALANCE.
        balance_signals: list[str] = []
        # Retirement decisions deferred from BALANCE (applied only after
        # FAITH accepts; dropped when the attempt is rejected).
        pending_cleanup: dict | None = None
        if failed_stage is None:
            lines = list(state["lines"] or [])

        if failed_stage is None and start_idx <= _STAGE_IDX["VALIDATE"]:
            _t0 = stage_enter("VALIDATE")
            advance("VALIDATE")
            # ---------------- VALIDATE ----------------
            issues: list[str] = []
            if len(lines) != len(source_lines):
                issues.append(
                    f"line count mismatch: source has {len(source_lines)} lines "
                    f"but translation has {len(lines)}"
                )
            else:
                for i, (src, dst) in enumerate(zip(source_lines, lines), start=1):
                    if not src.strip() and dst.strip():
                        issues.append(f"Line {i}: source line is empty but translation is not")
                    elif src.strip() and not dst.strip():
                        issues.append(f"Line {i}: translation line is empty but source is not")
            if issues:
                state["feedback"].extend(issues)
                log_gate("VALIDATE", "fail", issues)
                failed_stage = "VALIDATE"
            stage_exit("VALIDATE", _t0)

        if failed_stage is None and start_idx <= _STAGE_IDX["BALANCE"]:
            _t0 = stage_enter("BALANCE")
            advance("BALANCE")
            # ---------------- BALANCE (advisory) ----------------
            # All three tiers (drift signals, usage floor, over-count
            # ceiling) are advisory counting heuristics: none of them can
            # fail a chapter on its own. Drift signals run through the
            # glossary cleanup judgment, and whatever survives is handed to
            # the FAITH reviewer, which owns the verdict.
            try:
                drift_signals, warnings, over_count = balance.check(
                    pairs,
                    lines,
                    _cfg_value(cfg, "min_term_coverage"),
                    _cfg_value(cfg, "fuzzy_max_distance"),
                )
                if drift_signals or warnings or over_count:
                    logger.log_event(project_dir, {
                        "event": "balance_advisory", "chapter": file,
                        "drift_signals": [f["message"] for f in drift_signals],
                        "warnings": warnings, "over_count": over_count,
                    })
                if warnings:
                    for warning in warnings[:5]:
                        print(f"{tag} [warn] balance advisory: {warning}")
                    if len(warnings) > 5:
                        print(f"{tag} [warn] ... and {len(warnings) - 5} more (see logs)")
                if drift_signals:
                    if _cfg_value(cfg, "glossary_auto_cleanup"):
                        # Mundane glossary entries trip the drift check by
                        # being naturally rephrased; ask the glossary job
                        # whether each flagged term deserves enforcement.
                        # The retirement itself is DEFERRED to the accepted
                        # attempt (applied alongside GLOSSARY_EXPAND) -- this
                        # split only filters which signals reach the FAITH
                        # reviewer, which owns the verdict.
                        drift_signals, pending_cleanup = _cleanup_drift_signals(
                            project_dir, cfg, tpl_glossary_cleanup,
                            drift_signals, body_for_counts, tag, chapter=file,
                        )
                    for f in drift_signals:
                        print(f"{tag} [warn] balance drift signal: {f['message']}")
                    balance_signals = [f["message"] for f in drift_signals]
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - advisory: never block the chapter
                print(f"{tag} [warn] balance check failed - continuing: {type(exc).__name__}: {exc}")
                logger.log_event(project_dir, {
                    "event": "balance_advisory", "chapter": file,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            stage_exit("BALANCE", _t0)

        if failed_stage is None and start_idx <= _STAGE_IDX["FAITH"]:
            _t0 = stage_enter("FAITH")
            advance("FAITH")
            # ---------------- FAITH ----------------
            try:
                prompt = fill(
                    tpl_faithfulness,
                    build_ctx(lines, extra={"balance_signals_section": balance_signals_section()}),
                    "faithfulness.md",
                )
                print(f"{tag} [init] FAITH")
                resp = _chat(project_dir, cfg, "reviewer", prompt,
                              json_schema=VERDICT_SCHEMA, chapter=file)
                data = client.extract_json(resp)
                verdict = data.get("verdict") if isinstance(data, dict) else None
                reasons = data.get("reasons") if isinstance(data, dict) else None
                if verdict != "SUCCESS":
                    fb = (
                        [str(r) for r in reasons if str(r).strip()]
                        if isinstance(reasons, list)
                        else []
                    )
                    if not fb:
                        fb = ["faithfulness review rejected the translation without giving reasons"]
                    state["feedback"].extend(fb)
                    log_gate("FAITH", "fail", fb)
                    log_result("faithfulness", verdict=verdict,
                               reasons=fb, accepted=False)
                    failed_stage = "FAITH"
                else:
                    log_gate("FAITH", "pass", [])
                    log_result("faithfulness", verdict=verdict,
                               reasons=[], accepted=True)
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - becomes retry feedback
                state["feedback"].append(f"FAITH review failed: {type(exc).__name__}: {exc}")
                log_gate("FAITH", "error", [f"{type(exc).__name__}: {exc}"])
                failed_stage = "FAITH"
            stage_exit("FAITH", _t0)

        if failed_stage is None and start_idx <= _STAGE_IDX["GLOSSARY_EXPAND"]:
            _t0 = stage_enter("GLOSSARY_EXPAND")
            advance("GLOSSARY_EXPAND")
            # ---------------- GLOSSARY_EXPAND (non-fatal) ----------------
            # Runs only on the attempt FAITH just accepted: new terms lock in
            # after the translation is accepted, never from a rejected one.
            # Deferred BALANCE retirements land here too (a crash-resume
            # entering at this stage finds no pending decisions -- nothing is
            # retired, the fail-safe direction).
            if pending_cleanup is not None:
                if _apply_pending_cleanup(project_dir, pending_cleanup, file, tag):
                    # retire() rewrote glossary.json; refresh so this stage's
                    # save cannot resurrect the retired terms.
                    g = glossary.load(project_dir)
                pending_cleanup = None
            try:
                max_terms = int(_cfg_value(cfg, "max_new_terms_per_chapter"))
                prompt = fill(
                    tpl_glossary_expand,
                    build_ctx(lines, extra={"max_terms": str(max_terms)}),
                    "glossary_expand.md",
                )
                print(f"{tag} [init] GLOSSARY_EXPAND")
                resp = _chat(project_dir, cfg, "glossary", prompt,
                              json_schema=TERMS_SCHEMA, chapter=file)
                data = client.extract_json(resp)
                raw_terms = data.get("terms") if isinstance(data, dict) else None
                if not isinstance(raw_terms, list):
                    raise ValueError("expected a 'terms' array")
                eligible = raw_terms[:max_terms]
                min_occ = int(_cfg_value(cfg, "min_term_occurrences"))
                corpus = ""
                if min_occ > 0 and eligible:
                    try:
                        corpus = _gate_corpus(project_dir)
                    except Exception as exc:  # noqa: BLE001 - fail open: expansion is auxiliary
                        print(f"{tag} [warn] occurrence gate disabled - source corpus unreadable: {type(exc).__name__}: {exc}")
                        min_occ = 0
                applied_terms: list[str] = []
                skipped_terms: list[str] = []
                for proposal in eligible:
                    try:
                        _apply_glossary_proposal(g, proposal, chapter_order, cfg, tpl_glossary_merge, tag, project_dir,
                                                 corpus=corpus, min_occurrences=min_occ,
                                                 chapter=file)
                        source = (proposal.get("source")
                                  if isinstance(proposal, dict) else None)
                        applied_terms.append(str(source))
                    except PipelineError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - skip this proposal only
                        print(f"{tag} [warn] skipped glossary proposal: {exc}")
                        skipped_terms.append(
                            f"{proposal.get('source') if isinstance(proposal, dict) else '?'}: {exc}")
                log_result("glossary_expand", proposed=len(raw_terms),
                           eligible=len(eligible), applied=applied_terms,
                           skipped=skipped_terms)
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - glossary expansion is auxiliary
                print(f"{tag} [warn] glossary expansion failed: {type(exc).__name__}: {exc}")
            stage_exit("GLOSSARY_EXPAND", _t0)
            glossary.save(project_dir, g)

        if failed_stage is None and start_idx <= _STAGE_IDX["TN_GENERATE"]:
            _t0 = stage_enter("TN_GENERATE")
            advance("TN_GENERATE")
            # ---------------- TN_GENERATE (parse failures non-fatal) ----------------
            try:
                max_notes = int(_cfg_value(cfg, "max_notes_per_chapter"))
                prompt = fill(
                    tpl_tn_generate,
                    build_ctx(lines, extra={"max_notes": str(max_notes)}),
                    "tn_generate.md",
                )
                print(f"{tag} [init] TN_GENERATE")
                resp = _chat(project_dir, cfg, "annotator", prompt,
                              json_schema=NOTES_SCHEMA, chapter=file)
                data = client.extract_json(resp)
                raw_notes = data.get("notes") if isinstance(data, dict) else None
                if not isinstance(raw_notes, list):
                    raise ValueError("expected a 'notes' array")
                state["notes"] = raw_notes
                log_result("tn_generate", notes_returned=len(raw_notes))
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - notes are optional
                print(f"{tag} [warn] note generation failed - continuing without notes: {exc}")
                state["notes"] = []
            stage_exit("TN_GENERATE", _t0)

        if failed_stage is None and start_idx <= _STAGE_IDX["TN_DEDUP"]:
            _t0 = stage_enter("TN_DEDUP")
            advance("TN_DEDUP")
            # ---------------- TN_DEDUP ----------------
            # max_notes is enforced HERE (not by trusting the model): the
            # prompt asks for at most max_notes severity-ordered entries,
            # and the cap truncates whatever actually came back. The cut
            # tail, the low-threshold discards, and invalid entries land in
            # notes/<stem>.dropped.json for review.
            kept_notes, history, warnings, dropped = tn.process(
                state["notes"] or [],
                len(lines),
                chapter_order,
                tn.load_history(project_dir),
                int(_cfg_value(cfg, "tn_gap_chapters")),
                bool(_cfg_value(cfg, "tn_keep_low_confidence")),
                max_notes=int(_cfg_value(cfg, "max_notes_per_chapter")),
            )
            tn.save_history(project_dir, history)
            state["notes"] = kept_notes
            for warning in warnings:
                print(f"{tag} [warn] {warning}")
            tn.save_dropped(project_dir, file, dropped)
            print(_notes_report_line(tag, stem, kept_notes, dropped))
            log_result("tn_dedup", notes_returned=len(state["notes"] or []),
                       kept=len(kept_notes), dropped=len(dropped))
            stage_exit("TN_DEDUP", _t0)

        if failed_stage is None:
            # ---------------- ASSEMBLE ----------------
            _t0 = stage_enter("ASSEMBLE")
            advance("ASSEMBLE")
            out_path = paths["translated"] / file
            assemble.assemble(out_path, fm, state["title"] or "", list(lines))
            # Notes live in the sidecar now, not in the markdown; save_notes
            # validates line indexes against the same lines list assemble
            # joined and drops stale entries with a warning.
            tn.save_notes(project_dir, file, list(lines), list(state["notes"] or []))
            (paths["draft"] / f"{stem}.state.json").unlink(missing_ok=True)
            project.set_status(manifest, file, "translated")
            try:
                project.save_manifest(project_dir, manifest)
            except OSError as exc:
                # The chapter file is already written; losing the manifest
                # flip must not demote a finished translation (run_range's
                # catch-all would re-mark it needs-review). The in-memory
                # manifest keeps "translated" for the rest of the run.
                print(f"[warn] manifest update failed for {file}: {exc} - "
                      "chapter file is written; status stays in-progress")
            print(
                f"{tag} [ok] translated -> {out_path.name} "
                f"(title: {state['title']}, notes: {len(state['notes'] or [])})"
            )
            # Rolling recap: record this chapter's own entry after assembly
            # (run_range's chapter commit versions story_state.json with the
            # chapter). Its failure must never affect the translated outcome
            # -- the helper swallows every exception internally.
            story.record_recap(project_dir, cfg, file,
                               state["title"] or "", "\n".join(lines),
                               prev_recap, tag)
            stage_exit("ASSEMBLE", _t0)
            return "translated", state, stages_run, attempts_run

        # ---------------- failure handling ----------------
        assert failed_stage is not None
        state["attempt"] = int(state["attempt"]) + 1
        # Persist TRANSLATE, not the failed stage: the next action is a
        # re-translation. A persisted failed-gate stage combined with
        # non-null lines would make a crash+resume re-validate the already
        # rejected lines and burn attempts without ever re-translating.
        state["stage"] = "TRANSLATE"
        if failed_stage != "TRANSLATE":
            # A gate-rejected attempt retranslates the whole chapter (the
            # rejected snapshot covers all lines), so per-chunk resume state
            # must not survive into the retry. A TRANSLATE-stage failure
            # keeps its chunks: the validated parts are exactly what a
            # crash resume would reuse.
            state["chunks"] = []
        if lines:
            # Snapshot the translation the gate just rejected so the retry
            # prompt can show it, not just the feedback bullets. Kept when a
            # later attempt dies in TRANSLATE (the last gate-judged
            # translation stays the most useful reference); cleared
            # implicitly when ASSEMBLE deletes the state file.
            state["rejected"] = list(lines)
        save_state(paths["draft"], file, state)
        log_feedback(failed_stage)
        logger.log_event(project_dir, {"event": "attempt_failed", "chapter": file,
                                    "attempt": state["attempt"], "stage": failed_stage,
                                    "feedback": list(state["feedback"][-3:])})
        print(f"{tag} [FAIL] {failed_stage} failed (attempt {state['attempt']}/{max_attempts})")
        if int(state["attempt"]) >= max_attempts:
            project.set_status(manifest, file, "needs-review")
            project.save_manifest(project_dir, manifest)
            for item in state["feedback"]:
                print(f"{tag} [FAIL] feedback: {item}")
            print(f"{tag} [FAIL] gave up after {state['attempt']} attempts - marked needs-review")
            return "needs-review", state, stages_run, attempts_run
        # else: loop back to TRANSLATE with the accumulated feedback


def _chapter_subject(file: str, outcome: str) -> str:
    """Commit subject for one finished chapter; the number comes from the
    CHAPTER_NNNN filename (manifest order would re-read chapters.json)."""
    match = project.CHAPTER_RE.match(file)
    number = int(match.group(1)) if match else 0
    return f"translate: chapter {number:04d} ({outcome})"


def run_range(project_dir: Path, files: list[str], cfg: dict, force: bool = False) -> dict:
    """Run run_chapter sequentially over files.

    Returns {"translated": [...], "needs-review": [...], "skipped": [...]}.
    A chapter whose run raises (e.g. ValueError from malformed YAML frontmatter
    in its source file) is reported, marked needs-review, and skipped so the
    remaining chapters still run. KeyboardInterrupt always propagates (it
    derives from BaseException, not Exception). When auto_build_epub is
    enabled, a serialized background epub build runs after each translated
    chapter and finalize() guarantees a complete final epub.
    """
    scheduler = (
        autobuild.AutoBuildScheduler(project_dir)
        if files and _cfg_value(cfg, "auto_build_epub") else None
    )
    results: dict[str, list[str]] = {"translated": [], "needs-review": [], "skipped": []}
    try:
        for file in files:
            try:
                outcome = run_chapter(project_dir, file, cfg, force=force)
            except (PipelineError, client.LLMFatal):
                # A provider call that exhausted its retries, or an
                # irrecoverable provider code, stops the batch. The old
                # "one bad chapter must not abort the batch" catch marked the
                # chapter needs-review and moved to the next file, which turned
                # a dead API key into a whole run of needs-review chapters and
                # a misleading exit 1. LLMFatal is also a PipelineError (see
                # client.LLMFatal); both are named so the intent survives any
                # future change to that hierarchy.
                raise
            except Exception as exc:  # noqa: BLE001 - one bad chapter must not abort the batch
                reason = str(exc).strip() or type(exc).__name__
                print(f"[FAIL] {file}: {reason.splitlines()[0]}")
                try:
                    manifest = project.load_manifest(project_dir)
                    project.set_status(manifest, file, "needs-review")
                    project.save_manifest(project_dir, manifest)
                except Exception:  # noqa: BLE001 - manifest marking is best-effort
                    print(f"[FAIL] {file}: could not mark needs-review in the manifest")
                results.setdefault("needs-review", []).append(file)
                vcs.commit(project_dir, _chapter_subject(file, "needs-review"))
                continue
            results.setdefault(outcome, []).append(file)
            if outcome != "skipped":
                vcs.commit(project_dir, _chapter_subject(file, outcome))
            if outcome == "translated" and scheduler is not None:
                scheduler.trigger(file)
                scheduler.poll()
    except KeyboardInterrupt:
        if scheduler is not None:
            scheduler.abort()
        raise
    if scheduler is not None:
        scheduler.finalize()
    return results
