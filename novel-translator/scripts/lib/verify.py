"""Whole-book glossary-drift verification: does a pinned glossary rendering
MISTRANSLATE anywhere it appears?

The question this module exists to answer is not "is the glossary internally
consistent" (that is `review glossary`) and not "is this chapter faithful"
(that is the FAITH gate, per chapter, at translate time). It is the
book-wide, after-the-fact one: a glossary entry that renders correctly in the
chapters that seeded it can be WRONG for the same term used in another sense,
and because every later chapter is translated against that entry, the damage
propagates forward. That is the contamination this scans for -- a glossary item
that would force a translation to match it where matching it mistranslates.

Two stages, because the expensive answer is only worth buying for the terms
where a cheap answer says "look here".

Stage 1 is deterministic and free, and runs per chapter. For each translated
chapter, match the chapter's SOURCE against the glossary (glossary.contextual),
then ask how the TRANSLATION rendered those terms (balance.underused, the same
classification the pipeline's balance advisory prints). A glossary term that
appears in the source and is rendered in the translation fewer times than its
usage floor -- max(1, ceil(min_coverage * src)) -- is a candidate. Natural
English repeats nouns less than Chinese does, so this is genuinely ambiguous on
its own: it is a question-generator, not a verdict. A term rendered not at all
is DRIFT rather than under-use, and a whole-book scan asks only about the
under-used ones, because an absent rendering is already actionable inside a
translate run: the balance advisory reports it as a drift signal and tells the
translator to use the canonical form. The FAITH investigation is the one caller
that cannot use that signal -- the translation was already produced and already
rejected -- so it asks about drift rows as well (investigate_faith, balance.drifted).

Stage 2 batches BY TERM, across the whole book, and asks the one question stage
1 cannot: does forcing this pinned rendering, in these contexts, produce a
mistranslation? The grain is the term rather than the chapter because the drift
definition is "words meaning different things in different contexts" -- spotting
that needs one term's usages laid against EACH OTHER, and per-chapter calls
structurally cannot see a chapter that renders the term faithfully contradict
one that does not. The verdict itself stays per-(term, chapter), so the
status-flip contract is unchanged. It runs on the `verify` provider job, which
has its own arbitration template (consensus_verify.md) because the general
reviewer/glossary arbitration wording instructs the merge to treat a pinned
rendering as a project decision that is never an error -- the exact opposite
of what this stage asks for.

A term's occurrences are grouped into BLOCKS first -- one per run of windows
that overlap or touch around its hits, scoped to a single chapter -- and the
blocks are packed into batches of at most BATCH_CHARS. Every occurrence reaches
a call: a dropped one is exactly where a contaminating one hides, so the cap is
paid for with more calls rather than with a silent gap, and the call count is
reported. Grouping happens BEFORE batching, which is what makes the evidence
non-repeating: a line near two hits is presented once, inside one call, rather
than twice inside one prompt or twice across two of them.

Each block pairs a span of source lines with the translation lines at the
SAME indices, both numbered. That pairing is the pipeline's line-indexed
protocol -- VALIDATE refuses a translation whose line count differs from its
source -- so index N here is index N there, and the span is what lets the
model judge a SENSE rather than a bare hit line. A chapter whose counts do not
match is reported as an error and left unchecked: its indices cannot be trusted,
and verify never exits 0 over a chapter it failed to check.

Advisory, and it never rewrites a translated chapter. A confirmed term marks
its chapter `needs-review`, which is the existing human review surface: a
status flip is what makes a chapter eligible for `retry`, so it is a schedule
for re-translation and is made ONLY on a stage-2 confirmation, never on a
stage-1 counting hit. The same confirmation ALSO flags the glossary entry, as a
per-term contamination record: flipping the chapter fixes the past, while
leaving the entry in place means the next chapter translated against it hits
the same wall. Nothing writes glossary.json -- it feeds the translator's
pinned-terms list, so a flag stored there would change translation behaviour,
which is a separate decision.

Failure policy follows lib/review.py: one batch's model failure is reported and
skipped so a book scan survives a single bad call, while client.LLMFatal still
propagates (a provider that will never answer is not a one-batch problem). Every
error is returned to the caller, which reports it and exits non-zero -- verify
never exits 0 over a chapter it failed to check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib import balance, client, config, consensus, glossary, pipeline, project
from lib.pipeline import _lang_name, fill

JOB = "verify"

TEMPLATE = "glossary_verify.md"

CONTEXT_WINDOW = 2

CONTEXT_CHARS = 400

BATCH_CHARS = 128_000
"""Characters one batch of RENDERED EVIDENCE may carry: exactly what
_render_blocks emits for that batch -- every block's header line with its
occurrence counts, its "[N] " prefix, its source and translation lines, the
indentation on all of them, and the blank-line gaps between blocks. It does NOT
bound the rendered prompt -- the rubric, the term block and the language names are
constant per call, and counting them would make the cap a statement about text
the model never reads as evidence, which is a different quantity, measured per
call rather than per batch.

_batches measures that rendered form by calling the same renderer the prompt is
built from, so "no batch's evidence exceeds BATCH_CHARS" is a statement about
what the model reads, not about a summary of it. A cost function counting only
the chapter name, the line range and the raw lines under-counted the header, the
prefix and the indentation by 14.8% on a real book: an invariant true of the
proxy and false of the prompt, and true of the prompt is the whole point.

The number is a share of the INPUT context every model in use has -- each
carries at least 256,000 -- so a full batch of evidence still fits a call with
room for the prompt's own prose, and a book whose chapters run far longer than
the measured one is not split for nothing. The ceiling the old cap of eight
contexts per call implied was 32,000 (8 x (2 * CONTEXT_WINDOW + 1) lines x 2
languages x CONTEXT_CHARS), and it bound nothing real: a 23-term run on a real
73-chapter book measured a p95 payload of 10,674 characters, and its largest
single block rendered at 34,873 -- already past it before any batching decision
was made. Overhead is what that derivation missed, since it counts line payload
only: eight maximal windows render at 33,454 where their payload is 32,000.

It is a character budget rather than a block count because a block spans however
many lines its hits' windows covered, so counting blocks bounds nothing. A block
larger than the cap on its own ships alone: the cap bounds how many blocks
travel together, never whether evidence reaches a call.

It is not derived from the project's config because the config carries no
input-context key to derive it from: max_tokens and max_tokens_limit cap
OUTPUT, so a per-project derivation would mean inventing a key."""

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "chapter": {"type": "string"},
                    "mistranslates": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["source", "chapter", "mistranslates", "reason"],
            },
        }
    },
    "required": ["verdicts"],
}


def _hit_lines(source_lines: list[str], entry: dict) -> list[int]:
    """Indexes of the chapter's source lines that contain the entry's own
    matchable strings (its source plus any variants).

    This is an EVIDENCE locator, deliberately not a count: the number of lines
    here is how many CONTEXTS a term is judged on, while how many OCCURRENCES
    it was credited with stays balance's number, so the two can disagree and the
    prompt does not claim otherwise. A line that merely contains the term is
    enough to anchor a window, and a line both a longer nested term and this one
    contain is offered for the shorter one too -- which costs redundancy and
    cannot under-report a term's context.
    """
    needles = [s for s in [entry.get("source")] + list(entry.get("variants") or [])
               if s]
    if not needles:
        return []
    needles.sort(key=lambda s: (-len(s), s))
    return [index for index, line in enumerate(source_lines)
            if any(needle in line for needle in needles)]


def _chapter_lines(translated_body: str) -> list[str]:
    """The translated chapter's lines, read the way read_chapter reads a source
    chapter: blank lines off both ends are stripped, the body between is kept
    verbatim.

    The symmetry is the point, not tidiness. VALIDATE compares a translation
    against the source lines read_chapter returns, which are trimmed, so those
    trimmed lines are what "line N" means to the entire line-indexed protocol.
    Reading the translation raw instead would make a trailing blank line look
    like a paragraph of translation, and the pairing below would be off by one
    against a protocol the pipeline itself is already enforcing.

    The body arriving here has already been through read_chapter, so the
    frontmatter it carries on disk is gone and this is the blank-line half of
    that same read -- the two halves together are what makes a source chapter
    and its translation line up. Trimming what read_chapter left is therefore
    completing it, not repeating it: the source is not trimmed a second time,
    and must not be."""
    lines = translated_body.splitlines()
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    last = len(lines)
    while last > first and not lines[last - 1].strip():
        last -= 1
    return lines[first:last]


def _block(chapter_file: str, source_lines: list[str],
           translated_lines: list[str], low: int, high: int, hits: int,
           row: dict) -> dict:
    """One evidence block: the lines low..high of a chapter, source and
    translation at the same 1-based line numbers, carrying how many hit lines
    the span covers and the counts stage 1 flagged this term on THIS chapter.

    The span arrives clamped to the chapter's edges -- _blocks is where a hit
    becomes a window; this renders one.

    The counts travel with the block rather than with the term because they are
    per chapter: the same term can be rendered faithfully everywhere but one.
    """
    return {
        "chapter": chapter_file,
        "low": low,
        "lines": f"{low + 1}-{high + 1}",
        "hits": hits,
        "source": [f"{n + 1}: {source_lines[n][:CONTEXT_CHARS]}"
                   for n in range(low, high + 1)],
        "translation": [f"{n + 1}: {translated_lines[n][:CONTEXT_CHARS]}"
                        for n in range(low, high + 1)],
        "source_count": row["src_count"],
        "rendered_count": row["tgt_count"],
        "floor": row["floor"],
    }


def _blocks(chapter_file: str, source_lines: list[str],
            translated_lines: list[str], indices: list[int],
            row: dict) -> list[dict]:
    """One chapter's hits as evidence blocks: each hit's CONTEXT_WINDOW window,
    clamped to the chapter, with windows that OVERLAP OR TOUCH merged into one
    block spanning min(low)..max(high).

    Merging is union-preserving by construction. A merged block spans exactly
    the lines its own hits' windows covered: nothing is added, so the model is
    never shown evidence the book does not have, and nothing is dropped, so a
    hit can never fall outside every block and go unjudged. Windows with a gap
    between them stay separate blocks, because joining across a gap would
    present lines no hit's window reached.

    Blocks are chapter-scoped, and must be: line numbers restart at zero in
    every chapter, so merging a term's hits ACROSS chapters would fuse unrelated
    windows into a span that spans nothing.

    This runs once per (term, chapter), BEFORE any batching. Coalescing inside
    the batch loop would produce blocks that overlap across batches -- the same
    line re-read by the next call, the repetition this removes restored, and
    every per-prompt check still green.
    """
    spans: list[list[int]] = []
    for index in sorted(indices):
        low = max(0, index - CONTEXT_WINDOW)
        high = min(len(source_lines) - 1, index + CONTEXT_WINDOW)
        if spans and low <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], high)
            spans[-1][2] += 1
            continue
        spans.append([low, high, 1])
    return [_block(chapter_file, source_lines, translated_lines,
                   low, high, hits, row)
            for low, high, hits in spans]


def _term_block(term: dict) -> dict:
    """The glossary entry under judgement, as the JSON object the prompt reads."""
    return {
        "source": term["source"],
        "pinned": term["pinned"],
        "definition": term["definition"],
        "category": term["category"],
    }


_BLOCK_SEPARATOR = "\n\n"


def _render_block(block: dict, position: int) -> str:
    """One block as the prompt presents it: the numbered header line with the
    chapter's counts, then the source lines and the translation lines at the
    same indices, both indented.

    `position` is the number _render_blocks puts on the block, and it is part of
    the rendered text, so _batches measures a block at the position its batch
    really gives it -- a tenth block's "[10] " is wider than a first block's
    "[1] ", and a cap that ignored that would be a cap on its own proxy again.
    """
    return (
        f"[{position}] {block['chapter']} lines {block['lines']} "
        f"(source {block['source_count']} occurrence(s), rendered "
        f"{block['rendered_count']}, floor {block['floor']})\n"
        f"  source:\n"
        + "\n".join(f"    {line}" for line in block["source"])
        + "\n  translation:\n"
        + "\n".join(f"    {line}" for line in block["translation"]))


def _render_blocks(blocks: list[dict]) -> str:
    """Blocks as the prompt's numbered evidence blocks, source above
    translation under each line range."""
    return _BLOCK_SEPARATOR.join(
        _render_block(block, position)
        for position, block in enumerate(blocks, start=1))


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


def _prompt(project_dir: Path, cfg: dict, term: dict,
            blocks: list[dict]) -> str:
    """One term's judging prompt: the rubric plus the term and the blocks it is
    judged on. The data is IN the prompt -- the rubric alone answers nothing.

    It reports the batch as CONTEXTS and as the occurrences those contexts
    cover, because the rubric's rules reason about occurrences: a context count
    alone would tell the model it is holding fewer pieces of evidence than it
    is, and calling the unit a block in one place and a context in the rest
    would make it map between two names for the same thing."""
    return fill(
        pipeline._load_template(project.paths(project_dir)["templates"], TEMPLATE),
        {
            "source_lang": _lang_name(cfg.get("source_lang")),
            "target_lang": _lang_name(cfg.get("target_lang")),
            "term": json.dumps(_term_block(term), ensure_ascii=False, indent=2),
            "contexts": _render_blocks(blocks),
            "context_count": str(len(blocks)),
            "hit_count": str(sum(block["hits"] for block in blocks)),
        },
        TEMPLATE,
    )


def _verdicts(project_dir: Path, cfg: dict, term: dict,
              blocks: list[dict]) -> tuple[list[dict], str | None]:
    """Stage 2 for one term's batch: the confirmed-or-not verdicts keyed by
    chapter, plus an error string when the batch could not be judged.

    One consensus.chat per batch, on the `verify` job -- so a project that fans
    `verify` out gets one model per block plus the consensus_verify.md
    arbitration, exactly like every other job.

    The trace's chapter= is the FIRST CHAPTER IN THIS BATCH'S BLOCK ORDER, not
    the first chapter the term occurs in. A batch is a payload limit and can
    open mid-book, so the bucket a call is filed under is the chapter its
    evidence starts on; batches are ordered by _batches, so that is the lowest
    chapter this batch carries. It is a tier-2 label for logs/verify.jsonl,
    not a claim that the call judged only that chapter -- the verdicts are keyed
    by the chapter each one names.

    A verdict naming a term or a chapter that was not in this batch is
    dropped: a model that answers about some other context has not judged this
    one, and letting it through would confirm a chapter on evidence about a
    different one. Contexts the model omits are NOT confirmed, so a truncated
    or partial reply cannot confirm anything.
    """
    label = (f"{term['source']} ({len(blocks)} context(s), "
             f"{', '.join(sorted({b['chapter'] for b in blocks}))})")
    try:
        resp = consensus.chat(project_dir, cfg, JOB,
                              _prompt(project_dir, cfg, term, blocks),
                              json_schema=VERIFY_SCHEMA,
                              chapter=Path(blocks[0]["chapter"]).stem)
        data = client.extract_json(resp)
    except client.LLMFatal:
        raise
    except Exception as exc:
        return [], f"{label}: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        return [], f"{label}: response is not a JSON object with a verdicts list"
    offered = {block["chapter"]: block for block in blocks}
    out: list[dict] = []
    for row in data["verdicts"]:
        if not isinstance(row, dict):
            continue
        name = row.get("source")
        chapter = row.get("chapter")
        if not isinstance(name, str) or name != term["source"]:
            continue
        if not isinstance(chapter, str) or chapter not in offered:
            continue
        reason = row.get("reason")
        out.append({
            "source": name,
            "pinned": term["pinned"],
            "chapter": chapter,
            "source_count": offered[chapter]["source_count"],
            "rendered_count": offered[chapter]["rendered_count"],
            "floor": offered[chapter]["floor"],
            "mistranslates": row.get("mistranslates") is True,
            "reason": reason if isinstance(reason, str) and reason.strip() else "",
        })
    return out, None


def _batches(blocks: list[dict]) -> list[list[dict]]:
    """Blocks packed into batches of at most BATCH_CHARS of RENDERED EVIDENCE,
    in non-decreasing (chapter, low) order whatever order they arrive in.

    Sorting here rather than at the call sites makes the order a property of
    the batch itself: the model reads the first block first, and the chapter
    that block sits in is also the chapter _verdicts files the call under.
    Ordering drives attention and is not recovered by any later step, so it is
    stated once and holds for both callers.

    A block's cost is _render_block's own output for it AT THE POSITION THIS
    BATCH PUTS IT IN, plus the gap that precedes it -- so `size` is the exact
    length of _render_blocks(batch), not a summary of it. Taking the cost from
    the renderer rather than re-deriving the layout here is what keeps the two
    from drifting apart, and measuring the rendered form is what makes BATCH_CHARS
    a bound on what the model reads: the header line, the "[N] " prefix and the
    indentation are bytes the model receives, and a cost that omitted them
    under-counted by 14.8% on a real book. A block ships at position one of its
    batch, so a block that opens the next batch is measured again at position
    one; the position of a block already in the batch never moves.

    The rubric, the term block and the language names are the same for every
    call, so they stay uncounted -- the cap is on the evidence a batch carries,
    not on the rendered prompt.

    A block that alone exceeds the cap ships BY ITSELF. Dropping or narrowing
    evidence is the one outcome this stage must never produce, because a
    contaminating usage is exactly what a dropped window hides; the cap bounds
    how many blocks travel together, not the payload of any one of them.
    """
    ordered = sorted(blocks, key=lambda block: (block["chapter"], block["low"]))
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for block in ordered:
        cost = len(_render_block(block, len(current) + 1))
        if current:
            cost += len(_BLOCK_SEPARATOR)
        if current and size + cost > BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
            cost = len(_render_block(block, 1))
        current.append(block)
        size += cost
    if current:
        batches.append(current)
    return batches


def _merge(into: dict[tuple[str, str], dict], verdicts: list[dict]) -> None:
    """Fold one batch's verdicts into the per-(term, chapter) findings.

    A term whose contexts span more than one batch produces one finding per
    CHAPTER, not per batch: the contexts of that chapter in this batch add to
    the ones already judged, and a confirmation anywhere among them confirms the
    chapter, because a batch boundary is a payload limit and not a judgement
    about where the drift is."""
    for verdict in verdicts:
        key = (verdict["source"], verdict["chapter"])
        current = into.get(key)
        if current is None:
            into[key] = dict(verdict)
            continue
        current["mistranslates"] = (current["mistranslates"]
                                    or verdict["mistranslates"])
        if verdict["mistranslates"] and not current["reason"]:
            current["reason"] = verdict["reason"]


def _contamination(findings: list[dict]) -> list[dict]:
    """One record per term the model confirmed, naming every chapter it
    confirmed in. The glossary entry is the thing that propagates, so it is
    flagged on its own rather than only through the chapters it hurt."""
    out: list[dict] = []
    for source in sorted({f["source"] for f in findings if f["mistranslates"]}):
        hits = [f for f in findings
                if f["source"] == source and f["mistranslates"]]
        out.append({
            "source": source,
            "pinned": hits[0]["pinned"],
            "chapters": sorted({f["chapter"] for f in hits}),
            "reason": next((f["reason"] for f in hits if f["reason"]), ""),
        })
    return out


def verify_project(project_dir: Path, cfg: dict,
                   chapters: set[str] | None = None) -> dict:
    """Scan every chapter that has both a source and a translation.

    Returns
    {"chapters": [record], "contaminated_terms": [record], "examined": int,
     "flagged": int, "confirmed": int, "calls": int, "confirmed_chapters":
     [str], "errors": [str]}.

    One record per chapter EXAMINED, in chapter order, clean chapters
    included with empty "terms" and "findings" -- the record set is the input
    a later visualization is built from, so a chapter that was checked and
    passed is as much a result as one that was checked and failed.

    A chapter is examined when translated/<file> exists next to its
    source/<file>: verify reads what the project already produced and never
    translates. A missing, unreadable or malformed translation -- one whose
    frontmatter does not parse as a YAML mapping -- is simply not a chapter
    this scan has anything to say about, so it contributes no record and its
    error goes to `errors`.

    Both sides of the pairing are read through project.read_chapter, never
    raw. The source needs it for its own frontmatter; the translation needs it
    because assemble.assemble writes frontmatter into every chapter it
    produces, so reading the translation as a file counted those header lines
    as translated text and rejected every real chapter as unpairable.

    `chapters`, when given, restricts the scan to those file names. It is the
    cascade check's scope: a glossary edit can only have contaminated the
    chapters the edited term actually occurs in, so scanning the whole book
    would spend stage 2's calls on chapters the edit could not have reached.
    The restriction is applied BEFORE stage 1, not as a filter on its results,
    because the calls are the cost -- dropping records afterwards would have
    already paid for them. None (the default) scans everything, so the
    standalone `verify` command is unchanged.

    Nothing here writes: the status flip and the JSONL are the caller's, so
    the analysis stays pure and testable."""
    root = Path(project_dir)
    paths = project.paths(root)
    g = glossary.load(root)
    records: list[dict] = []
    errors: list[str] = []
    flagged = 0
    by_term: dict[str, dict] = {}
    for chapter in project.discover(root):
        if chapters is not None and chapter.file not in chapters:
            continue
        translated = paths["translated"] / chapter.file
        if not translated.is_file():
            continue
        try:
            _frontmatter, source_body = project.read_chapter(chapter.path)
            _translated_fm, translated_body = project.read_chapter(translated)
        except (OSError, ValueError) as exc:
            errors.append(f"{chapter.file}: {exc}")
            continue
        source_lines = source_body.splitlines()
        translated_lines = _chapter_lines(translated_body)
        record = {"chapter": chapter.file, "terms": [], "findings": [],
                  "error": None}
        if len(source_lines) != len(translated_lines):
            message = (f"{chapter.file}: source has {len(source_lines)} lines "
                       f"but the translation has {len(translated_lines)} - the "
                       f"line indices stage 2 pairs cannot be trusted, so the "
                       f"chapter was not checked")
            errors.append(message)
            record["error"] = message
            records.append(record)
            continue
        rows = scan_chapter(cfg, g, source_lines, translated_lines)
        if rows:
            flagged += 1
            by_source = {entry.get("source", ""): entry
                         for entry in g.get("terms", [])}
            record["terms"] = rows
            for row in rows:
                entry = by_source.get(row["source"], {})
                term = by_term.setdefault(row["source"], {
                    "source": row["source"],
                    "pinned": row["translation"],
                    "definition": entry.get("definition", ""),
                    "category": entry.get("category", ""),
                    "blocks": [],
                })
                term["blocks"].extend(
                    _blocks(chapter.file, source_lines, translated_lines,
                            _hit_lines(source_lines, entry), row))
        records.append(record)

    findings: dict[tuple[str, str], dict] = {}
    calls = 0
    for source in sorted(by_term):
        term = by_term[source]
        for batch in _batches(term["blocks"]):
            calls += 1
            verdicts, error = _verdicts(root, cfg, term, batch)
            if error:
                errors.append(error)
                batch_chapters = {block["chapter"] for block in batch}
                for record in records:
                    if record["chapter"] in batch_chapters and not record["error"]:
                        record["error"] = error
            _merge(findings, verdicts)

    per_chapter: dict[str, list[dict]] = {}
    for (_source, chapter), finding in findings.items():
        per_chapter.setdefault(chapter, []).append(finding)
    for record in records:
        record["findings"] = per_chapter.get(record["chapter"], [])

    resolved = list(findings.values())
    confirmed_chapters = sorted(
        record["chapter"] for record in records
        if any(f["mistranslates"] for f in record["findings"]))
    return {
        "chapters": records,
        "contaminated_terms": _contamination(resolved),
        "examined": len(records),
        "flagged": flagged,
        "confirmed": sum(1 for finding in resolved
                         if finding["mistranslates"]),
        "calls": calls,
        "confirmed_chapters": confirmed_chapters,
        "errors": errors,
    }


def investigate_faith(project_dir: Path, cfg: dict, g: dict, chapter: str,
                      source_lines: list[str],
                      translated_lines: list[str]) -> dict:
    """Ask whether ONE chapter's faithfulness rejection was glossary
    over-binding, and report what a pinned rendering would cost to force.

    Returns {"considered": bool, "candidates": [str], "findings": [record],
     "contaminated_terms": [record], "calls": int, "errors": [str]}.

    considered False means the pre-filter declined the chapter and NO model
    call was made. That filter is the whole point of this function. A chapter
    FAITH-rejects for many reasons -- an invented event, a dropped negation,
    a wrong pronoun -- and the glossary is only implicated when the source
    matched a pinned term that the translation then declined to use. So the
    gate is stage 1's own answer on THIS chapter, computed from the same pairs
    the balance advisory already produced, and it is free: a rejected chapter
    with no declined pinned term is dismissed before any call.

    Both shapes of decline count, under-use AND a wholesale refusal
    (balance.underused and balance.drifted). A chapter that renders every
    pinned term it matched zero times is not a weaker over-binding candidate
    than one that renders some of them -- it is the purest one, the shape a
    translator produces when the entry does not fit the sense -- and it is the
    one a whole-book scan must not pay for, which is why the two pre-filters
    differ. It is still free: both classifications are arithmetic over counts
    already computed.

    The surviving rows go through the same stage 2 as a book scan -- same
    rubric, same per-term batching, same context windows -- because it is the
    same question, asked about one chapter instead of all of them. Confirmed
    terms are returned as findings; this function writes nothing and flips no
    status, because the caller is mid-translate and the chapter is already
    headed for needs-review by the FAITH gate itself.
    """
    root = Path(project_dir)
    empty = {"considered": False, "candidates": [], "findings": [],
             "contaminated_terms": [], "calls": 0, "errors": []}
    if len(source_lines) != len(translated_lines):
        empty["errors"].append(
            f"{chapter}: source has {len(source_lines)} lines but the "
            f"translation has {len(translated_lines)} - the line indices stage "
            f"2 pairs cannot be trusted, so no glossary over-binding was "
            f"investigated")
        return empty

    pairs = [(entry, count) for entry, count in glossary.contextual(
        g, "\n".join(source_lines),
        int(config.get_number(cfg, "contextual_glossary_cap")))]
    min_coverage = float(config.get_number(cfg, "min_term_coverage"))
    fuzzy_max = int(config.get_number(cfg, "fuzzy_max_distance"))
    rows = (balance.underused(pairs, translated_lines,
                              min_coverage=min_coverage, fuzzy_max=fuzzy_max)
            + balance.drifted(pairs, translated_lines,
                              min_coverage=min_coverage, fuzzy_max=fuzzy_max))
    if not rows:
        return empty

    by_source = {entry.get("source", ""): entry
                 for entry in g.get("terms", [])}
    by_term: dict[str, dict] = {}
    for row in rows:
        entry = by_source.get(row["source"], {})
        term = by_term.setdefault(row["source"], {
            "source": row["source"],
            "pinned": row["translation"],
            "definition": entry.get("definition", ""),
            "category": entry.get("category", ""),
            "blocks": [],
        })
        term["blocks"].extend(
            _blocks(chapter, source_lines, translated_lines,
                    _hit_lines(source_lines, entry), row))

    findings: dict[tuple[str, str], dict] = {}
    calls = 0
    errors: list[str] = []
    for source in sorted(by_term):
        term = by_term[source]
        for batch in _batches(term["blocks"]):
            calls += 1
            verdicts, error = _verdicts(root, cfg, term, batch)
            if error:
                errors.append(error)
            _merge(findings, verdicts)

    resolved = list(findings.values())
    return {
        "considered": True,
        "candidates": sorted(by_term),
        "findings": resolved,
        "contaminated_terms": _contamination(resolved),
        "calls": calls,
        "errors": errors,
    }