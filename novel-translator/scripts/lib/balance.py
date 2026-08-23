"""Glossary balance checking between source text and its translation."""

import math
import re

CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")


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

    Targets are the deduped translation + alt_translations. If any target
    contains CJK characters, count exact substrings. Otherwise: multi-word
    targets count as case-insensitive, whitespace-flexible word-boundary
    phrases (glossary phrases are supposed to be rendered verbatim);
    single-word targets count as tokens equal to the (lowercased) target or
    within levenshtein distance fuzzy_max of a target of length >= 5.
    """
    targets: list[str] = []
    for target in [entry.get("translation", "")] + list(entry.get("alt_translations") or []):
        if target and target not in targets:
            targets.append(target)
    if not targets:
        return 0
    text = "\n".join(lines)
    if any(CJK_RE.search(target) for target in targets):
        return sum(text.count(target) for target in targets)
    low = text.lower()
    tokens = _TOKEN_RE.findall(low)
    total = 0
    for target in targets:
        words = target.lower().split()
        if not words:
            continue
        if len(words) > 1:
            # Phrase match with word boundaries; the final word may take a
            # natural inflection ("spirit stone" also matches "spirit stones").
            head = r"\s+".join(re.escape(w) for w in words[:-1])
            tail = re.escape(words[-1]) + r"(?:es|s|ed|ing)?"
            pattern = r"\b" + (head + r"\s+" if head else "") + tail + r"\b"
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


def check(pairs: list[tuple[dict, int]], source_body: str,
          translated_lines: list[str], min_coverage: float = 0.25,
          fuzzy_max: int = 2) -> tuple[list[dict], list[str], list[str]]:
    """Compare source-side counts (pairs from glossary.contextual()) against
    target-side counts. source_body is accepted for interface symmetry (the
    source counts arrive via pairs).

    Three-tier, mostly-advisory semantics. The gate exists to catch DRIFT (a
    term rendered not at all); everything else is a counting heuristic too
    noisy to fail a chapter on (shared-rendering contamination: "Senior"
    inside "Senior Brother"; generic English words: "plot", "system";
    noun-frequency mismatch between Chinese and English).

    Returns (failures, warnings, info):
    - failures (hard, retry-worthy): one dict per term whose canonical
      rendering is absent entirely while the term appears src >= 2 times —
      the reliable drift/omission signal. Dict keys: "source", "translation",
      "src_count", "tgt_count", and "message" (the human-readable line used
      for feedback/console output).
    - warnings (advisory, console + trace log): canonical rendering below
      the usage floor ceil(min_coverage * src) — possible under-use, but
      natural English legitimately repeats nouns less than Chinese.
    - info (trace log only): target count above src + max(2, src) — the
      most false-positive-prone direction; recorded for the human, never
      blocking.
    """
    failures: list[dict] = []
    warnings: list[str] = []
    info: list[str] = []
    for entry, src_count in pairs:
        tgt_count = count_in_target(entry, translated_lines, fuzzy_max)
        floor = max(1, math.ceil(src_count * min_coverage))
        extra = tgt_count - src_count
        term = entry['source']
        expected = entry['translation']
        if src_count >= 2 and tgt_count == 0:
            failures.append({
                "source": term,
                "translation": expected,
                "src_count": src_count,
                "tgt_count": tgt_count,
                "message": (
                    f"Glossary term '{term}' (expected '{expected}'): source has "
                    f"{src_count} occurrence(s), translation has 0 — canonical "
                    "rendering never used (drift or omission)."
                ),
            })
        elif tgt_count < floor:
            warnings.append(
                f"'{term}' rendered {tgt_count}/{src_count} times "
                f"(floor {floor}) — possible under-use."
            )
        elif extra > max(2, src_count):
            info.append(
                f"'{term}' rendered {tgt_count}/{src_count} times "
                f"(ceiling {src_count + max(2, src_count)}) — possible count "
                "contamination from shared renderings or generic words."
            )
    return failures, warnings, info
