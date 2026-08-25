"""Translation glossary: storage, lookup, contextual rendering, seeding.

Glossary file (glossary.json) layout:

    {"terms": [entry, ...],
     "retired": [<source>, ...]  # optional — sources that must never be
                                 # re-added by seed/expansion}

Entry schema:

    source              str          term in the original language (unique key)
    translation         str          fixed translation
    variants            [str]        alternative source-script spellings
                                     (e.g. traditional Chinese)
    alt_translations    [str]        alternative accepted translations
    definition          str          one sentence, target language
    category            str          one of CATEGORIES
    origin              str          "seeded" | "model"
    first_seen_chapter  int | None

set_fields() and merge_entries() are the building blocks the
`glossary set` / `glossary merge` CLI commands share; both treat the
glossary as a dict-of-entries and only save when something actually
changes.
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

# Same CJK range used by lib.review._CJK_RE; mirrored here so the glossary
# set/merge helpers don't need to import review (review already imports
# glossary — a back-edge would be circular). Validates translations on
# CJK-source entries exactly like apply_fixes() does.
_CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


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


def retired_sources(g: dict) -> set[str]:
    """Sources retired from the glossary (mundane terms that must never be
    re-added by seeding or expansion)."""
    return set(g.get("retired") or [])


def retire(project_dir: Path, sources: list[str]) -> list[str]:
    """Remove the entries matching sources from glossary.json and record each
    removed source in its "retired" list (deduped, order-preserving).

    Matching uses find() (source OR variants). Returns only the sources that
    actually removed an entry; sources with no matching entry are silently
    ignored (and not added to "retired").
    """
    g = load(project_dir)
    retired = [s for s in (g.get("retired") or []) if isinstance(s, str)]
    removed: list[str] = []
    for source in sources:
        entry = find(g, source)
        if entry is None:
            continue
        terms = g.setdefault("terms", [])
        for idx, item in enumerate(terms):
            if item is entry:
                del terms[idx]
                break
        if source not in retired:
            retired.append(source)
        removed.append(source)
    if removed:
        g["retired"] = retired
        save(project_dir, g)
    return removed


def set_fields(
    entry: dict, *,
    translation: str | None = None,
    definition: str | None = None,
    category: str | None = None,
    add_variant: list[str] | None = None,
    remove_variant: list[str] | None = None,
    alt_translations: str | None = None,
    add_alt: list[str] | None = None,
    remove_alt: list[str] | None = None,
) -> tuple[dict, list[tuple[str, object, object]]]:
    """Apply atomic field edits to a copy of `entry`.

    Validates exactly like apply_fixes(): unknown category -> ValueError;
    translation still containing CJK characters for a CJK-source entry ->
    ValueError (definitions deliberately skip the CJK check — they may
    legitimately quote source-language terms). Removing an absent variant
    or alt is a silent no-op (the entry stays byte-identical, so callers
    can detect "no change" via the empty `changes` list).

    Returns (new_entry, [(field, old, new), ...]) where each tuple is one
    field that actually differs from the input. Caller decides whether to
    save based on the changes list — this function never writes.
    """
    if not isinstance(entry, dict):
        raise ValueError("entry must be a dict")
    new = dict(entry)
    changes: list[tuple[str, object, object]] = []

    if translation is not None:
        new_translation = translation.strip()
        if not new_translation:
            raise ValueError("empty --translation")
        source = entry.get("source") or ""
        if (
            isinstance(source, str)
            and source
            and _CJK_RE.search(source)
            and _CJK_RE.search(new_translation)
        ):
            raise ValueError(
                f"translation '{new_translation}' still contains source-language "
                f"characters"
            )
        old = entry.get("translation")
        if old != new_translation:
            new["translation"] = new_translation
            changes.append(("translation", old, new_translation))

    if definition is not None:
        old = entry.get("definition")
        if old != definition:
            new["definition"] = definition
            changes.append(("definition", old, definition))

    if category is not None:
        if category not in CATEGORIES:
            raise ValueError(
                f"unknown --category '{category}' "
                f"(must be one of: {', '.join(CATEGORIES)})"
            )
        old = entry.get("category")
        if old != category:
            new["category"] = category
            changes.append(("category", old, category))

    if add_variant or remove_variant:
        old_variants = list(entry.get("variants") or [])
        new_variants = list(old_variants)
        for v in add_variant or []:
            if isinstance(v, str) and v and v not in new_variants:
                new_variants.append(v)
        for v in remove_variant or []:
            if v in new_variants:
                new_variants.remove(v)
        if new_variants != old_variants:
            new["variants"] = new_variants
            changes.append(("variants", list(old_variants), list(new_variants)))

    if alt_translations is not None or add_alt or remove_alt:
        old_alts = list(entry.get("alt_translations") or [])
        if alt_translations is not None:
            # Replace semantics: split on comma, strip, drop empties.
            new_alts = [a.strip() for a in alt_translations.split(",")]
            new_alts = [a for a in new_alts if a]
        else:
            new_alts = list(old_alts)
        for a in add_alt or []:
            if isinstance(a, str) and a and a not in new_alts:
                new_alts.append(a)
        for a in remove_alt or []:
            if a in new_alts:
                new_alts.remove(a)
        if new_alts != old_alts:
            new["alt_translations"] = new_alts
            changes.append(
                ("alt_translations", list(old_alts), list(new_alts))
            )

    return new, changes


def merge_entries(
    g: dict, keep_source: str, remove_source: str,
) -> tuple[dict, str, int, int, bool]:
    """Merge the `remove_source` entry into the `keep_source` entry in place.

    Union `remove`'s `variants` and `alt_translations` into the kept entry
    (keep-first, deduped — set semantics). Fill the kept entry's
    `definition` only when it is empty AND the removed entry has one. Append
    `remove_source` to the top-level `retired` list (set semantics — no
    duplicates). Drop the removed entry from `terms`. The kept entry's
    `translation`, `category`, `origin`, and `first_seen_chapter` are
    preserved verbatim.

    Returns (kept_entry, remove_source_key, variants_added, alt_added,
    definition_filled). Raises ValueError when either entry is missing or
    the two resolve to the same entry. Never writes — caller saves.

    Pre-condition: callers should detect "remove already retired" up front
    via retired_sources(g) for a friendly no-op message; this helper does
    not look at `g["retired"]` (a merge against an already-retired source
    would simply fail to find the entry).
    """
    if keep_source == remove_source:
        raise ValueError(
            f"--keep and --remove must be distinct ('{keep_source}')"
        )
    keep = find(g, keep_source)
    if keep is None:
        raise ValueError(f"no glossary entry for --keep '{keep_source}'")
    remove = find(g, remove_source)
    if remove is None:
        raise ValueError(f"no glossary entry for --remove '{remove_source}'")
    if keep is remove:
        # Source-vs-variant collision resolving to one entry: refuse.
        raise ValueError(
            f"--keep and --remove must be distinct ('{keep_source}')"
        )

    keep_variants = list(keep.get("variants") or [])
    variants_added = 0
    for v in (remove.get("variants") or []):
        if isinstance(v, str) and v and v not in keep_variants:
            keep_variants.append(v)
            variants_added += 1

    keep_alts = list(keep.get("alt_translations") or [])
    alt_added = 0
    for a in (remove.get("alt_translations") or []):
        if isinstance(a, str) and a and a not in keep_alts:
            keep_alts.append(a)
            alt_added += 1

    def_filled = False
    keep_def = (keep.get("definition") or "").strip()
    remove_def = (remove.get("definition") or "").strip()
    if not keep_def and remove_def:
        keep["definition"] = remove.get("definition")
        def_filled = True

    keep["variants"] = keep_variants
    keep["alt_translations"] = keep_alts

    terms = g.get("terms", [])
    for i, item in enumerate(terms):
        if item is remove:
            del terms[i]
            break

    retired = [s for s in (g.get("retired") or []) if isinstance(s, str)]
    remove_key = remove.get("source")
    if isinstance(remove_key, str) and remove_key not in retired:
        retired.append(remove_key)
    g["retired"] = retired

    return keep, (remove_key if isinstance(remove_key, str) else remove_source), \
        variants_added, alt_added, def_filled


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
    then source asc, capped at cap.

    Counting is cross-entry and longest-first: every matchable string
    (source + variants) across ALL entries is combined into a single
    longest-first alternation, so a longer compound consumes its characters
    and a shorter nested term is never also credited for that occurrence —
    e.g. when both 修仙 and 仙界 are glossary entries, the 仙界 inside
    修仙界 counts only for 修仙, not for 仙界. A string owned by several
    entries (one entry's source is another entry's variant) credits each
    owner exactly once per occurrence.
    """
    owners: dict[str, list[dict]] = {}
    for entry in g.get("terms", []):
        for s in [entry.get("source")] + list(entry.get("variants") or []):
            if s:
                owners.setdefault(s, []).append(entry)
    if not owners:
        return []
    pattern = re.compile(
        "|".join(
            re.escape(s) for s in sorted(owners, key=lambda s: (-len(s), s))
        )
    )
    counts: dict[int, int] = {id(entry): 0 for entry in g.get("terms", [])}
    for match in pattern.finditer(body):
        credited: set[int] = set()
        for entry in owners[match[0]]:
            key = id(entry)
            if key not in credited:
                credited.add(key)
                counts[key] += 1
    pairs: list[tuple[dict, int]] = []
    for entry in g.get("terms", []):
        count = counts.get(id(entry), 0)
        if count >= 1:
            pairs.append((entry, count))
    pairs.sort(key=lambda pair: (-pair[1], pair[0].get("source", "")))
    return pairs[:cap]


def render_contextual(pairs: list[tuple[dict, int]]) -> str:
    """Render contextual pairs as one line per entry (or a placeholder).

    Uses the Hy-MT2 trained terminology pattern: one line per entry, exactly
    '<source> translates to "<translation>"'. No categories, definitions, or
    counts here -- they stay in the JSON data for the other stages.
    """
    if not pairs:
        return "(no glossary terms appear in this chapter)"
    return "\n".join(
        f"{entry.get('source', '')} translates to \"{entry.get('translation', '')}\""
        for entry, _count in pairs
    )


def load_catalogue(path: Path) -> dict:
    """Load a seed catalogue JSON: {"language", "name", "terms": [...]}."""
    catalogue_path = Path(path)
    data = json.loads(catalogue_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
        raise ValueError(f"catalogue {catalogue_path.name} must be a JSON object with a 'terms' list")
    return data


def seed(project_dir: Path, catalogue: dict, min_count: int) -> tuple[int, int]:
    """Seed the glossary from a catalogue against the source corpus.

    Returns (added, skipped): existing terms are skipped; retired sources
    (see retire()) are skipped so mundane terms never come back; terms whose
    corpus count >= min_count are upserted with origin="seeded" and
    first_seen_chapter=None; terms below the threshold are ignored. Saves
    only when something was added.
    """
    parts = []
    for chapter in project.discover(project_dir):
        _frontmatter, body = project.read_chapter(chapter.path)
        parts.append(body)
    corpus = "\n".join(parts)
    g = load(project_dir)
    retired = retired_sources(g)
    added = 0
    skipped = 0
    for term in catalogue.get("terms", []):
        source = term.get("source")
        if not source:
            continue
        if source in retired:
            skipped += 1
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
