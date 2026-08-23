"""Translation glossary: storage, lookup, contextual rendering, seeding.

Glossary file (glossary.json) layout: {"terms": [entry, ...]}. Entry schema:

    source              str          term in the original language (unique key)
    translation         str          fixed translation
    variants            [str]        alternative source-script spellings
                                     (e.g. traditional Chinese)
    alt_translations    [str]        alternative accepted translations
    definition          str          one sentence, target language
    category            str          one of CATEGORIES
    origin              str          "seeded" | "model"
    first_seen_chapter  int | None
"""

import json
import re
from pathlib import Path

try:
    from . import project
except ImportError:  # imported with scripts/lib directly on sys.path
    import project

CATEGORIES = ("place", "person", "org", "skill", "technique", "level",
              "state", "item", "honorific", "other")


def empty() -> dict:
    """A fresh, empty glossary."""
    return {"terms": []}


def load(project_dir: Path) -> dict:
    """Read glossary.json; empty() when missing."""
    glossary_path = Path(project_dir) / "glossary.json"
    if not glossary_path.is_file():
        return empty()
    data = json.loads(glossary_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{glossary_path} must contain a JSON object")
    data.setdefault("terms", [])
    return data


def save(project_dir: Path, g: dict) -> None:
    """Write glossary.json as UTF-8 JSON (ensure_ascii=False, indent 2)."""
    glossary_path = Path(project_dir) / "glossary.json"
    project.atomic_write_text(
        glossary_path, json.dumps(g, ensure_ascii=False, indent=2) + "\n", newline="\n"
    )


def find(g: dict, source: str) -> dict | None:
    """Entry whose source or variants match, else None."""
    for entry in g.get("terms", []):
        if entry.get("source") == source or source in (entry.get("variants") or []):
            return entry
    return None


def upsert(g: dict, entry: dict) -> bool:
    """Normalize and insert/replace an entry; True if an existing entry was
    replaced in place, False if appended."""
    norm = dict(entry)
    if norm.get("variants") is None:
        norm["variants"] = []
    if norm.get("alt_translations") is None:
        norm["alt_translations"] = []
    norm.setdefault("category", "other")
    norm.setdefault("origin", "model")
    norm.setdefault("first_seen_chapter", None)
    terms = g.setdefault("terms", [])
    existing = find(g, norm["source"])
    if existing is not None:
        for i, item in enumerate(terms):
            if item is existing:
                terms[i] = norm
                return True
    terms.append(norm)
    return False


def count_in_text(entry: dict, text: str) -> int:
    """Non-overlapping occurrences of source + variants in text.

    Counted via a single longest-first alternation so variants that are
    substrings of the source term (e.g. nickname 小丫 inside 裴小丫) are each
    counted exactly once instead of double-counted.
    """
    terms = sorted(
        {t for t in [entry.get("source")] + list(entry.get("variants") or []) if t},
        key=len,
        reverse=True,
    )
    if not terms:
        return 0
    pattern = "|".join(re.escape(t) for t in terms)
    return len(re.findall(pattern, text))


def contextual(g: dict, body: str, cap: int) -> list[tuple[dict, int]]:
    """[(entry, count)] for entries appearing in body, sorted by count desc
    then source asc, capped at cap."""
    pairs: list[tuple[dict, int]] = []
    for entry in g.get("terms", []):
        count = count_in_text(entry, body)
        if count >= 1:
            pairs.append((entry, count))
    pairs.sort(key=lambda pair: (-pair[1], pair[0].get("source", "")))
    return pairs[:cap]


def render_contextual(pairs: list[tuple[dict, int]]) -> str:
    """Render contextual pairs as one line per entry (or a placeholder)."""
    if not pairs:
        return "(no glossary terms appear in this chapter)"
    lines = []
    for entry, _count in pairs:
        # The translation is quoted and the category parenthesized so models
        # can't mistake the bracketed metadata for part of the term.
        line = f"{entry.get('source', '')} = \"{entry.get('translation', '')}\" ({entry.get('category', 'other')})"
        if entry.get("definition"):
            line += f" — {entry['definition']}"
        lines.append(line)
    return "\n".join(lines)


def load_catalogue(path: Path) -> dict:
    """Load a seed catalogue JSON: {"language", "name", "terms": [...]}."""
    catalogue_path = Path(path)
    data = json.loads(catalogue_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
        raise ValueError(f"catalogue {catalogue_path.name} must be a JSON object with a 'terms' list")
    return data


def seed(project_dir: Path, catalogue: dict, min_count: int) -> tuple[int, int]:
    """Seed the glossary from a catalogue against the source corpus.

    Returns (added, skipped): existing terms are skipped; terms whose corpus
    count >= min_count are upserted with origin="seeded" and
    first_seen_chapter=None; terms below the threshold are ignored. Saves
    only when something was added.
    """
    parts = []
    for chapter in project.discover(project_dir):
        _frontmatter, body = project.read_chapter(chapter.path)
        parts.append(body)
    corpus = "\n".join(parts)
    g = load(project_dir)
    added = 0
    skipped = 0
    for term in catalogue.get("terms", []):
        source = term.get("source")
        if not source:
            continue
        if find(g, source) is not None:
            skipped += 1
            continue
        if count_in_text(term, corpus) >= min_count:
            entry = dict(term)
            entry["origin"] = "seeded"
            entry["first_seen_chapter"] = None
            upsert(g, entry)
            added += 1
    if added > 0:
        save(project_dir, g)
    return added, skipped
