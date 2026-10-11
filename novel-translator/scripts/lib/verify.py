"""Whole-book glossary-drift verification: does a pinned glossary rendering
MISTRANSLATE anywhere it appears?

The question this module exists to answer is not "is the glossary internally
consistent" (that is `review glossary`) and not "is this chapter faithful"
(that is the FAITH gate, per chapter, at translate time). It is the
book-wide, after-the-fact one: a glossary entry that renders correctly in the
chapters that seeded it can be WRONG for a term the same chapter uses in
another sense, and because every later chapter is translated against that
entry, the damage propagates forward. That is the contamination this scans
for -- a glossary item that would force a translation to match it where
matching it mistranslates.

Two stages, because the expensive answer is only worth buying for the chapters
where a cheap answer says "look here".

Stage 1 is deterministic and free. For each translated chapter, match the
chapter's SOURCE against the glossary (glossary.contextual), then ask how the
TRANSLATION rendered those terms (balance.underused, the same classification
the pipeline's balance advisory prints). A glossary term that appears in the
source and is rendered in the translation fewer times than its usage floor
-- max(1, ceil(min_coverage * src)) -- is a candidate. Natural English
repeats nouns less than Chinese does, so this is genuinely ambiguous on its
own: it is a question-generator, not a verdict. A term rendered not at all is
DRIFT, not under-use, and is left to the FAITH reviewer that already owns it
inside a translate run.

Stage 2 spends provider calls on the flagged chapters ONLY, and asks the one
question stage 1 cannot: does forcing this pinned rendering, in THIS chapter's
context, produce a mistranslation? It runs on the `verify` provider job, which
has its own arbitration template (consensus_verify.md) because the general
reviewer/glossary arbitration wording instructs the merge to treat a pinned
rendering as a project decision that is never an error -- the exact opposite
of what this stage asks for.

Advisory, and it never rewrites a translated chapter. A confirmed term marks
its chapter `needs-review`, which is the existing human review surface: a
status flip is what makes a chapter eligible for `retry`, so it is a schedule
for re-translation and is made ONLY on a stage-2 confirmation, never on a
stage-1 counting hit.

Failure policy follows lib/review.py: one chapter's model failure is reported
and skipped so a book scan survives a single bad batch, while client.LLMFatal
still propagates (a provider that will never answer is not a per-chapter
problem). Every error is returned to the caller, which reports it and exits
non-zero -- verify never exits 0 over a chapter it failed to check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib import balance, client, config, consensus, glossary, pipeline, project
from lib.pipeline import _lang_name, fill

JOB = "verify"

TEMPLATE = "glossary_verify.md"

MAX_TERMS_PER_CALL = 40

EXCERPT_LINES = 3

EXCERPT_CHARS = 400

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "mistranslates": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["source", "mistranslates", "reason"],
            },
        }
    },
    "required": ["verdicts"],
}


def _source_excerpt(source_lines: list[str], entry: dict) -> list[str]:
    """Up to EXCERPT_LINES lines of the chapter's source that contain the
    entry's own matchable strings (its source plus any variants).

    This is an EVIDENCE excerpt for the model, deliberately not the count:
    glossary.contextual's cross-entry longest-first ownership decides how many
    occurrences a term is credited with, and reproducing that here would mean
    a second implementation of it. A line that merely contains the term is
    enough context for the semantic question, and any line both a longer
    nested term and this one contain is included for the shorter one too --
    which costs a little redundancy and cannot under-report a term's context.
    """
    needles = [s for s in [entry.get("source")] + list(entry.get("variants") or [])
               if s]
    if not needles:
        return []
    needles.sort(key=lambda s: (-len(s), s))
    picked: list[str] = []
    for line in source_lines:
        if not any(needle in line for needle in needles):
            continue
        picked.append(line[:EXCERPT_CHARS])
        if len(picked) >= EXCERPT_LINES:
            break
    return picked


def _term_record(entry: dict, row: dict, source_lines: list[str]) -> dict:
    """One under-used glossary term, as the JSON object stage 2 reads. Carries
    the counts that stage 1 flagged on, so the model can see what "under-used"
    actually meant for this term rather than being asked to re-derive it."""
    return {
        "source": row["source"],
        "pinned": row["translation"],
        "definition": entry.get("definition", ""),
        "category": entry.get("category", ""),
        "source_count": row["src_count"],
        "rendered_count": row["tgt_count"],
        "floor": row["floor"],
        "source_lines": _source_excerpt(source_lines, entry),
    }


def scan_chapter(cfg: dict, g: dict, source_lines: list[str],
                  translated_lines: list[str]) -> list[dict]:
    """Stage 1 for one chapter: the under-used (source, rendering) rows.

    Source-side matching, then a check of how the TRANSLATION rendered the
    terms that matched -- the balance shape, reused rather than re-derived.
    The cap and the coverage/fuzzy thresholds are the project's own resolved
    config, so a project that tuned min_term_coverage tunes verify with it."""
    source_body = "\n".join(source_lines)
    pairs = glossary.contextual(g, source_body,
                                int(config.get_number(cfg, "contextual_glossary_cap")))
    return balance.underused(
        pairs, translated_lines,
        min_coverage=float(config.get_number(cfg, "min_term_coverage")),
        fuzzy_max=int(config.get_number(cfg, "fuzzy_max_distance")),
    )


def _prompt(project_dir: Path, cfg: dict, chapter_file: str,
            records: list[dict]) -> str:
    return fill(
        pipeline._load_template(project.paths(project_dir)["templates"], TEMPLATE),
        {
            "source_lang": _lang_name(cfg.get("source_lang")),
            "target_lang": _lang_name(cfg.get("target_lang")),
            "chapter": chapter_file,
            "terms": "\n".join(json.dumps(record, ensure_ascii=False)
                               for record in records),
        },
        TEMPLATE,
    )


def _verdicts(project_dir: Path, cfg: dict, chapter_file: str,
              records: list[dict]) -> tuple[dict[str, dict], str | None]:
    """Stage 2 for one flagged chapter: {source: verdict} per term stage 1
    flagged, plus an error string when the chapter could not be judged.

    One call per chapter, through consensus.chat on the `verify` job -- so a
    project that fans `verify` out gets one model per block plus the
    consensus_verify.md arbitration, exactly like every other job.

    A verdict naming a term that was not in this chapter's list is dropped: a
    model that answers about some other chapter's term has not judged this
    one, and letting it through would confirm a chapter on evidence about a
    different one. Terms the model omits are treated as NOT confirmed, so a
    truncated or partial reply cannot confirm anything.
    """
    try:
        resp = consensus.chat(project_dir, cfg, JOB, _prompt(project_dir, cfg,
                                                              chapter_file, records),
                              json_schema=VERIFY_SCHEMA,
                              chapter=Path(chapter_file).stem)
        data = client.extract_json(resp)
    except client.LLMFatal:
        raise
    except Exception as exc:
        return {}, f"{chapter_file}: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        return {}, f"{chapter_file}: response is not a JSON object with a verdicts list"
    offered = {record["source"]: record for record in records}
    out: dict[str, dict] = {}
    for row in data["verdicts"]:
        if not isinstance(row, dict):
            continue
        name = row.get("source")
        if not isinstance(name, str) or name not in offered:
            continue
        reason = row.get("reason")
        out[name] = {
            "source": name,
            "pinned": offered[name]["pinned"],
            "source_count": offered[name]["source_count"],
            "rendered_count": offered[name]["rendered_count"],
            "floor": offered[name]["floor"],
            "mistranslates": row.get("mistranslates") is True,
            "reason": reason if isinstance(reason, str) and reason.strip() else "",
        }
    return out, None


def verify_project(project_dir: Path, cfg: dict) -> dict:
    """Scan every chapter that has both a source and a translation.

    Returns
    {"chapters": [record], "examined": int, "flagged": int, "confirmed": int,
     "confirmed_chapters": [str], "errors": [str]}.

    One record per chapter EXAMINED, in chapter order, clean chapters
    included with empty "terms" and "findings" -- the record set is the input
    a later visualization is built from, so a chapter that was checked and
    passed is as much a result as one that was checked and failed.

    A chapter is examined when translated/<file> exists next to its
    source/<file>: verify reads what the project already produced and never
    translates. A missing or unreadable translation is simply not a chapter
    this scan has anything to say about, so it contributes no record.

    Nothing here writes: the status flip and the JSONL are the caller's, so
    the analysis stays pure and testable."""
    root = Path(project_dir)
    paths = project.paths(root)
    g = glossary.load(root)
    records: list[dict] = []
    errors: list[str] = []
    flagged = 0
    for chapter in project.discover(root):
        translated = paths["translated"] / chapter.file
        if not translated.is_file():
            continue
        try:
            _frontmatter, source_body = project.read_chapter(chapter.path)
            translated_body = translated.read_text(encoding="utf-8-sig")
        except OSError as exc:
            errors.append(f"{chapter.file}: {exc}")
            continue
        source_lines = source_body.splitlines()
        rows = scan_chapter(cfg, g, source_lines, translated_body.splitlines())
        record = {"chapter": chapter.file, "terms": rows, "findings": [],
                  "error": None}
        if rows:
            flagged += 1
            by_source = {entry.get("source", ""): entry
                         for entry in g.get("terms", [])}
            records_for_prompt = [
                _term_record(by_source.get(row["source"], {}), row, source_lines)
                for row in rows[:MAX_TERMS_PER_CALL]
            ]
            verdicts, error = _verdicts(root, cfg, chapter.file, records_for_prompt)
            if error:
                errors.append(error)
                record["error"] = error
            record["findings"] = [verdicts[row["source"]]
                                  for row in rows[:MAX_TERMS_PER_CALL]
                                  if row["source"] in verdicts]
        records.append(record)

    confirmed_chapters = [record["chapter"] for record in records
                          if any(f["mistranslates"] for f in record["findings"])]
    return {
        "chapters": records,
        "examined": len(records),
        "flagged": flagged,
        "confirmed": sum(1 for record in records
                         for f in record["findings"] if f["mistranslates"]),
        "confirmed_chapters": confirmed_chapters,
        "errors": errors,
    }