"""Staged, resumable translation pipeline for novel chapters.

Per-chapter stages: TRANSLATE -> VALIDATE -> BALANCE -> GLOSSARY_EXPAND ->
FAITH -> TN_GENERATE -> TN_DEDUP -> ASSEMBLE.

Progress is persisted in draft/<stem>.state.json after every stage, so an
interrupted chapter resumes at the failed stage instead of restarting, and a
failed attempt re-runs TRANSLATE with the accumulated reviewer feedback.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import assemble, balance, client, config, glossary, project, tn

STAGES = (
    "PREP",
    "TRANSLATE",
    "VALIDATE",
    "BALANCE",
    "GLOSSARY_EXPAND",
    "FAITH",
    "TN_GENERATE",
    "TN_DEDUP",
    "ASSEMBLE",
)

TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        # Numbered line protocol: each translated line echoes the 1-based
        # index of its source line. Explicit indices make dropped/merged
        # lines structurally detectable instead of off-by-one guesswork.
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "t": {"type": "string"}},
                "required": ["i", "t"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "lines"],
    "additionalProperties": False,
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

_STAGE_IDX = {name: i for i, name in enumerate(STAGES)}
_KEY_RE = re.compile(r"\{\{([^{}]+)\}\}")
_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


class PipelineError(Exception):
    """Fatal pipeline error (bad spec, missing template, unfilled placeholder)."""


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
    """Acceptable normalized name tokens for a manifest entry (suffix-insensitive)."""
    file_l = str(entry.get("file", "")).strip().lower()
    suffix = str(entry.get("suffix") or "").strip().lower()
    core = file_l[:-3] if file_l.endswith(".md") else file_l
    core_nosuf = core[: -(len(suffix) + 1)] if suffix and core.endswith("." + suffix) else core
    num = _entry_number(entry)
    return {
        file_l,
        core,
        core_nosuf,
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
    """Resolve a spec ("1,3-5,Chapter_0007.zh.md") to file names in manifest order.

    Items may be chapter numbers ("7"), inclusive ranges ("3-5"), or exact
    file names matched suffix-insensitively ("Chapter_0007.md" matches
    "Chapter_0007.zh.md"). Raises PipelineError when nothing matches an item.
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
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_state(draft_dir: Path, file: str, state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    project.atomic_write_text(
        _state_path(draft_dir, file), json.dumps(state, ensure_ascii=False, indent=2)
    )


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _cfg_value(cfg: dict, key: str) -> Any:
    if key in cfg:
        return cfg[key]
    if key in config.DEFAULTS:
        return config.DEFAULTS[key]
    raise PipelineError(f"missing config key: {key}")


def _load_template(templates_dir: Path, name: str) -> str:
    path = templates_dir / name
    if not path.is_file():
        raise PipelineError(f"missing template: {path}")
    return path.read_text(encoding="utf-8")


def _apply_glossary_proposal(
    g: dict, proposal: Any, chapter_order: int, cfg: dict, merge_template: str, tag: str
) -> None:
    """Apply one glossary proposal: add, merge, or silently skip.

    Raises on malformed proposals or merge failures; callers treat those as
    non-fatal and skip the proposal.
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
        # A proposal whose source is contained in (or contains) a known term's
        # source, with the same translation, is a nickname/short form: absorb
        # it as a variant instead of creating a double-counting entry.
        for term in g.get("terms", []):
            esrc = str(term.get("source", ""))
            etr = str(term.get("translation", "")).strip().lower()
            if etr == tr.strip().lower() and esrc and (src in esrc or esrc in src):
                union_variants(term, [src] + variants)
                return
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
    # whether the translation matches.
    union_variants(existing, variants)

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
    resp = client.chat(config.provider(cfg, "glossary"), prompt, json_schema=MERGE_SCHEMA)
    merged = client.extract_json(resp)
    if not isinstance(merged, dict):
        raise ValueError("merge response is not a JSON object")

    updated = dict(existing)
    for key in ("translation", "definition", "category"):
        new_value = merged.get(key)
        if isinstance(new_value, str) and new_value.strip():
            updated[key] = new_value
        # absent (or garbage) values keep the existing entry's value

    terms = g.setdefault("terms", [])
    for idx, term in enumerate(terms):
        if term is existing or term == existing:
            terms[idx] = updated
            print(f"{tag} [ok] glossary ~ '{src}' -> '{updated.get('translation', '')}'")
            return
    glossary.upsert(g, updated)  # defensive: existing entry was not found in the list


# --------------------------------------------------------------------------
# pipeline runner
# --------------------------------------------------------------------------


def run_chapter(project_dir: Path, file: str, cfg: dict, force: bool = False) -> str:
    """Run the staged pipeline for one chapter.

    Returns "translated", "needs-review", or "skipped". Prints progress lines
    prefixed with "[Chapter_NNNN] ".
    """
    paths = project.paths(project_dir)
    manifest = project.load_manifest(project_dir)
    entry = project.find_entry(manifest, file)
    if entry is None:
        raise PipelineError(f"{file}: no manifest entry (run 'init' or 'status' first)")
    tag = f"[Chapter_{int(entry['number']):04d}]"
    stem = Path(file).stem

    if entry.get("status") == "translated" and not force:
        print(f"{tag} [ok] already translated - skipped")
        return "skipped"

    if force:
        for artifact in (f"{stem}.state.json", f"{stem}.md", f"{stem}.lines.json"):
            (paths["draft"] / artifact).unlink(missing_ok=True)
        (paths["translated"] / file).unlink(missing_ok=True)
        print(f"{tag} [init] force: removed previous draft and translated artifacts")

    state = load_state(paths["draft"], file)
    if state is None:
        state = {
            "stage": "TRANSLATE",
            "attempt": 0,
            "feedback": [],
            "title": None,
            "lines": None,
            "notes": None,
            "updated_at": "",
        }
    state.setdefault("stage", "TRANSLATE")
    state.setdefault("attempt", 0)
    state.setdefault("feedback", [])
    state.setdefault("title", None)
    state.setdefault("lines", None)
    state.setdefault("notes", None)

    if state["stage"] not in STAGES:
        state["stage"] = "TRANSLATE"
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
    first = source_lines[0].strip().strip("\u3000 ") if source_lines else ""
    if first and first == str(fm.get("chapter_title", "")).strip().strip("\u3000 "):
        source_lines = source_lines[1:]
        print(f"{tag} [init] leading chapter-title line handled via the title field")
    body_for_counts = "\n".join(source_lines)
    chapter_order = int(entry.get("order", 0))

    templates_dir = paths["templates"]
    tpl_translation = _load_template(templates_dir, "translation.md")
    tpl_glossary_expand = _load_template(templates_dir, "glossary_expand.md")
    tpl_glossary_merge = _load_template(templates_dir, "glossary_merge.md")
    tpl_faithfulness = _load_template(templates_dir, "faithfulness.md")
    tpl_tn_generate = _load_template(templates_dir, "tn_generate.md")

    max_attempts = int(_cfg_value(cfg, "max_attempts"))
    g = glossary.load(project_dir)

    def advance(next_stage: str) -> None:
        state["stage"] = next_stage
        save_state(paths["draft"], file, state)

    def feedback_section() -> str:
        if not state["feedback"]:
            return ""
        return (
            "NOTE: A previous translation attempt was rejected. "
            "Address every issue below and translate the entire chapter again from scratch:\n"
            + "\n".join(f"- {item}" for item in state["feedback"])
        )

    def build_ctx(translation_lines: list[str], feedback: str = "",
                  extra: dict[str, str] | None = None) -> dict[str, str]:
        ctx: dict[str, str] = {
            "source_lang": str(cfg.get("source_lang", "")),
            "target_lang": str(cfg.get("target_lang", "")),
            "chapter_title": str(fm.get("chapter_title", "")),
            "chapter_file": file,
            "line_count": str(len(source_lines)),
            "source_lines": json.dumps(source_lines, ensure_ascii=False),
            "translation_lines": json.dumps(translation_lines, ensure_ascii=False),
            "glossary": glossary_str,
            "feedback_section": feedback,
        }
        if extra:
            ctx.update(extra)
        return ctx

    while True:
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
            try:
                print(f"{tag} [init] TRANSLATE (attempt {state['attempt'] + 1})")
                # Long chapters are translated in balanced chunks: models
                # cannot hold an exact line count over 100+ lines, and the
                # line-for-line mapping is the backbone of the whole pipeline
                # (validation, balance check, TN line indices).
                chunk_size = int(_cfg_value(cfg, "translate_chunk_size"))
                src_total = len(source_lines)
                n_chunks = max(1, (src_total + chunk_size - 1) // chunk_size)
                base, extra = divmod(src_total, n_chunks)
                title: str | None = None
                tlines: list[str] = []
                lo = 0
                for k in range(n_chunks):
                    size = base + (1 if k < extra else 0)
                    chunk = source_lines[lo:lo + size]
                    hi = lo + size
                    expected = list(range(lo + 1, hi + 1))
                    if n_chunks > 1:
                        print(f"{tag} [init] translating part {k + 1}/{n_chunks} (lines {lo + 1}-{hi})")
                    numbered = [{"i": lo + j + 1, "t": ln} for j, ln in enumerate(chunk)]
                    chunk_ctx = build_ctx(
                        [],
                        feedback=feedback_section(),
                        extra={
                            "source_lines": json.dumps(numbered, ensure_ascii=False),
                            "line_count": str(len(chunk)),
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
                        resp = client.chat(
                            config.provider(cfg, "translator"), prompt,
                            json_schema=TRANSLATION_SCHEMA,
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
                            retry_ctx = dict(chunk_ctx)
                            retry_ctx["feedback_section"] = (
                                "NOTE: the previous response for this part was rejected. Fix "
                                "the issue and translate these lines again from scratch:\n- "
                                + problem
                            )
                            prompt = fill(tpl_translation, retry_ctx, "translation.md")
                        else:
                            raise ValueError(f"TRANSLATE {problem}")
                    if clines is None:  # defensive; unreachable via the loop above
                        raise ValueError(f"TRANSLATE part {k + 1} produced no valid lines")
                    tlines.extend(clines)
                    lo = hi
                state["title"] = title or ""
                state["lines"] = tlines
                project.write_chapter(
                    paths["draft"] / f"{stem}.md", {**fm, "title": title}, "\n".join(tlines)
                )
                (paths["draft"] / f"{stem}.lines.json").write_text(
                    json.dumps({"title": title, "lines": tlines}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - becomes retry feedback
                state["feedback"].append(f"TRANSLATE failed: {type(exc).__name__}: {exc}")
                failed_stage = "TRANSLATE"

        lines: list[str] = []
        if failed_stage is None:
            lines = list(state["lines"] or [])

        if failed_stage is None and start_idx <= _STAGE_IDX["VALIDATE"]:
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
                failed_stage = "VALIDATE"

        if failed_stage is None and start_idx <= _STAGE_IDX["BALANCE"]:
            advance("BALANCE")
            # ---------------- BALANCE ----------------
            try:
                _ok, issues = balance.check(
                    pairs,
                    body_for_counts,
                    lines,
                    _cfg_value(cfg, "balance_tolerance"),
                    _cfg_value(cfg, "fuzzy_max_distance"),
                )
                if issues:
                    state["feedback"].extend(str(issue) for issue in issues)
                    failed_stage = "BALANCE"
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - becomes retry feedback
                state["feedback"].append(f"BALANCE check failed: {type(exc).__name__}: {exc}")
                failed_stage = "BALANCE"

        if failed_stage is None and start_idx <= _STAGE_IDX["GLOSSARY_EXPAND"]:
            advance("GLOSSARY_EXPAND")
            # ---------------- GLOSSARY_EXPAND (non-fatal) ----------------
            try:
                max_terms = int(_cfg_value(cfg, "max_new_terms_per_chapter"))
                prompt = fill(
                    tpl_glossary_expand,
                    build_ctx(lines, extra={"max_terms": str(max_terms)}),
                    "glossary_expand.md",
                )
                print(f"{tag} [init] GLOSSARY_EXPAND")
                resp = client.chat(
                    config.provider(cfg, "glossary"), prompt, json_schema=TERMS_SCHEMA
                )
                data = client.extract_json(resp)
                raw_terms = data.get("terms") if isinstance(data, dict) else None
                if not isinstance(raw_terms, list):
                    raise ValueError("expected a 'terms' array")
                for proposal in raw_terms[:max_terms]:
                    try:
                        _apply_glossary_proposal(g, proposal, chapter_order, cfg, tpl_glossary_merge, tag)
                    except PipelineError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - skip this proposal only
                        print(f"{tag} [warn] skipped glossary proposal: {exc}")
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - glossary expansion is auxiliary
                print(f"{tag} [warn] glossary expansion failed: {type(exc).__name__}: {exc}")
            glossary.save(project_dir, g)

        if failed_stage is None and start_idx <= _STAGE_IDX["FAITH"]:
            advance("FAITH")
            # ---------------- FAITH ----------------
            try:
                prompt = fill(tpl_faithfulness, build_ctx(lines), "faithfulness.md")
                print(f"{tag} [init] FAITH")
                resp = client.chat(
                    config.provider(cfg, "reviewer"), prompt, json_schema=VERDICT_SCHEMA
                )
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
                    failed_stage = "FAITH"
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - becomes retry feedback
                state["feedback"].append(f"FAITH review failed: {type(exc).__name__}: {exc}")
                failed_stage = "FAITH"

        if failed_stage is None and start_idx <= _STAGE_IDX["TN_GENERATE"]:
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
                resp = client.chat(
                    config.provider(cfg, "annotator"), prompt, json_schema=NOTES_SCHEMA
                )
                data = client.extract_json(resp)
                raw_notes = data.get("notes") if isinstance(data, dict) else None
                if not isinstance(raw_notes, list):
                    raise ValueError("expected a 'notes' array")
                state["notes"] = raw_notes
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001 - notes are optional
                print(f"{tag} [warn] note generation failed - continuing without notes: {exc}")
                state["notes"] = []

        if failed_stage is None and start_idx <= _STAGE_IDX["TN_DEDUP"]:
            advance("TN_DEDUP")
            # ---------------- TN_DEDUP ----------------
            kept_notes, history, warnings = tn.process(
                state["notes"] or [],
                len(lines),
                chapter_order,
                tn.load_history(project_dir),
                int(_cfg_value(cfg, "tn_gap_chapters")),
            )
            tn.save_history(project_dir, history)
            state["notes"] = kept_notes
            for warning in warnings:
                print(f"{tag} [warn] {warning}")

        if failed_stage is None:
            # ---------------- ASSEMBLE ----------------
            advance("ASSEMBLE")
            out_path = paths["translated"] / file
            assemble.assemble(
                out_path, fm, state["title"] or "", list(lines), list(state["notes"] or [])
            )
            (paths["draft"] / f"{stem}.state.json").unlink(missing_ok=True)
            project.set_status(manifest, file, "translated")
            project.save_manifest(project_dir, manifest)
            print(
                f"{tag} [ok] translated -> {out_path.name} "
                f"(title: {state['title']}, notes: {len(state['notes'] or [])})"
            )
            return "translated"

        # ---------------- failure handling ----------------
        assert failed_stage is not None
        state["attempt"] = int(state["attempt"]) + 1
        # Persist TRANSLATE, not the failed stage: the next action is a
        # re-translation. A persisted failed-gate stage combined with
        # non-null lines would make a crash+resume re-validate the already
        # rejected lines and burn attempts without ever re-translating.
        state["stage"] = "TRANSLATE"
        save_state(paths["draft"], file, state)
        print(f"{tag} [FAIL] {failed_stage} failed (attempt {state['attempt']}/{max_attempts})")
        if int(state["attempt"]) >= max_attempts:
            project.set_status(manifest, file, "needs-review")
            project.save_manifest(project_dir, manifest)
            for item in state["feedback"]:
                print(f"{tag} [FAIL] feedback: {item}")
            print(f"{tag} [FAIL] gave up after {state['attempt']} attempts - marked needs-review")
            return "needs-review"
        # else: loop back to TRANSLATE with the accumulated feedback


def run_range(project_dir: Path, files: list[str], cfg: dict, force: bool = False) -> dict:
    """Run run_chapter sequentially over files.

    Returns {"translated": [...], "needs-review": [...], "skipped": [...]}.
    A chapter whose run raises (e.g. ValueError from malformed YAML frontmatter
    in its source file) is reported, marked needs-review, and skipped so the
    remaining chapters still run. KeyboardInterrupt always propagates (it
    derives from BaseException, not Exception).
    """
    results: dict[str, list[str]] = {"translated": [], "needs-review": [], "skipped": []}
    for file in files:
        try:
            outcome = run_chapter(project_dir, file, cfg, force=force)
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
            continue
        results.setdefault(outcome, []).append(file)
    return results
