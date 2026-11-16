# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""The speech checks, their tiers, and the record they write.

    uv run tests/test_speech_checks.py
"""
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import findings
from lib.errors import PipelineError

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def codes(found: list[dict]) -> list[str]:
    return [f["check"] for f in found]


def case_c1_refuses() -> None:
    """The source opens a speech and the translation does not: a refusal."""
    source = ["“你听我说。", "第二段。"]
    translation = ["You listen to me.", "Second."]
    found = findings.check_speech(source, translation)
    check("C1 fires", codes(found) == ["C1"], repr(found))
    check("C1 refuses", found and found[0]["tier"] == "refuse", repr(found))
    check("the finding names the paragraph", found and found[0]["paragraph"] == 1,
          repr(found))


def case_c1_quiet_when_opened() -> None:
    """A translation that opens the speech is fine."""
    source = ["“你听我说。", "第二段。"]
    translation = ['"You listen to me."', "Second."]
    found = findings.check_speech(source, translation)
    check("C1 is quiet", codes(found) == [], repr(found))


def case_c1_ignores_a_sound() -> None:
    """A quoted sound is not a speech, and does not open one."""
    source = ["“砰”的一声，门被踹开了。", "第二段。"]
    translation = ["With a loud bang, the door was kicked open.", "Second."]
    found = findings.check_speech(source, translation)
    check("a quoted sound does not open a speech", "C1" not in codes(found),
          repr(found))


def case_c2_split_speech() -> None:
    """A speech that continues must not start a new one."""
    source = ["“你听我说。", "这件事不能急。", "你明白吗？”"]
    translation = ['"You listen to me.', '"This cannot be rushed.', 'Do you understand?"']
    found = findings.check_speech(source, translation)
    check("C2 fires on the continuation", "C2" in codes(found), repr(found))
    check("C2 reports",
          [f for f in found if f["check"] == "C2"][0]["tier"] == "report", repr(found))
    check("the same defect is also seen by C4 (the source has no mark here)",
          "C4" in codes(found), repr(found))


def case_c3_missing_close() -> None:
    """A source that closes a speech and a translation with no mark at all."""
    source = ["“你听我说。”", "第二段。"]
    translation = ["You listen to me", "Second."]
    found = findings.check_speech(source, translation)
    check("C3 fires when the translation carries no mark",
          "C3" in codes(found), repr(found))


def case_c4_stray_quote() -> None:
    """A source with no mark and a translation that gained one."""
    source = ["他站在门口。", "第二段。"]
    translation = ['He stood in the "doorway".', "Second."]
    found = findings.check_speech(source, translation)
    check("C4 fires", "C4" in codes(found), repr(found))


def case_tiers() -> None:
    """Only C1 refuses; the rest are recorded and the chapter is delivered."""
    source = ["“你听我说。", "他走了。", "“再见。”"]
    translation = ["You listen to me.", 'He left the "door".', "Goodbye."]
    found = findings.check_speech(source, translation)
    tiers = {f["check"]: f["tier"] for f in found}
    check("C1 is the only refusing tier",
          tiers.get("C1") == "refuse"
          and all(t == "report" for c, t in tiers.items() if c != "C1"),
          repr(tiers))


def case_feedback() -> None:
    """The refusal names the paragraph and both texts."""
    source = ["“你听我说。"]
    translation = ["You listen to me."]
    found = findings.check_speech(source, translation)
    lines = findings.feedback_lines(found, source, translation)
    check("the feedback names the paragraph and both texts",
          len(lines) == 1 and "Line 1" in lines[0]
          and "你听我说" in lines[0] and "You listen" in lines[0],
          repr(lines))


def case_speech_structure() -> None:
    """The structure the translator is told about."""
    source = ["“你听我说。", "这件事不能急。", "你明白吗？”", "他走了。"]
    section = findings.speech_structure(source, 1)
    check("the structure names the speech's run",
          "opens at 1" in section and "closes at 3" in section, repr(section))
    check("the structure says the pieces between carry no marks",
          "without marks" in section or "continue it" in section, repr(section))
    check("narration is not mentioned", "line 4" not in section, repr(section))


def case_speech_structure_self_contained() -> None:
    """A speech that opens and closes in one paragraph needs no guidance."""
    check("a complete speech gets no section",
          findings.speech_structure(["“你好。”", "第二段。"], 1) == "")


def case_speech_structure_empty() -> None:
    """No dialogue, no section: a rule with no subject only costs tokens."""
    check("a source without dialogue gets no section",
          findings.speech_structure(["他走了。", "第二段。"], 1) == "")


def case_speech_structure_offsets() -> None:
    """The section speaks in the prompt's line numbers."""
    section = findings.speech_structure(["“你好。", "第二段。", "再见。”"], 40)
    check("the section uses the chunk's own line numbers",
          "opens at 40" in section and "closes at 42" in section, repr(section))


def case_record() -> None:
    """The record is written, read back, and a chapter's entries replaced."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = ["“你听我说。"]
        translation = ["You listen to me."]
        found = findings.check_speech(source, translation)
        findings.write_findings(root, "CHAPTER_0001.md", found, source, translation)
        records = findings.read_findings(root)
        check("the record is written", len(records) == 1, repr(records))
        check("the record names the chapter and the check",
              records[0]["chapter"] == "CHAPTER_0001.md"
              and records[0]["check"] == "C1", repr(records))
        check("the record carries both texts",
              "你听我说" in records[0]["source"]
              and "You listen" in records[0]["translation"], repr(records))
        # a chapter translated again replaces its own entries
        findings.write_findings(root, "CHAPTER_0001.md", [], source, translation)
        check("a chapter's earlier entries are replaced",
              findings.read_findings(root) == [], repr(findings.read_findings(root)))
        # another chapter's entries are kept
        findings.write_findings(root, "CHAPTER_0002.md", found, source, translation)
        records = findings.read_findings(root)
        check("another chapter's entries are kept",
              len(records) == 1 and records[0]["chapter"] == "CHAPTER_0002.md",
              repr(records))
        # every line is a JSON object
        body = findings.findings_path(root).read_text(encoding="utf-8")
        check("every line is a JSON object",
              all(json.loads(line) for line in body.splitlines() if line.strip()))


def case_record_survives_a_torn_line() -> None:
    """A torn line is skipped, not fatal."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = findings.findings_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"chapter": "CHAPTER_0001.md"}\n{not json\n',
                        encoding="utf-8")
        records = findings.read_findings(root)
        check("a torn line is skipped", len(records) == 1, repr(records))


def main() -> int:
    for case in (case_c1_refuses, case_c1_quiet_when_opened,
                 case_c1_ignores_a_sound, case_c2_split_speech,
                 case_c3_missing_close, case_c4_stray_quote, case_tiers,
                 case_feedback, case_speech_structure,
                 case_speech_structure_empty, case_speech_structure_offsets,
                 case_record, case_record_survives_a_torn_line):
        case()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
