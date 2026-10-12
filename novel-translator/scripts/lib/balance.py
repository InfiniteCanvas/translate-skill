"""Glossary balance checking between source text and its translation."""

import math
import re

CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")


def is_cjk(text: str) -> bool:
    """Shared "is this string source-script?" predicate over CJK_RE -- the
    widest CJK class in the skill (compat ideographs and halfwidth katakana
    included), so glossary.set_fields, review.apply_fixes, and
    fix.invalid_translation_reason all answer it identically. Callers keep
    their own surrounding guards (e.g. isinstance(source, str))."""
    return bool(CJK_RE.search(text))

GUIDE_ONLY_CATEGORIES = frozenset({"unit"})


def _stem(word: str) -> str:
    """Strip one common English inflection suffix, keeping a 3+ char stem.

    Good enough for counting: glossary terms must appear with natural
    grammar (plurals, possessives, tense), and an exact-only count would
    call those legitimate renderings balance violations.
    """
    for suffix in ("'s", "’s", "es", "s", "ed", "ing"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: len(word) - len(suffix)]
    return word


def levenshtein(a: str, b: str, band: int | None = None) -> int:
    """Classic DP edit distance. With band: returns band + 1 immediately when
    abs(len(a) - len(b)) > band (callers only compare against band)."""
    if band is not None and abs(len(a) - len(b)) > band:
        return band + 1
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def count_in_target(entry: dict, lines: list[str], fuzzy_max: int = 2) -> int:
    """Count an entry's translation(s) in translated lines.

    Targets are the translation + alt_translations deduped
    case-insensitively (matching is case-insensitive, so case variants are
    one target; the first-seen form is kept). Per target: CJK targets count
    as exact substrings (there are no word boundaries to match against);
    multi-word Latin targets count as case-insensitive phrases whose words
    may be joined by whitespace OR a hyphen (glossary phrases are supposed
    to be rendered verbatim, but English hyphenates attributive compounds:
    "outer sect" also matches "outer-sect disciple"); single-word targets
    count as tokens equal to the (lowercased) target or within levenshtein
    distance fuzzy_max of a target of length >= 5.
    """
    targets: list[str] = []
    seen: set[str] = set()
    for target in [entry.get("translation", "")] + list(entry.get("alt_translations") or []):
        key = target.strip().casefold()
        if target and key not in seen:
            seen.add(key)
            targets.append(target)
    if not targets:
        return 0
    text = "\n".join(lines)
    total = sum(text.count(target) for target in targets if CJK_RE.search(target))
    wordy = [target for target in targets if not CJK_RE.search(target)]
    if not wordy:
        return total
    low = text.lower()
    tokens = _TOKEN_RE.findall(low)
    for target in wordy:
        words = target.lower().replace("-", " ").split()
        if not words:
            continue
        if len(words) > 1:
            joiner = r"[\s-]+"
            head = joiner.join(re.escape(w) for w in words[:-1])
            tail = re.escape(words[-1]) + r"(?:es|s|ed|ing)?"
            pattern = r"\b" + (head + joiner if head else "") + tail + r"\b"
            total += len(re.findall(pattern, low))
            continue
        word = words[0]
        stem = _stem(word)
        for token in tokens:
            if (
                token == word
                or _stem(token) == stem
                or (len(word) >= 5 and levenshtein(token, word, band=fuzzy_max) <= fuzzy_max)
            ):
                total += 1
    return total


def classify(entry: dict, src_count: int, translated_lines: list[str],
             min_coverage: float = 0.25,
             fuzzy_max: int = 2) -> tuple[str, int, int]:
    """One glossary entry's balance verdict against a chapter's translation,
    as (kind, tgt_count, floor).

    kind is one of "skip", "drift", "under", "over" or "ok", and is MUTUALLY
    EXCLUSIVE, in that order of precedence: an entry whose canonical rendering
    is absent entirely is drift and never also counted as under-use, because
    the pipeline reports it as an actionable omission while the under-use line
    would describe the same absence as a soft heuristic.

    "skip" covers the two entries the count is meaningless for -- a GUIDE_ONLY
    category (injected as a rendering guide, never a verbatim obligation) and
    an entry with no rendering to match.

    This is the single definition of the usage floor,
    `max(1, ceil(src_count * min_coverage))`, and of what counts as
    contamination. check() reports its three severities from it, and
    lib/verify.py's deterministic stage reads the same verdict through
    underused(), so the pipeline's advisory balance and the whole-book verify
    scan can never drift apart on what "under-used" means.
    """
    if entry.get("category") in GUIDE_ONLY_CATEGORIES:
        return ("skip", 0, 0)
    if not entry.get("translation"):
        return ("skip", 0, 0)
    tgt_count = count_in_target(entry, translated_lines, fuzzy_max)
    floor = max(1, math.ceil(src_count * min_coverage))
    if src_count >= 2 and tgt_count == 0:
        return ("drift", tgt_count, floor)
    if tgt_count < floor:
        return ("under", tgt_count, floor)
    if tgt_count - src_count > max(2, src_count):
        return ("over", tgt_count, floor)
    return ("ok", tgt_count, floor)


def _rows_of_kind(pairs: list[tuple[dict, int]],
                  translated_lines: list[str], min_coverage: float,
                  fuzzy_max: int, wanted: str) -> list[dict]:
    """The rows classify() gave one verdict, as data, in classify()'s own
    order -- so underused() and drifted() cannot disagree about a chapter."""
    rows: list[dict] = []
    for entry, src_count in pairs:
        kind, tgt_count, floor = classify(entry, src_count, translated_lines,
                                         min_coverage, fuzzy_max)
        if kind != wanted:
            continue
        rows.append({
            "source": entry.get("source", ""),
            "translation": entry.get("translation", ""),
            "src_count": src_count,
            "tgt_count": tgt_count,
            "floor": floor,
        })
    return rows


def underused(pairs: list[tuple[dict, int]],
              translated_lines: list[str], min_coverage: float = 0.25,
              fuzzy_max: int = 2) -> list[dict]:
    """The same rows check() warns about as possible under-use, as data.

    One dict per entry classified "under" -- the canonical rendering present in
    the source at least `src_count` times but rendered in the translation
    fewer times than the usage floor. Dict keys: "source", "translation",
    "src_count", "tgt_count", "floor".

    This is the structured face of check()'s middle tier, for a caller that
    must act on an under-used term rather than print it: lib/verify.py hands
    each row to the model that decides whether forcing the pinned rendering
    here would mistranslate, and it needs the counts to do that. Ordering is
    check()'s, because both walk `pairs` once through classify().

    Note what is NOT here: "drift" entries, which drifted() returns. A
    whole-book scan asks the semantic question only about the under-used ones,
    where the count is genuinely ambiguous, because an absent rendering is
    already actionable on its own -- check() reports it as a drift signal and
    the translator is told to use the canonical form. The one caller that
    cannot use that signal is a FAITH rejection (lib/verify.investigate_faith):
    the translation was already produced and already rejected, so the absence
    is not a correction to apply but a question to ask."""
    return _rows_of_kind(pairs, translated_lines, min_coverage, fuzzy_max, "under")


def drifted(pairs: list[tuple[dict, int]],
            translated_lines: list[str], min_coverage: float = 0.25,
            fuzzy_max: int = 2) -> list[dict]:
    """The same rows check() reports as drift signals, as data.

    One dict per entry classified "drift" -- the canonical rendering absent
    from the translation entirely while the source matched it at least twice.
    Same keys and same order as underused(), because both read classify().

    A wholesale refusal of a pinned form is a stronger over-binding candidate
    than partial under-use, not a weaker one: it is the shape a translator
    produces when forcing the entry produces a mistranslation, and no retry can
    fix it while the entry is still pinned. So the FAITH investigation asks
    about both (lib/verify.investigate_faith). A whole-book scan does not --
    paying stage 2 for every absent rendering in the book is not a trade it
    makes -- and neither does the balance advisory, which reports these as
    drift signals itself."""
    return _rows_of_kind(pairs, translated_lines, min_coverage, fuzzy_max, "drift")


def check(pairs: list[tuple[dict, int]],
          translated_lines: list[str], min_coverage: float = 0.25,
          fuzzy_max: int = 2) -> tuple[list[dict], list[str], list[str]]:
    """Compare source-side counts (pairs from glossary.contextual()) against
    target-side counts.

    Three-tier, mostly-advisory semantics. The gate exists to catch DRIFT (a
    term rendered not at all); everything else is a counting heuristic too
    noisy to fail a chapter on (shared-rendering contamination: "Senior"
    inside "Senior Brother"; generic English words: "plot", "system";
    noun-frequency mismatch between Chinese and English).

    Entries whose category is in GUIDE_ONLY_CATEGORIES are skipped entirely:
    they are injected into the translation prompt as rendering guides, and
    counting their short polysemous source strings is noise. Which tier an
    entry falls into is decided by classify(), which lib/verify.py also reads,
    so the two never disagree.

    Returns (drift_signals, warnings, over_count):
    - drift_signals (advisory, surfaced to the FAITH reviewer, who owns the
      pass/fail verdict): one dict per term whose canonical rendering is
      absent entirely while the term appears src >= 2 times -- the reliable
      drift/omission heuristic, though nested-term counting false positives
      are possible, and dismissing those is the reviewer's job. Dict keys:
      "source", "translation", "src_count", "tgt_count", and "message"
      (the human-readable line used for feedback/console output).
    - warnings (advisory, console + trace log): canonical rendering below
      the usage floor ceil(min_coverage * src) -- possible under-use, but
      natural English legitimately repeats nouns less than Chinese.
    - over_count (trace log only): target count above src + max(2, src) -- the
      most false-positive-prone direction; recorded for the human, never
      blocking.
    """
    drift_signals: list[dict] = []
    warnings: list[str] = []
    over_count: list[str] = []
    for entry, src_count in pairs:
        kind, tgt_count, floor = classify(entry, src_count, translated_lines,
                                         min_coverage, fuzzy_max)
        if kind == "skip":
            continue
        term = entry.get("source", "")
        expected = entry.get("translation", "")
        if kind == "drift":
            drift_signals.append({
                "source": term,
                "translation": expected,
                "src_count": src_count,
                "tgt_count": tgt_count,
                "message": (
                    f"Glossary term '{term}' must be rendered verbatim as "
                    f"\"{expected}\" (source has {src_count} occurrence(s), "
                    "translation has 0) — use the exact canonical rendering "
                    "wherever the term appears; do not paraphrase, hyphenate, "
                    "or vary it."
                ),
            })
        elif kind == "under":
            warnings.append(
                f"'{term}' rendered {tgt_count}/{src_count} times "
                f"(floor {floor}) — possible under-use."
            )
        elif kind == "over":
            over_count.append(
                f"'{term}' rendered {tgt_count}/{src_count} times "
                f"(ceiling {src_count + max(2, src_count)}) — possible count "
                "contamination from shared renderings or generic words."
            )
    return drift_signals, warnings, over_count
