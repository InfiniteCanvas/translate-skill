# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""calibrate_speech.py - measure the speech checks against a real book.

    uv run scripts/calibrate_speech.py --project <book-dir> [--out FILE]

The refusal tier is only allowed to refuse a chapter after it has been
measured against a book the workflow has actually produced. This script
does the measuring: it pairs a book's source chapters with their
translations, runs the checks, and reports

- how many paragraphs each check fires on, and the rate;
- every firing BY REFERENCE - chapter, line, check, tier - never by
  quotation, because the report has to be filed in a round brief and a
  brief lives in a repository;
- the stricter variants of the two ambiguous checks, so the choice of
  definition is a number rather than an opinion;
- the rate the refusal bound should be set from.

It reads the book; it writes nothing except the optional report file.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import findings, project  # noqa: E402


def paired_chapters(project_dir: Path) -> list[tuple[str, list[str], list[str]]]:
    """The chapters whose source and translation have the same line count.

    A chapter whose counts differ is left out of the measurement: the
    pipeline refuses such a chapter (its line counts must match), so it
    is not a book the workflow delivered.
    """
    source_dir = project.paths(project_dir)["source"]
    translated_dir = project.paths(project_dir)["translated"]
    pairs: list[tuple[str, list[str], list[str]]] = []
    for chapter in project.discover(project_dir):
        translated_path = translated_dir / chapter.file
        if not translated_path.is_file():
            continue
        _, source_body = project.read_chapter(chapter.path)
        _, translated_body = project.read_chapter(translated_path)
        source_lines = source_body.split("\n")
        translated_lines = translated_body.split("\n")
        if len(source_lines) != len(translated_lines):
            continue
        pairs.append((chapter.file, source_lines, translated_lines))
    return pairs


def stricter_variants(source_lines: list[str], translated_lines: list[str]) -> dict:
    """The two ambiguous checks under their stricter readings.

    C3 measured on "the source paragraph ENDS with a closing mark" and C4
    on "the translation OPENS a quote" - the readings that exclude the
    legitimate renderings the looser ones admit.
    """
    strict_c3 = 0
    strict_c4 = 0
    for source, translated in zip(source_lines, translated_lines):
        src = source.strip()
        dst = translated.strip()
        opens = src.count("“")
        closes = src.count("”")
        marks = sum(dst.count(ch) for ch in ('"', "“", "”"))
        if src.endswith("”") and marks == 0:
            strict_c3 += 1
        if opens == 0 and closes == 0 and dst[:1] in ('"', "“"):
            strict_c4 += 1
    return {"C3_strict": strict_c3, "C4_strict": strict_c4}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, help="the book directory")
    ap.add_argument("--out", default="", help="write the report to this file")
    ap.add_argument("--bound", type=float, default=0.05,
                    help="the refusal rate bound to suggest (default 0.05)")
    args = ap.parse_args()

    project_dir = Path(args.project).expanduser().resolve()
    pairs = paired_chapters(project_dir)
    if not pairs:
        print(f"[FAIL] no chapter in {project_dir} has both a source and a "
              f"translation of the same length")
        return 1

    per_check: dict[str, int] = {check: 0 for check in findings.CHECKS}
    per_class: dict[str, int] = {}
    per_check_strict = {"C3_strict": 0, "C4_strict": 0}
    lines_total = 0
    firings: list[tuple[str, int, str, str, str]] = []

    for name, source_lines, translated_lines in pairs:
        lines_total += len(source_lines)
        found = findings.check_speech(source_lines, translated_lines)
        for finding in found:
            per_check[finding["check"]] += 1
            index = finding["paragraph"] - 1
            kind = findings.classify_finding(
                finding["check"],
                source_lines[index] if index < len(source_lines) else "",
                translated_lines[index] if index < len(translated_lines) else "",
            )
            per_class[kind] = per_class.get(kind, 0) + 1
            firings.append((name, finding["paragraph"], finding["check"],
                            finding["tier"], kind))
        variants = stricter_variants(source_lines, translated_lines)
        for key, value in variants.items():
            per_check_strict[key] += value

    print(f"[calibrate] {len(pairs)} chapter(s), {lines_total} paragraph pair(s)")
    print()
    print("check  firings   rate      tier    what")
    for check in findings.CHECKS:
        count = per_check[check]
        rate = count / lines_total if lines_total else 0.0
        print(f"{check:<6} {count:>8}   {rate:>7.4%}   {findings.CHECK_TIER[check]:<6} "
              f"{findings.CHECK_MEANING[check]}")
    print()
    print("the stricter readings of the two ambiguous checks:")
    for key, count in per_check_strict.items():
        rate = count / lines_total if lines_total else 0.0
        print(f"{key:<10} {count:>8}   {rate:>7.4%}")
    print()
    print("what each firing is, by class:")
    for kind, count in sorted(per_class.items()):
        print(f"  {kind:<16} {count:>4}   {findings.FIRING_CLASSES.get(kind, kind)}")
    print()
    print(f"every firing, by reference (chapter, line, check, tier, class):")
    for name, line, check, tier, kind in firings:
        print(f"  {name} line {line} {check} {tier} {kind}")
    print()
    refusal_rate = per_check["C1"] / lines_total if lines_total else 0.0
    print(f"the refusal tier fires on {refusal_rate:.4%} of paragraphs")
    print(f"a bound of {args.bound:.2%} stops the run when a chapter's refusal "
          f"rate exceeds it ({args.bound:.2%} of a 90-paragraph chapter is "
          f"{int(args.bound * 90)} firings)")

    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(
                f"{name} line {line} {check} {tier} {kind}"
                for name, line, check, tier, kind in firings
            ) + "\n",
            encoding="utf-8",
        )
        print(f"[ok] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
