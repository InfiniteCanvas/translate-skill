"""Regression tests for review.py (advisory glossary quality review).

Covers the deterministic heuristic tier (duplicate / collision /
wrong_language / category / variant), the model tier's row normalization
(unknown kind -> "other", bad severity -> "info", empty reason dropped,
source must be in the batch), the heuristic-wins merge with borrowed
suggestions, per-batch failure resilience, and apply_fixes()' full guard
matrix (origin/severity/kind gating, conflict detection, category and
target-language validation, in-place mutation preserving every other
field).

Model-tier cases stub lib.client.chat via review.client (the call site is
a module-attribute lookup, so the swap takes effect); no network, no
repo-fixture mutation -- every glossary file lives in a TemporaryDirectory.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_glossary_review.py
"""

import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary, review  # noqa: E402

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


def make_glossary(*entries: dict) -> dict:
    return {"terms": list(entries)}


def write_glossary(dir_path: Path, entries: list) -> Path:
    """Write {"terms": entries} to <dir_path>/glossary.json; return its path."""
    project_dir = Path(dir_path)
    glossary.save(project_dir, {"terms": list(entries)})
    return project_dir / "glossary.json"


def kind_findings(g: dict, kind: str) -> list[dict]:
    """Heuristic findings of one kind, as (source, severity) tuples."""
    return [f for f in review._heuristic_findings(g) if f["kind"] == kind]


def pick(findings: list, source: str, kind: str) -> list[dict]:
    return [f for f in findings if f["source"] == source and f["kind"] == kind]


def review_cfg() -> dict:
    """Minimal cfg review_glossary accepts; glossary provider is never called."""
    return {
        "source_lang": "zh",
        "target_lang": "en",
        "log_llm": False,
        "providers": {"glossary": {"base_url": "http://unused", "model": None}},
    }


def run_with_fake_chat(project_dir: Path, rows_by_call: list,
                       batch_size: int = 40) -> tuple[dict, list[str]]:
    """review_glossary with lib.client.chat stubbed per call.

    rows_by_call: one entry per expected call -- either a dict (returned as
    JSON) or an Exception instance (raised). Returns (result, prompts)."""
    prompts: list[str] = []

    def fake_chat(provider_cfg, prompt, json_schema=None, temperature=None,
                  max_tokens=None, meta_hook=None):
        prompts.append(prompt)
        step = rows_by_call[len(prompts) - 1]
        if isinstance(step, Exception):
            raise step
        return json.dumps(step, ensure_ascii=False)

    orig = review.client.chat
    review.client.chat = fake_chat
    try:
        result = review.review_glossary(project_dir, review_cfg(),
                                        batch_size=batch_size)
    finally:
        review.client.chat = orig
    return result, prompts


def case_1_duplicate() -> None:
    """One string owned by 2+ entries: warn on every owner after the first."""
    g = make_glossary(
        {"source": "小丫", "translation": "little maid"},
        {"source": "裴小丫", "translation": "Pei Xiaoya", "variants": ["小丫"]},
    )
    dups = kind_findings(g, "duplicate")
    got = [(f["source"], f["severity"]) for f in dups]
    check("1a duplicate: warn on 裴小丫 (later owner of 小丫)",
          got == [("裴小丫", "warn")], f"dups={got}")
    check("1b duplicate: first owner 小丫 gets no finding",
          all(f["source"] != "小丫" for f in dups))
    if dups:
        check("1c duplicate: origin heuristic",
              dups[0]["origin"] == "heuristic", f"origin={dups[0]['origin']!r}")


def case_2_collision() -> None:
    """Same translation (case-insensitive) on distinct sources: info, later one."""
    g = make_glossary(
        {"source": "灵石", "translation": "Spirit Stone"},
        {"source": "仙石", "translation": "spirit stone"},
    )
    cols = kind_findings(g, "collision")
    got = [(f["source"], f["severity"]) for f in cols]
    check("2a collision: info on later entry 仙石 only",
          got == [("仙石", "info")], f"collisions={got}")
    check("2b collision: first entry 灵石 gets no finding",
          all(f["source"] != "灵石" for f in cols))


def case_3_wrong_language() -> None:
    """CJK translation on CJK source, or translation == source: warn."""
    g = make_glossary(
        {"source": "灵根", "translation": "灵力"},
        {"source": "Nasa", "translation": "nasa"},
        {"source": "灵石", "translation": "spirit stone"},
    )
    wl = kind_findings(g, "wrong_language")
    by_source = {f["source"]: f["severity"] for f in wl}
    check("3a wrong_language: CJK translation on CJK source -> warn",
          by_source.get("灵根") == "warn", f"wl={by_source}")
    check("3b wrong_language: identical Latin 'Nasa'/'nasa' -> warn",
          by_source.get("Nasa") == "warn", f"wl={by_source}")
    check("3c wrong_language: clean CJK->English entry -> no finding",
          "灵石" not in by_source and len(wl) == 2, f"wl={by_source}")


def case_4_category() -> None:
    """Missing category -> info; unknown category -> warn; known -> silent."""
    g = make_glossary(
        {"source": "灵石", "translation": "spirit stone"},
        {"source": "灵根", "translation": "spirit root", "category": "blah"},
        {"source": "天雷宗", "translation": "Heavenly Thunder Sect",
         "category": "place"},
    )
    cats = kind_findings(g, "category")
    by_source = {f["source"]: f["severity"] for f in cats}
    check("4a category: missing -> info", by_source.get("灵石") == "info",
          f"cats={by_source}")
    check("4b category: unknown 'blah' -> warn", by_source.get("灵根") == "warn",
          f"cats={by_source}")
    check("4c category: known 'place' -> no finding",
          "天雷宗" not in by_source and len(cats) == 2, f"cats={by_source}")


def case_5_variant() -> None:
    """CJK source with a non-CJK variant: info; CJK variant: silent."""
    g = make_glossary(
        {"source": "裴家村", "translation": "Pei Family Village",
         "variants": ["Pei Family Village"], "category": "place"},
        {"source": "灵根", "translation": "spirit root",
         "variants": ["靈根"], "category": "skill"},
    )
    vars_ = kind_findings(g, "variant")
    got = [(f["source"], f["severity"]) for f in vars_]
    check("5 variant: non-CJK variant on CJK source -> info (裴家村 only)",
          got == [("裴家村", "info")], f"variants={got}")


def case_6_model_normalization() -> None:
    """Row normalization: unknown kind, bad severity, empty reason, bad source."""
    with tempfile.TemporaryDirectory() as td:
        project_dir = Path(td)
        write_glossary(project_dir, [
            {"source": "灵石", "translation": "spirit stone", "category": "item"},
            {"source": "天雷宗", "translation": "Heavenly Thunder Sect",
             "category": "org"},
        ])
        rows = {"findings": [
            {"source": "灵石", "kind": "mistranslation", "severity": "warn",
             "reason": "r1", "suggestion": "Fixed Name",
             "action": "Set the translation field to the suggested name."},
            {"source": "灵石", "kind": "bizarre_kind", "severity": "info",
             "reason": "r2", "suggestion": "", "action": 123},
            {"source": "天雷宗", "kind": "mistranslation", "severity": "garbage",
             "reason": "r3", "suggestion": "Sect of Thunder"},
            {"source": "灵石", "kind": "variant", "severity": "info",
             "reason": "", "suggestion": ""},
            {"source": "不存在", "kind": "mistranslation", "severity": "warn",
             "reason": "r5", "suggestion": "X"},
        ]}
        result, prompts = run_with_fake_chat(project_dir, [rows])
        fs = result["findings"]
        check("6a model: one batch, one call", result["batches"] == 1
              and len(prompts) == 1,
              f"batches={result['batches']} calls={len(prompts)}")
        check("6b model: prompt renders template + entries",
              len(prompts) == 1 and "[Glossary Review]" in prompts[0]
              and "灵石" in prompts[0])
        check("6c model: 5 rows -> 3 findings (2 dropped)", len(fs) == 3,
              f"findings={[(f['source'], f['kind'], f['severity']) for f in fs]}")
        ok = pick(fs, "灵石", "mistranslation")
        check("6d model: valid warn row kept verbatim",
              len(ok) == 1 and ok[0]["severity"] == "warn"
              and ok[0]["suggestion"] == "Fixed Name"
              and ok[0]["origin"] == "model", f"row={ok}")
        check("6d2 model: action passes through (non-str -> empty)",
              len(ok) == 1 and ok[0]["action"] == "Set the translation field to the suggested name."
              and pick(fs, "灵石", "other")[0]["action"] == "",
              f"actions={[f.get('action') for f in fs]}")
        other = pick(fs, "灵石", "other")
        check("6e model: unknown kind normalized to 'other'",
              len(other) == 1 and other[0]["severity"] == "info"
              and other[0]["origin"] == "model", f"row={other}")
        demoted = pick(fs, "天雷宗", "mistranslation")
        check("6f model: severity 'garbage' demoted to 'info'",
              len(demoted) == 1 and demoted[0]["severity"] == "info"
              and demoted[0]["suggestion"] == "Sect of Thunder", f"row={demoted}")
        check("6g model: empty-reason and unknown-source rows dropped",
              not pick(fs, "灵石", "variant") and not pick(fs, "不存在", "mistranslation")
              and all(f["reason"] for f in fs))


def case_7_merge_borrows_suggestion() -> None:
    """Heuristic wins on (source, kind) but borrows the model's suggestion."""
    with tempfile.TemporaryDirectory() as td:
        project_dir = Path(td)
        write_glossary(project_dir, [
            {"source": "灵根", "translation": "灵根", "category": "skill"},
        ])
        rows = {"findings": [
            {"source": "灵根", "kind": "wrong_language", "severity": "warn",
             "reason": "model says wrong", "suggestion": "spirit root",
             "action": "Replace the untranslated translation with the suggestion."},
        ]}
        result, _prompts = run_with_fake_chat(project_dir, [rows])
        wl = pick(result["findings"], "灵根", "wrong_language")
        check("7a merge: exactly one wrong_language finding", len(wl) == 1,
              f"findings={result['findings']}")
        if wl:
            f = wl[0]
            check("7b merge: heuristic wins (origin heuristic)",
                  f["origin"] == "heuristic", f"origin={f['origin']!r}")
            check("7c merge: model suggestion borrowed",
                  f["suggestion"] == "spirit root", f"suggestion={f['suggestion']!r}")
            check("7d merge: severity stays warn", f["severity"] == "warn",
                  f"severity={f['severity']!r}")
            check("7e merge: borrowed suggestion is fixable",
                  f.get("fixable") is True, f"fixable={f.get('fixable')!r}")
            check("7f merge: model action borrowed too",
                  f.get("action") == "Replace the untranslated translation with the suggestion.",
                  f"action={f.get('action')!r}")
        else:
            for name in ("7b merge: heuristic wins (origin heuristic)",
                         "7c merge: model suggestion borrowed",
                         "7d merge: severity stays warn",
                         "7e merge: borrowed suggestion is fixable",
                         "7f merge: model action borrowed too"):
                check(name, False, "no finding to inspect")


def case_8_batch_resilience() -> None:
    """A failed batch lands in batch_errors; other batches' findings survive."""
    entries = [
        {"source": "灵一", "translation": "first spirit", "category": "item"},
        {"source": "灵二", "translation": "second spirit", "category": "item"},
        {"source": "灵三", "translation": "third spirit", "category": "item"},
        {"source": "灵四", "translation": "fourth spirit", "category": "item"},
        {"source": "灵五", "translation": "fifth spirit", "category": "item"},
    ]
    with tempfile.TemporaryDirectory() as td:
        project_dir = Path(td)
        write_glossary(project_dir, entries)
        # batch_size 2 -> batches [灵一,灵二] [灵三,灵四] [灵五]; 2nd call raises
        rows_by_call = [
            {"findings": [{"source": "灵一", "kind": "mistranslation",
                           "severity": "warn", "reason": "b1",
                           "suggestion": "fix one"}]},
            RuntimeError("mock batch failure"),
            {"findings": [{"source": "灵五", "kind": "mistranslation",
                           "severity": "warn", "reason": "b3",
                           "suggestion": "fix five"}]},
        ]
        result, prompts = run_with_fake_chat(project_dir, rows_by_call, batch_size=2)
        check("8a batches: 5 entries / batch_size 2 -> 3 batches, 3 calls",
              result["batches"] == 3 and len(prompts) == 3,
              f"batches={result['batches']} calls={len(prompts)}")
        errs = result["batch_errors"]
        check("8b batches: exactly one recorded batch error",
              len(errs) == 1 and "batch 2/3" in errs[0], f"errors={errs}")
        sources = {f["source"] for f in result["findings"]}
        check("8c batches: batches 1+3 findings kept, batch 2 gone",
              sources == {"灵一", "灵五"}, f"sources={sources}")
        check("8d batches: surviving findings are model-origin warns",
              all(f["origin"] == "model" and f["severity"] == "warn"
                  for f in result["findings"]))


def case_9_apply_fixes_guards() -> None:
    """apply_fixes guard matrix: eligibility, conflicts, validation, preservation."""
    with tempfile.TemporaryDirectory() as td:
        project_dir = Path(td)
        write_glossary(project_dir, [
            {"source": "灵根", "variants": [], "translation": "灵根",
             "alt_translations": [], "definition": "Spirit root.",
             "category": "skill", "origin": "model", "first_seen_chapter": 1},
            {"source": "天雷宗", "variants": ["雷宗"], "translation": "river town",
             "alt_translations": [], "definition": "A sect of heavenly thunder.",
             "category": "org", "origin": "model", "first_seen_chapter": 3},
        ])

        def entry(g: dict, source: str) -> dict:
            return next(t for t in g["terms"] if t["source"] == source)

        # 9a: eligible model warn mistranslation -> applied, fields preserved
        fixes = review.apply_fixes(project_dir, [
            {"source": "天雷宗", "kind": "mistranslation", "severity": "warn",
             "reason": "wrong rendering", "suggestion": "Heavenly Thunder Sect",
             "origin": "model"},
        ])
        ap, sk = fixes["applied"], fixes["skipped"]
        check("9a apply: one applied, nothing skipped",
              len(ap) == 1 and not sk, f"applied={ap} skipped={sk}")
        if ap:
            check("9b apply: applied record fields",
                  ap[0] == {"source": "天雷宗", "field": "translation",
                            "kind": "mistranslation", "old": "river town",
                            "new": "Heavenly Thunder Sect"}, f"record={ap[0]}")
        g = glossary.load(project_dir)
        e = entry(g, "天雷宗")
        check("9c apply: on-disk translation updated, every other field kept",
              e["translation"] == "Heavenly Thunder Sect"
              and e["variants"] == ["雷宗"] and e["origin"] == "model"
              and e["first_seen_chapter"] == 3 and e["category"] == "org"
              and e["definition"] == "A sect of heavenly thunder.", f"entry={e}")
        check("9d apply: untouched entry stays untouched",
              entry(g, "灵根")["translation"] == "灵根")

        # 9e-9j: the guard batch -- nothing may apply
        guard_findings = [
            # heuristic findings only qualify when the merge flagged their
            # borrowed suggestion "fixable"; a bare heuristic never does
            {"source": "天雷宗", "kind": "mistranslation", "severity": "warn",
             "reason": "heuristic without the merge flag", "suggestion": "Never Applied",
             "origin": "heuristic"},
            {"source": "天雷宗", "kind": "mistranslation", "severity": "info",
             "reason": "info is report-only", "suggestion": "Also Never",
             "origin": "model"},
            {"source": "天雷宗", "kind": "category", "severity": "warn",
             "reason": "not a category", "suggestion": "nonsense",
             "origin": "model"},
            {"source": "天雷宗", "kind": "mistranslation", "severity": "warn",
             "reason": "suggestion left in source script", "suggestion": "雷宗",
             "origin": "model"},
            {"source": "灵根", "kind": "mistranslation", "severity": "warn",
             "reason": "conflict A", "suggestion": "Suggestion A",
             "origin": "model"},
            {"source": "灵根", "kind": "mistranslation", "severity": "warn",
             "reason": "conflict B", "suggestion": "Suggestion B",
             "origin": "model"},
            {"source": "灵根", "kind": "duplicate", "severity": "warn",
             "reason": "dups never auto-fix", "suggestion": "whatever",
             "origin": "model"},
            {"source": "灵根", "kind": "collision", "severity": "warn",
             "reason": "collisions never auto-fix", "suggestion": "whatever",
             "origin": "model"},
        ]
        fixes = review.apply_fixes(project_dir, guard_findings)
        ap, sk = fixes["applied"], fixes["skipped"]
        check("9e guards: nothing applied", not ap, f"applied={ap}")
        got = sorted((s["source"], s["field"], s["reason"]) for s in sk)
        expect = sorted([
            ("天雷宗", "category", "invalid category"),
            ("天雷宗", "translation", "suggestion not in target language"),
            ("灵根", "translation", "conflicting suggestions"),
            ("灵根", "translation", "conflicting suggestions"),
        ])
        check("9f guards: exact skip set (invalid category / CJK suggestion / "
              "2x conflict; heuristic+info+report-only kinds never even listed)",
              got == expect, f"skipped={got}")
        g = glossary.load(project_dir)
        check("9g guards: glossary on disk unchanged by the guard batch",
              entry(g, "天雷宗")["translation"] == "Heavenly Thunder Sect"
              and entry(g, "天雷宗")["category"] == "org"
              and entry(g, "灵根")["translation"] == "灵根"
              and entry(g, "灵根")["category"] == "skill")

        # 9g2: a merge-borrowed suggestion (heuristic origin, fixable) applies
        fixes = review.apply_fixes(project_dir, [
            {"source": "天雷宗", "kind": "wrong_language", "severity": "warn",
             "reason": "borrowed fix", "suggestion": "Heavenly Thunder Sect (merged)",
             "origin": "heuristic", "fixable": True},
        ])
        ap, sk = fixes["applied"], fixes["skipped"]
        check("9g2 apply: merge-borrowed (fixable) suggestion applied",
              len(ap) == 1 and not sk
              and ap[0]["field"] == "translation"
              and ap[0]["old"] == "Heavenly Thunder Sect"
              and ap[0]["new"] == "Heavenly Thunder Sect (merged)",
              f"applied={ap} skipped={sk}")

        # 9h: a valid category fix on the other entry -> applied
        fixes = review.apply_fixes(project_dir, [
            {"source": "灵根", "kind": "category", "severity": "warn",
             "reason": "miscategorized", "suggestion": "org", "origin": "model"},
        ])
        ap, sk = fixes["applied"], fixes["skipped"]
        check("9h apply: valid category fix applied",
              len(ap) == 1 and not sk
              and ap[0] == {"source": "灵根", "field": "category",
                            "kind": "category", "old": "skill", "new": "org"},
              f"applied={ap} skipped={sk}")
        g = glossary.load(project_dir)
        e = entry(g, "灵根")
        check("9i apply: category updated in place, other fields kept",
              e["category"] == "org" and e["translation"] == "灵根"
              and e["origin"] == "model" and e["first_seen_chapter"] == 1,
              f"entry={e}")


def case_10_report() -> None:
    """write_report: indexed findings, fix-resolution exclusion, sections."""
    with tempfile.TemporaryDirectory() as td:
        project_dir = Path(td)
        terms = [
            {"source": "灵根", "variants": [], "translation": "spirit root",
             "alt_translations": [], "definition": "Innate aptitude.",
             "category": "other", "origin": "seeded", "first_seen_chapter": None},
            {"source": "裴家村", "variants": ["Pei Family Village"],
             "translation": "Pei Family Village", "alt_translations": [],
             "definition": "A village.", "category": "place",
             "origin": "model", "first_seen_chapter": 2},
            {"source": "天雷宗", "variants": [], "translation": "river town",
             "alt_translations": [], "definition": "A sect.", "category": "org",
             "origin": "model", "first_seen_chapter": 1},
        ]
        write_glossary(project_dir, terms)
        cfg = {"source_lang": "zh", "target_lang": "en"}
        findings = [
            {"source": "天雷宗", "kind": "mistranslation", "severity": "warn",
             "reason": "bad rendering of the sect name",
             "suggestion": "Heavenly Thunder Sect",
             "action": "Rename the sect's translation to the suggested English name.",
             "origin": "model"},
            {"source": "裴家村", "kind": "variant", "severity": "info",
             "reason": "variant 'Pei Family Village' contains no CJK characters",
             "suggestion": "", "origin": "heuristic"},
        ]

        # report-only run: everything indexed, warns before info, continuous [1]..[2]
        path = review.write_report(
            project_dir, findings=findings, terms=terms, applied=[], skipped=[],
            ran_fix=False, batches=1, batch_errors=[], cfg=cfg,
        )
        check("10a report: written as review-report.md",
              path.name == review.REPORT_NAME and path.is_file(), f"path={path}")
        text = path.read_text(encoding="utf-8")
        check("10b report: warn indexed [1], info [2] (continuous)",
              "### [1] warn / mistranslation / 天雷宗" in text
              and "### [2] info / variant / 裴家村" in text, "headings missing")
        check("10c report: model action preferred, template fallback",
              "- Action: Rename the sect's translation to the suggested English name." in text
              and "- Action: Set the `translation` field of this entry" not in text
              and "Remove the flagged string from `variants`" in text,
              "action lines wrong")
        check("10d report: full entry JSON embedded",
              '"first_seen_chapter": 2' in text and '"Pei Family Village"' in text,
              "entry JSON missing")
        check("10e report: next-steps footer",
              "retry --chapters" in text and "retired" in text, "footer missing")

        # --fix run where the translation fix resolved the warn: excluded from
        # numbering, listed under Fixed automatically instead
        applied = [{"source": "天雷宗", "field": "translation",
                    "kind": "mistranslation", "old": "river town",
                    "new": "Heavenly Thunder Sect"}]
        skipped = [{"source": "灵根", "field": "definition",
                    "reason": "conflicting suggestions"}]
        path = review.write_report(
            project_dir, findings=findings, terms=terms, applied=applied,
            skipped=skipped, ran_fix=True, batches=1, batch_errors=[], cfg=cfg,
        )
        text = path.read_text(encoding="utf-8")
        check("10f report: resolved finding renumbered out (info becomes [1])",
              "### [1] info / variant / 裴家村" in text
              and "### [2]" not in text
              and "bad rendering of the sect name" not in text,
              "numbering wrong")
        check("10g report: fixed-automatically section with old -> new",
              "Fixed automatically" in text
              and "`天雷宗`: translation 'river town' -> 'Heavenly Thunder Sect'" in text,
              "applied section wrong")
        check("10h report: skipped section with reason",
              "conflicting suggestions" in text, "skipped section wrong")

        # clean run
        path = review.write_report(
            project_dir, findings=[], terms=terms, applied=[], skipped=[],
            ran_fix=False, batches=1, batch_errors=[], cfg=cfg,
        )
        text = path.read_text(encoding="utf-8")
        check("10i report: clean report, nothing indexed",
              "No outstanding findings" in text and "[1]" not in text,
              "clean report wrong")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_duplicate()
    case_2_collision()
    case_3_wrong_language()
    case_4_category()
    case_5_variant()
    case_6_model_normalization()
    case_7_merge_borrows_suggestion()
    case_8_batch_resilience()
    case_9_apply_fixes_guards()
    case_10_report()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
