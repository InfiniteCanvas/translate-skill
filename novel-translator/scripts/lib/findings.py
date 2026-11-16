"""Speech and emphasis fidelity: the checks, their tiers, and the record.

A mirror marks a speech on its FIRST and LAST pieces; the pieces in
between carry no quotation marks at all. That is the source's shape, and
it is the shape the translation has to reproduce - a speech split into
per-paragraph quotes reads wrong, and a speech rendered as narration
reads worse.

The checks below compare the source's structure with the translation's,
paragraph by paragraph, and they are tiered by what they can establish:

- **refuse** - the contradiction is certain. The source opens a speech
  and the translation does not open one: the reader sees narration where
  the book has someone talking. The chapter is refused, and the gate's
  feedback tells the translator exactly which paragraph contradicts which.
- **report** - the contradiction is real but not certain. A stray quote
  where the source has none can be a possessive rendered as a quote
  (damage) or a title the English quotes (correct); a dropped closing
  mark can be a dropped emphasis (correct) or a dropped speech (damage).
  Those are recorded against the chapter, and the chapter is delivered.

The refusal tier is calibrated against a real book before it is allowed
to refuse anything (see `scripts/calibrate_speech.py`), and the run
stops rather than re-translating a whole book when the firing rate says
the check itself is misfiring.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import project

# The checks, in the order they are reported.
CHECKS = ("C1", "C2", "C3", "C4")

# What each check means, for the record and for the operator's tools.
CHECK_MEANING = {
    "C1": "the source opens a speech here and the translation does not open one",
    "C2": "the source continues an open speech here and the translation starts a new one",
    "C3": "the source closes a speech here and the translation carries no mark at all",
    "C4": "the source has no mark here and the translation gained one",
}

# Which tier each check belongs to. Only C1 is certain enough to refuse.
CHECK_TIER = {"C1": "refuse", "C2": "report", "C3": "report", "C4": "report"}

_QUOTE_CHARS = ('"', "“", "”")
_CJK_OPEN = "“"
_CJK_CLOSE = "”"


def _marks(text: str) -> tuple[int, int]:
    """How many dialogue marks a text carries, and where its first ones sit."""
    opens = text.count(_CJK_OPEN)
    closes = text.count(_CJK_CLOSE)
    return opens, closes


def _translation_marks(text: str) -> int:
    """How many dialogue marks a translation carries, in either quote style."""
    return sum(text.count(ch) for ch in _QUOTE_CHARS)


def _translation_opens(text: str) -> bool:
    """Whether a translation opens a dialogue with a mark."""
    stripped = text.strip()
    return bool(stripped) and stripped[0] in ('"', "“")


def check_speech(source_lines: list[str], translated_lines: list[str]) -> list[dict]:
    """The findings for one chapter's paragraph pairs.

    Each finding is {"check", "tier", "paragraph", "what"} where `paragraph`
    is the 1-based line number in the SOURCE (the number the translator sees)
    and `what` names the check in words. The two texts are not repeated
    here: the record carries them, and the gate's feedback carries them
    to the translator.
    """
    findings: list[dict] = []
    speech_open = False
    for index, (source, translated) in enumerate(zip(source_lines, translated_lines)):
        src = source.strip()
        dst = translated.strip()
        opens, closes = _marks(src)
        leads = src.startswith(_CJK_OPEN)
        # The paragraph's first quote is closed inside it -> it is a complete
        # speech or an emphasis, not a speech that runs on.
        paired_inside = (
            opens > 0 and closes > 0 and src.index(_CJK_CLOSE) > src.index(_CJK_OPEN)
        )
        opens_speech = leads and not paired_inside
        translation_opens = _translation_opens(dst)
        translation_marks = _translation_marks(dst)

        if opens_speech and not translation_opens:
            findings.append(_finding("C1", index))
        elif speech_open and not leads and translation_opens:
            findings.append(_finding("C2", index))
        if closes > 0 and translation_marks == 0:
            findings.append(_finding("C3", index))
        if opens == 0 and closes == 0 and translation_marks > 0:
            findings.append(_finding("C4", index))

        if leads:
            speech_open = not (closes > 0)
        elif closes > 0:
            speech_open = False
    return findings


def _finding(check: str, index: int) -> dict:
    return {
        "check": check,
        "tier": CHECK_TIER[check],
        "paragraph": index + 1,
        "what": CHECK_MEANING[check],
    }


# What a firing is, by the shape of the two texts. The classes are the
# point of the calibration: a check that cannot say WHICH kind of thing
# it found cannot be judged.
FIRING_CLASSES = {
    "C1": "a speech the source opens, dropped - a defect",
    "C2": "one speech split into per-paragraph quotes - a defect",
    "C3_emphasis": "the source's own emphasis quote, dropped on purpose - a false positive",
    "C3_speech": "a speech's closing mark dropped - a defect",
    "C4_possessive": "a quote standing right after a word, where the source has none - a stray quote (a possessive rendered as one) - a defect",
    "C4_quoted": "a quote standing after a space - a title or a term the English quotes - a false positive",
}


def classify_finding(check: str, source: str, translated: str) -> str:
    """Which kind of thing a firing is, from the shape of the two texts."""
    src = source.strip()
    dst = translated.strip()
    if check == "C1":
        return "C1"
    if check == "C2":
        return "C2"
    if check == "C3":
        # The source's closing mark closes an emphasis pair (an opener that
        # is not at the paragraph's start) when the paragraph does not open
        # with it - that is a word quoted inside the narration, and dropping
        # it is a rendering choice, not a lost speech.
        opens = src.count(_CJK_OPEN)
        closes = src.count(_CJK_CLOSE)
        opens_mid = opens > 0 and not src.startswith(_CJK_OPEN)
        return "C3_emphasis" if (opens_mid and closes > 0) else "C3_speech"
    # C4: a quote right after a word character is a possessive that was
    # rendered as a quote; a quote after a space is a title or a term.
    for index, char in enumerate(dst):
        if char in ('"', "“") and index > 0 and (dst[index - 1].isalnum()):
            return "C4_possessive"
    return "C4_quoted"


def refusal_findings(findings: list[dict]) -> list[dict]:
    """The findings that block the chapter."""
    return [f for f in findings if f["tier"] == "refuse"]


def report_findings(findings: list[dict]) -> list[dict]:
    """The findings that are recorded and the chapter is delivered anyway."""
    return [f for f in findings if f["tier"] == "report"]


def feedback_lines(findings: list[dict], source_lines: list[str],
                   translated_lines: list[str]) -> list[str]:
    """The gate feedback for the refusal tier: the paragraph and both texts."""
    lines: list[str] = []
    for finding in refusal_findings(findings):
        index = finding["paragraph"] - 1
        source = source_lines[index].strip() if index < len(source_lines) else ""
        translated = translated_lines[index].strip() if index < len(translated_lines) else ""
        lines.append(
            f"Line {finding['paragraph']}: {finding['what']} - "
            f"source: {source} | translation: {translated}"
        )
    return lines


def speech_structure(lines: list[str], first_line: int = 1) -> str:
    """How the source marks its speech, in words the translator can use.

    The mirror marks a speech on its first and last pieces only, so the
    pieces in between are bare. A translator that puts a quote on every
    piece splits one speech into several, and a translator that quotes a
    bare piece turns narration into speech. The structure is described
    here in the chunk's own line numbers, because the prompt's line
    numbers are the ones the model must echo.

    Empty when the source carries no dialogue at all - there is nothing
    to say, and a rule with no subject only costs tokens.
    """
    runs: list[tuple[int, int]] = []
    open_at: int | None = None
    for index, line in enumerate(lines):
        text = line.strip()
        opens, closes = _marks(text)
        leads = text.startswith(_CJK_OPEN)
        paired_inside = (
            opens > 0 and closes > 0 and text.index(_CJK_CLOSE) > text.index(_CJK_OPEN)
        )
        number = first_line + index
        if leads and not paired_inside:
            if open_at is None:
                open_at = number
        if closes > 0 and open_at is not None:
            runs.append((open_at, number))
            open_at = None
    if open_at is not None:
        runs.append((open_at, first_line + len(lines) - 1))
    if not runs:
        return ""

    out = [
        "The source marks each speech on its FIRST and LAST piece only; the "
        "pieces in between carry no quotation marks. Reproduce that exactly: "
        "open a quotation where the source opens one, close it where the "
        "source closes it, and give a piece in between no quotation of its own."
    ]
    for start, end in runs:
        if start == end:
            out.append(f"- line {start}: a complete speech, open and close it here")
        else:
            out.append(
                f"- lines {start}-{end}: one speech - it opens at {start}, closes "
                f"at {end}, and the lines between continue it without marks"
            )
    return "\n".join(out)


def findings_path(project_dir: Path) -> Path:
    """Where the findings record lives."""
    return project.paths(project_dir)["logs"] / "findings.jsonl"


def read_findings(project_dir: Path) -> list[dict]:
    """Every recorded finding, in file order. Lenient: a torn or unreadable
    record yields what can be read, not an exception."""
    path = findings_path(project_dir)
    if not path.is_file():
        return []
    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records


def write_findings(project_dir: Path, chapter: str,
                    findings: list[dict], source_lines: list[str] | None = None,
                    translated_lines: list[str] | None = None) -> Path:
    """Record a chapter's findings, replacing any earlier record of them.

    The record is rewritten whole and written atomically, so a run killed
    mid-write cannot leave a torn file, and a chapter that is translated
    again leaves no stale findings behind - a finding that no longer
    fires is gone, which is what the operator's list has to show.
    """
    path = findings_path(project_dir)
    kept = [record for record in read_findings(project_dir)
            if record.get("chapter") != chapter]
    for finding in findings:
        index = int(finding["paragraph"]) - 1
        record = {
            "chapter": chapter,
            "paragraph": finding["paragraph"],
            "check": finding["check"],
            "tier": finding["tier"],
            "what": finding["what"],
        }
        if source_lines is not None and index < len(source_lines):
            record["source"] = source_lines[index].strip()[:120]
        if translated_lines is not None and index < len(translated_lines):
            record["translation"] = translated_lines[index].strip()[:120]
        record["class"] = classify_finding(
            finding["check"], record.get("source", ""), record.get("translation", ""))
        kept.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in kept)
    project.atomic_write_text(path, body, newline="\n")
    return path
