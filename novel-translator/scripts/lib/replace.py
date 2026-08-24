"""Term replacement across translated chapters.

Smart phrase matching reuses the balance checker's multi-word semantics
(case-insensitive, words joined by whitespace or hyphens, optional inflection
on the last word) minus the fuzzy/stem tier — a rewrite must not guess.
Chapter files are rewritten surgically: the YAML frontmatter block is kept
byte-verbatim and only the body (everything after the closing '---') is
touched, so [^N] footnote markers and the Translator's Notes section keep
their structure.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from . import balance, glossary, project
except ImportError:  # imported with scripts/lib directly on sys.path
    import balance, glossary, project


class ReplaceError(Exception):
    """Replacement setup problem (unknown term, empty phrase)."""


def build_matcher(phrase: str) -> tuple[str, Any]:
    """("regex", compiled) for Latin phrases, ("literal", phrase) for CJK.

    The regex mirrors balance.count_in_target's phrase branch: \\b-bounded,
    case-insensitive, words joined by [\\s-]+, optional inflection on the
    last word — captured (group 1) so the replacement can re-append it.
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
    bare '---' closes), but no YAML parse/re-serialize — a replace must not
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
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rewrite `phrase` -> `new` in the body of every translated chapter.

    Manifest-driven: chapters with status 'translated', existing files only
    (missing files are reported, never globbed). Only files whose text
    actually changed are rewritten (atomic, LF). Returns
    {scanned, changed, occurrences, per_chapter: [(file, count)], missing}.
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
        text = path.read_text(encoding="utf-8")
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
    return {
        "scanned": scanned,
        "changed": changed,
        "occurrences": occurrences,
        "per_chapter": per_chapter,
        "missing": missing,
    }


def glossary_replace(
    project_dir: Path, source_term: str, new_translation: str,
    dry_run: bool = False, keep_alt: bool = False,
) -> dict[str, Any]:
    """Change a glossary entry's translation (keyed by source or variant)
    and rewrite the old rendering in every translated chapter.

    By default the old rendering is pruned from alt_translations — a stale
    alt would let balance.count_in_target keep counting it as a valid hit,
    masking future drift. keep_alt=True leaves the alt list untouched for
    renderings that should stay accepted variants. dry_run reports the
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
    # str(None) would otherwise become the literal phrase "None".
    old = str(entry.get("translation") or "")
    if not old.strip():
        raise ReplaceError(
            f"glossary entry '{entry.get('source')}' has no translation to replace"
        )
    empty_chapters = {
        "scanned": 0, "changed": 0, "occurrences": 0, "per_chapter": [], "missing": [],
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

    # Fail on setup problems BEFORE the first write (glossary save).
    manifest_path = project.paths(project_dir)["manifest"]
    if not manifest_path.is_file():
        raise ReplaceError(f"{manifest_path} not found - run 'init' first")
    try:
        manifest = project.load_manifest(project_dir)
    except (OSError, ValueError) as exc:
        raise ReplaceError(f"cannot read manifest: {exc}") from exc

    pruned: list[str] = []
    if not keep_alt:
        alts = [a for a in (entry.get("alt_translations") or []) if isinstance(a, str)]
        pruned = [a for a in alts if a.strip().lower() == old.strip().lower()]
        if pruned:
            entry["alt_translations"] = [a for a in alts if a not in pruned]
            result["glossary"]["pruned_alt"] = pruned
    if not dry_run:
        entry["translation"] = new_translation
        glossary.save(project_dir, g)

    result["chapters"] = replace_chapters(
        project_dir, manifest, old, new_translation, dry_run
    )
    return result
