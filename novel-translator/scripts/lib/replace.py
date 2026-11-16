"""Term replacement across translated chapters.

Smart phrase matching reuses the balance checker's multi-word semantics
(case-insensitive, words joined by whitespace or hyphens, optional inflection
on the last word) minus the fuzzy/stem tier -- a rewrite must not guess.
Chapter files are rewritten surgically: the YAML frontmatter block is kept
byte-verbatim and only the body (everything after the closing '---') is
touched, so [^N] footnote markers and the Translator's Notes section keep
their structure.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lib import balance, glossary, project


class ReplaceError(Exception):
    """Replacement setup problem (unknown term, empty phrase)."""


def build_matcher(phrase: str) -> tuple[str, Any]:
    """("regex", compiled) for Latin phrases, ("literal", phrase) for CJK.

    The regex mirrors balance.count_in_target's phrase branch: \\b-bounded,
    case-insensitive, words joined by [\\s-]+, optional inflection on the
    last word -- captured (group 1) so the replacement can re-append it.
    """
    phrase = phrase.strip()
    if not phrase:
        raise ReplaceError("empty search phrase")
    if balance.CJK_RE.search(phrase):
        return ("literal", phrase)
    words = phrase.lower().replace("-", " ").split()
    if not words:
        raise ReplaceError(f"phrase {phrase!r} has no matchable words")
    joiner = r"[\s-]+"
    head = joiner.join(re.escape(w) for w in words[:-1])
    tail = re.escape(words[-1]) + r"(es|s|ed|ing)?"
    pattern = r"\b" + (head + joiner if head else "") + tail + r"\b"
    return ("regex", re.compile(pattern, re.IGNORECASE))


def _match_replacement(span: str, suffix: str, new: str) -> str:
    """Adapt `new` to how the matched span was written: keep the span's
    capitalization pattern and re-append its inflection suffix."""
    out = new
    words = [w for w in re.split(r"[\s-]+", span) if w]
    if words and all(w[0].isupper() for w in words):
        out = " ".join(p[:1].upper() + p[1:] for p in out.split(" "))
    elif span[:1].isupper():
        out = out[:1].upper() + out[1:]
    if suffix and not out.lower().endswith(suffix.lower()):
        out += suffix
    return out


def replace_text(text: str, phrase: str, new: str) -> tuple[str, int]:
    """Replace every smart match of `phrase` with `new`; returns
    (new_text, count)."""
    kind, matcher = build_matcher(phrase)
    if kind == "literal":
        return text.replace(matcher, new), text.count(matcher)
    count = 0

    def _sub(match: re.Match) -> str:
        nonlocal count
        count += 1
        return _match_replacement(match.group(0), match.group(1) or "", new)

    return matcher.sub(_sub, text), count


def _split_frontmatter(text: str) -> tuple[str, str]:
    """(head, tail): head is the frontmatter block INCLUDING the closing
    '---' line, preserved byte-for-byte; tail is everything after it.

    Same detection rule as project.read_chapter (first line '---', next
    bare '---' closes), but no YAML parse/re-serialize -- a replace must not
    reformat what it doesn't touch.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", text
    for close in range(1, len(lines)):
        if lines[close].strip() == "---":
            return "\n".join(lines[: close + 1]), "\n".join(lines[close + 1:])
    return "", text


def replace_chapters(
    project_dir: Path, manifest: list[dict], phrase: str, new: str,
    dry_run: bool = False, progress: list[str] | None = None,
) -> dict[str, Any]:
    """Rewrite `phrase` -> `new` in the body of every translated chapter.

    Manifest-driven: chapters with status 'translated', existing files only
    (missing files are reported, never globbed). Only files whose text
    actually changed are rewritten (atomic, LF). `progress`, when given,
    collects each file's name as its rewrite lands -- glossary_replace uses
    it to report how far an interrupted run got. Returns
    {scanned, changed, occurrences, per_chapter: [(file, count)], missing}.

    This is the RAW-PHRASE replacement: it matches the phrase wherever it
    sits. `glossary replace` uses scoped_replace_chapters instead, which
    only rewrites the paragraphs whose SOURCE paragraph carries the term.
    """
    translated = project.paths(project_dir)["translated"]
    entries = sorted(
        (e for e in manifest if e.get("status") == "translated"),
        key=lambda e: int(e.get("order", 0)),
    )
    per_chapter: list[tuple[str, int]] = []
    missing: list[str] = []
    occurrences = 0
    changed = 0
    scanned = 0
    for entry in entries:
        file = entry["file"]
        path = translated / file
        if not path.is_file():
            missing.append(file)
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8-sig")
        head, tail = _split_frontmatter(text)
        new_tail, n = replace_text(tail, phrase, new)
        if not n:
            continue
        per_chapter.append((file, n))
        occurrences += n
        changed += 1
        if not dry_run:
            out = head + "\n" + new_tail if head else new_tail
            project.atomic_write_text(path, out, newline="\n")
            if progress is not None:
                progress.append(file)
    return {
        "scanned": scanned,
        "changed": changed,
        "occurrences": occurrences,
        "per_chapter": per_chapter,
        "missing": missing,
    }


# The Translator's Notes heading a legacy chapter may carry baked into its
# markdown. The pipeline moved the notes into a sidecar, but an older
# project still has them at the end of the body - and they are NOT part of
# the line-for-line pairing with the source.
_TN_HEADING = "## Translator's Notes"


def _body_end(lines: list[str]) -> int:
    """The index where a chapter's baked-in notes begin, or the whole body."""
    for index, line in enumerate(lines):
        if line.strip() == _TN_HEADING:
            return index
    return len(lines)


def _content_range(lines: list[str]) -> tuple[int, int]:
    """The (start, end) of a body's content lines, exclusive.

    The same rule project.read_chapter applies: the blank lines between
    the frontmatter and the first content line, and the ones at the end,
    are not part of the body."""
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def _term_matchers(terms: list[str]) -> list[tuple[str, Any]]:
    """Matchers for the source-side spellings of a term."""
    matchers: list[tuple[str, Any]] = []
    for term in terms:
        text = str(term or "").strip()
        if not text:
            continue
        matchers.append(build_matcher(text))
    return matchers


def _carries_term(line: str, matchers: list[tuple[str, Any]]) -> bool:
    """Whether a source paragraph carries one of the term's spellings."""
    for kind, matcher in matchers:
        if kind == "literal":
            if matcher in line:
                return True
        elif matcher.search(line):
            return True
    return False


def scoped_replace_chapters(
    project_dir: Path, manifest: list[dict], phrase: str, new: str,
    terms: list[str], dry_run: bool = False, progress: list[str] | None = None,
) -> dict[str, Any]:
    """Rewrite `phrase` -> `new` ONLY in the paragraphs that carry the term.

    A glossary entry's rendering can be shared by two terms whose source
    words are one character apart (姜妍妍 and 姜艳艳, a step-sister and
    the protagonist), and a rewrite that matches the English string alone
    renames every one of them - it happened once, and it renamed a
    protagonist in 128 chapters and 1,905 occurrences. The scope is
    therefore the SOURCE: a translated paragraph may be rewritten only
    when the source paragraph it is paired with carries the term (or one of
    its variants).

    The pairing is the pipeline's own: the source's body lines with the
    leading chapter-title line dropped, paired with the translation's body
    lines by index. A chapter whose two sides do not have the same number
    of lines is UNPAIRABLE - the pipeline refuses such a chapter anyway -
    and is reported rather than guessed at.

    Returns {scanned, changed, occurrences, paragraphs, per_chapter,
    missing, unpairable}.
    """
    paths = project.paths(project_dir)
    translated = paths["translated"]
    source_dir = paths["source"]
    entries = sorted(
        (e for e in manifest if e.get("status") == "translated"),
        key=lambda e: int(e.get("order", 0)),
    )
    matchers = _term_matchers(terms)
    per_chapter: list[tuple[str, int, int]] = []
    missing: list[str] = []
    unpairable: list[str] = []
    occurrences = 0
    paragraphs = 0
    changed = 0
    scanned = 0
    for entry in entries:
        file = entry["file"]
        path = translated / file
        source_path = source_dir / file
        if not path.is_file() or not source_path.is_file():
            missing.append(file)
            continue
        scanned += 1
        source_fm, source_body = project.read_chapter(source_path)
        source_lines = source_body.split("\n")
        source_lines, _dropped = project.drop_leading_chapter_title(
            source_lines, source_fm)
        text = path.read_text(encoding="utf-8-sig")
        head, tail = _split_frontmatter(text)
        tail_lines = tail.split("\n")
        # the body is what pairs with the source; a baked-in notes
        # section at the end is not part of it
        notes_at = _body_end(tail_lines)
        start, end = _content_range(tail_lines[:notes_at])
        body_lines = tail_lines[start:end]
        if len(body_lines) != len(source_lines):
            unpairable.append(file)
            continue
        in_scope = any(_carries_term(line, matchers) for line in source_lines)
        chapter_occurrences = 0
        chapter_paragraphs = 0
        for index, source_line in enumerate(source_lines):
            if not _carries_term(source_line, matchers):
                continue
            replaced, count = replace_text(body_lines[index], phrase, new)
            if not count:
                continue
            body_lines[index] = replaced
            chapter_occurrences += count
            chapter_paragraphs += 1
        if not chapter_occurrences and not in_scope:
            continue
        # A chapter that carries the term also carries its notes, and a
        # note ABOUT the term that still shows the old rendering is the
        # stale text this command exists to remove.
        notes_replaced = 0
        if in_scope and notes_at < len(tail_lines):
            notes_text = "\n".join(tail_lines[notes_at:])
            replaced, notes_replaced = replace_text(notes_text, phrase, new)
            if notes_replaced:
                tail_lines[notes_at:] = replaced.split("\n")
                chapter_occurrences += notes_replaced
        if not chapter_occurrences:
            continue
        per_chapter.append((file, chapter_occurrences))
        occurrences += chapter_occurrences
        paragraphs += chapter_paragraphs
        changed += 1
        if not dry_run:
            tail_lines[start:end] = body_lines
            out = head + "\n" + "\n".join(tail_lines)
            project.atomic_write_text(path, out, newline="\n")
            if progress is not None:
                progress.append(file)
    return {
        "scanned": scanned,
        "changed": changed,
        "occurrences": occurrences,
        "paragraphs": paragraphs,
        "per_chapter": per_chapter,
        "missing": missing,
        "unpairable": unpairable,
    }


def glossary_replace(
    project_dir: Path, source_term: str, new_translation: str,
    dry_run: bool = False, keep_alt: bool = False,
) -> dict[str, Any]:
    """Change a glossary entry's translation (keyed by source or variant)
    and rewrite the old rendering in the paragraphs that carry the term.

    The rewrite is SCOPED by the source: a translated paragraph is only
    touched when the source paragraph it is paired with carries the term
    or one of its spellings. A rendering shared by two terms whose source
    words are one character apart used to rename both of them.

    By default the old rendering is pruned from alt_translations -- a stale
    alt would let balance.count_in_target keep counting it as a valid hit,
    masking future drift. keep_alt=True leaves the alt list untouched for
    renderings that should stay accepted variants. Chapters are rewritten
    before glossary.json is saved, so a mid-run failure leaves the glossary
    untouched and re-running the same command completes the rewrites
    idempotently. dry_run reports the
    glossary diff and chapter counts but writes nothing. Returns
    {glossary: {source, old, new, pruned_alt, noop}, chapters: {...}}.
    """
    source_term = source_term.strip()
    new_translation = new_translation.strip()
    if not source_term:
        raise ReplaceError("empty --source term")
    if not new_translation:
        raise ReplaceError("empty --translation")
    g = glossary.load(project_dir)
    entry = glossary.find(g, source_term)
    if entry is None:
        raise ReplaceError(
            f"no glossary entry for '{source_term}' (matched by source or variants)"
        )
    old = str(entry.get("translation") or "")
    if not old.strip():
        raise ReplaceError(
            f"glossary entry '{entry.get('source')}' has no translation to replace"
        )
    empty_chapters = {
        "scanned": 0, "changed": 0, "occurrences": 0, "paragraphs": 0,
        "per_chapter": [], "missing": [], "unpairable": [],
    }
    result: dict[str, Any] = {
        "glossary": {
            "source": entry.get("source"), "old": old, "new": new_translation,
            "pruned_alt": [], "noop": False,
        },
    }
    if old.strip().lower() == new_translation.lower():
        result["glossary"]["noop"] = True
        result["chapters"] = empty_chapters
        return result

    manifest_path = project.paths(project_dir)["manifest"]
    if not manifest_path.is_file():
        raise ReplaceError(f"{manifest_path} not found - run 'init' first")
    try:
        manifest = project.load_manifest(project_dir)
    except (OSError, ValueError) as exc:
        raise ReplaceError(f"cannot read manifest: {exc}") from exc
    build_matcher(old)

    pruned: list[str] = []
    if not keep_alt:
        alts = [a for a in (entry.get("alt_translations") or []) if isinstance(a, str)]
        pruned = [a for a in alts if a.strip().lower() == old.strip().lower()]
        if pruned:
            entry["alt_translations"] = [a for a in alts if a not in pruned]
            result["glossary"]["pruned_alt"] = pruned

    # The scope is the SOURCE: only the paragraphs whose source paragraph
    # carries the term (or one of its spellings) may be rewritten. A
    # rendering shared by two terms one character apart used to rename both
    # of them, and every occurrence of the protagonist with it.
    terms = [str(entry.get("source") or "")]
    terms += [v for v in (entry.get("variants") or []) if isinstance(v, str) and v]

    rewritten: list[str] = []
    try:
        result["chapters"] = scoped_replace_chapters(
            project_dir, manifest, old, new_translation, terms, dry_run,
            progress=rewritten,
        )
    except Exception:
        total = sum(1 for e in manifest if e.get("status") == "translated")
        print(f"[warn] replace incomplete: {len(rewritten)}/{total} chapters "
              f"rewritten; glossary.json not updated - re-run the same command "
              f"to finish")
        raise

    if not dry_run:
        entry["translation"] = new_translation
        glossary.save(project_dir, g)
    return result
