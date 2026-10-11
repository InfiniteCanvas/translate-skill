# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import contextlib
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import balance, config, consensus, glossary, pipeline, project, verify
import translate

PASSED = 0
FAILED: list[str] = []

CFG = {
    "source_lang": "zh",
    "target_lang": "en",
    "contextual_glossary_cap": 200,
    "min_term_coverage": 0.25,
    "fuzzy_max_distance": 2,
    "providers": {
        "translator": {"base_url": "http://fake/v1", "model": "m"},
        "consensus": {"base_url": "http://fake/v1", "model": "arb"},
    },
}

TERMS = {
    "terms": [
        {"source": "天道", "translation": "Heavenly Dao",
         "definition": "the way of heaven", "category": "technique"},
        {"source": "灵气", "translation": "spirit qi",
         "definition": "ambient vital energy", "category": "technique"},
        {"source": "长老", "translation": "Elder",
         "definition": "an elder of the sect", "category": "person"},
    ]
}

DRIFT_SOURCE = (
    "天道 governs the morning rites.\n"
    "He studied the 天道 for seven years.\n"
    "Again the 天道 was named.\n"
    "The 天道 cannot be bargained with.\n"
    "Once more the 天道 spoke.\n"
    "They wrote of the 天道.\n"
    "Even the 天道 has exceptions.\n"
    "Finally, the 天道 answered.\n"
)

DRIFT_TRANSLATION = (
    "Heavenly Dao governs the morning rites.\n"
    "He studied the doctrine for seven years.\n"
    "The Way was named again.\n"
    "It cannot be bargained with.\n"
    "It spoke once more.\n"
    "They wrote of it.\n"
    "Even this has exceptions.\n"
    "Finally, it answered.\n"
)

CLEAN_SOURCE = (
    "灵气 filled the hall.\n"
    "The 灵气 was thick that night.\n"
    "He drew 灵气 into his body.\n"
    "The 灵气 answered him.\n"
)

CLEAN_TRANSLATION = (
    "spirit qi filled the hall.\n"
    "The spirit qi was thick that night.\n"
    "He drew spirit qi into his body.\n"
    "The spirit qi answered him.\n"
)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_project(root: Path, chapters: list[tuple[str, str, str]], *,
                 translated: list[bool] | None = None) -> Path:
    """A sandbox project. `chapters` is (file, source body, translated body);
    `translated[i] is False` writes the source only, so that chapter has
    nothing to verify."""
    proj = root / "proj"
    write(proj / "config.json", json.dumps(CFG, indent=2) + "\n")
    write(proj / "glossary.json",
          json.dumps(TERMS, ensure_ascii=False, indent=2) + "\n")
    flags = translated or [True] * len(chapters)
    manifest = []
    for (file, source_body, translated_body), flag in zip(chapters, flags):
        number = int(file.removeprefix("CHAPTER_").removesuffix(".md"))
        write(proj / "source" / file, f"---\nnumber: {number}\n---\n{source_body}\n")
        if flag:
            write(proj / "translated" / file, translated_body + "\n")
        manifest.append({"file": file, "number": number, "status": "translated"})
    write(proj / "chapters.json", json.dumps(manifest, indent=2) + "\n")
    return proj


class Stub:
    """Swaps consensus.chat, so no provider is contacted and every call is
    recorded as (job, chapter)."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, str | None]] = []

    def __enter__(self):
        self.orig = consensus.chat
        consensus.chat = self.chat
        return self

    def __exit__(self, *exc):
        consensus.chat = self.orig
        return False

    def chat(self, project_dir, cfg, job, prompt, json_schema=None,
             max_tokens=None, chapter=None):
        self.calls.append((job, chapter))
        return self.responder(prompt, chapter)


def run_verify(proj: Path, args: list[str], responder) -> tuple[int, str, Stub]:
    ns = translate._build_parser().parse_args(
        ["verify", "--project", str(proj)] + args)
    stub = Stub(responder)
    out, err = io.StringIO(), io.StringIO()
    with stub, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = translate.cmd_verify(ns, proj)
    return code, out.getvalue() + err.getvalue(), stub


def records(proj: Path, relative: str = "logs/verify.jsonl") -> list[dict] | None:
    path = proj / relative
    if not path.is_file():
        return None
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def statuses(proj: Path) -> list[str]:
    return [e["status"] for e in project.load_manifest(proj)]


def confirms(source: str, reason: str = "a different sense in this chapter"):
    return lambda prompt, chapter: json.dumps({"verdicts": [
        {"source": source, "mistranslates": True, "reason": reason}]})


def clears(source: str):
    return lambda prompt, chapter: json.dumps({"verdicts": [
        {"source": source, "mistranslates": False,
         "reason": "the chapter uses the term in the pinned sense"}]})


def case_stage1_is_free_and_gates_stage2() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
            ("CHAPTER_0002.md", CLEAN_SOURCE, CLEAN_TRANSLATION),
        ])
        code, out, stub = run_verify(proj, [], clears("天道"))
        rows = records(proj)
        check("a1 stage 1 flags the under-used term: one call, on that chapter",
              [c for _j, c in stub.calls] == ["CHAPTER_0001"],
              f"calls={stub.calls!r}")
        check("a2 the clean chapter costs nothing -- no model call at all",
              len(stub.calls) == 1, f"calls={stub.calls!r}")
        check("a3 the call went out on the verify job",
              [j for j, _c in stub.calls] == ["verify"], f"calls={stub.calls!r}")
        check("a4 one JSONL line per chapter EXAMINED, clean chapters included",
              rows is not None and [r["chapter"] for r in rows]
              == ["CHAPTER_0001.md", "CHAPTER_0002.md"], f"rows={rows!r}")
        check("a5 the clean chapter's line carries empty findings and no rows",
              rows[1]["terms"] == [] and rows[1]["findings"] == []
              and rows[1]["error"] is None, f"row={rows[1]!r}")
        check("a6 the flagged chapter keeps its stage-1 row and its verdict",
              rows[0]["terms"][0]["source"] == "天道"
              and rows[0]["findings"][0]["mistranslates"] is False,
              f"row={rows[0]!r}")
        check("a7 nothing confirmed means exit 0", code == 0,
              f"code={code} out={out!r}")
        check("a8 and no chapter was flipped", statuses(proj)
              == ["translated", "translated"], f"{statuses(proj)!r}")


def case_status_flip_only_on_confirmation() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        before = (proj / "translated" / "CHAPTER_0001.md").read_bytes()
        code, out, stub = run_verify(proj, [], confirms("天道"))
        check("b1 a CONFIRMED chapter flips to needs-review and exits 1",
              code == 1 and statuses(proj) == ["needs-review"],
              f"code={code} statuses={statuses(proj)!r}")
        check("b2 the console names the chapter and the confirmed term",
              "CHAPTER_0001.md -> needs-review" in out and "天道" in out,
              f"out={out!r}")
        check("b3 translated text is byte-identical afterwards",
              (proj / "translated" / "CHAPTER_0001.md").read_bytes() == before,
              "verify rewrote a translated chapter")


def case_stage1_hit_alone_never_flips() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        code, out, stub = run_verify(proj, [], clears("天道"))
        check("c1 a stage-1 hit the model did NOT confirm leaves the chapter "
              "translated -- a counting heuristic must never schedule a "
              "re-translation",
              statuses(proj) == ["translated"] and code == 0,
              f"statuses={statuses(proj)!r} code={code}")
        record = records(proj)[0]
        check("c2 the JSONL still shows the flagged row: the scan happened, "
              "the confirmation just came back negative",
              len(record["terms"]) == 1
              and record["findings"][0]["mistranslates"] is False,
              f"row={record!r}")


def case_out_redirects_the_report() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0002.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        code, out, stub = run_verify(proj, ["--out", "reports/drift.jsonl"],
                                     confirms("天道"))
        check("d1 --out redirects the JSONL away from logs/verify.jsonl",
              records(proj, "reports/drift.jsonl") is not None
              and records(proj) is None, "")
        finding = records(proj, "reports/drift.jsonl")[0]["findings"][0]
        check("d2 the record carries the counts stage 1 flagged on, so the "
              "verdict can be read without re-running the scan",
              finding["source"] == "天道" and finding["source_count"] == 8
              and finding["rendered_count"] == 1 and finding["floor"] == 2,
              f"finding={finding!r}")


def case_verdicts_outside_the_chapter_are_dropped() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        code, out, stub = run_verify(
            proj, [], confirms("长老", "this term is not in this chapter"))
        check("e1 a verdict naming a term that was not offered cannot confirm "
              "the chapter",
              code == 0 and statuses(proj) == ["translated"],
              f"code={code} statuses={statuses(proj)!r}")
        check("e2 the chapter is still recorded, with the row it was flagged on "
              "and no findings",
              records(proj)[0]["terms"] and records(proj)[0]["findings"] == [],
              f"row={records(proj)[0]!r}")


def case_model_failure_is_reported_not_swallowed() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])

        def boom(prompt, chapter):
            raise RuntimeError("provider exploded")

        code, out, stub = run_verify(proj, [], boom)
        check("f1 an uncheckable chapter exits non-zero", code == 1,
              f"code={code} out={out!r}")
        check("f2 and says why on the console", "provider exploded" in out,
              f"out={out!r}")
        record = records(proj)[0]
        check("f3 the JSONL keeps the record and carries the error",
              bool(record["error"]) and "provider exploded" in record["error"],
              f"row={record!r}")
        check("f4 no confirmation means no status flip",
              statuses(proj) == ["translated"], f"{statuses(proj)!r}")


def case_chapter_without_translation_is_not_examined() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", CLEAN_SOURCE, CLEAN_TRANSLATION),
            ("CHAPTER_0002.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ], translated=[True, False])
        code, out, stub = run_verify(proj, [], clears("天道"))
        check("g1 a source chapter with no translation contributes no record",
              [r["chapter"] for r in records(proj)] == ["CHAPTER_0001.md"],
              f"rows={records(proj)!r}")
        check("g2 and the scan carries on to the rest of the book",
              code == 0 and len(records(proj)) == 1, f"code={code}")


def case_stage1_matches_the_pipeline_advisory() -> None:
    g = {"terms": [
        {"source": "天道", "translation": "Heavenly Dao",
         "category": "technique"},
        {"source": "灵气", "translation": "spirit qi", "category": "technique"},
    ]}
    pairs = [(g["terms"][0], 8), (g["terms"][1], 4)]
    lines = ["Heavenly Dao. He studied the doctrine. The Way. It spoke."]
    _drift, warnings, _over = balance.check(pairs, lines, 0.25, 2)
    rows = balance.underused(pairs, lines, 0.25, 2)
    check("h1 underused() returns exactly the terms check() warned about, as "
          "data",
          [r["source"] for r in rows]
          == [w.split("'")[1] for w in warnings] == ["天道"],
          f"rows={rows!r} warnings={warnings!r}")
    check("h2 an entry rendered not at all stays DRIFT and is NOT also offered "
          "as under-use -- the FAITH reviewer already owns that signal",
          balance.underused([(g["terms"][1], 4)], ["nothing at all here"],
                            0.25, 2) == [], "")
    scanned = verify.scan_chapter(CFG, g, DRIFT_SOURCE.splitlines(),
                                  DRIFT_TRANSLATION.splitlines())
    check("h3 scan_chapter reads the project's own coverage threshold and "
          "contextual cap",
          len(scanned) == 1 and scanned[0]["source"] == "天道"
          and scanned[0]["floor"] == 2 and scanned[0]["tgt_count"] == 1,
          f"scanned={scanned!r}")
    excerpt = verify._source_excerpt(DRIFT_SOURCE.splitlines(),
                                    {"source": "天道", "variants": []})
    check("h4 the source excerpt handed to the model really contains the term, "
          "capped at EXCERPT_LINES",
          len(excerpt) == verify.EXCERPT_LINES
          and all("天道" in line for line in excerpt), f"excerpt={excerpt!r}")


def case_job_and_template_coupling() -> None:
    check("i1 verify is a provider job with its own defaults",
          "verify" in config.PROVIDER_JOBS
          and "verify" in config.PROVIDER_DEFAULTS, "")
    check("i2 consensus is still last, so a fan-out job never arbitrates itself",
          config.PROVIDER_JOBS[-1] == "consensus", "")
    check("i3 verify is therefore a fan-out job and needs its own arbitration "
          "template",
          "verify" in config.CONSENSUS_JOBS, "")
    shipped = pipeline._SKILL_TEMPLATES
    name = "consensus_verify.md"
    text = (shipped / name).read_text(encoding="utf-8")
    digest = hashlib.sha256((shipped / name).read_bytes()).hexdigest()
    check("i4 consensus_verify.md ships AND its current sha256 is recorded in "
          ".history.json, which is what the template-assets suite asserts",
          digest in pipeline._shipped_revisions(name), f"digest={digest}")
    check("i5 it carries both placeholders consensus.chat fills",
          "{{task_prompt}}" in text and "{{candidates_section}}" in text, "")
    with tempfile.TemporaryDirectory() as td:
        _text, resolved_name = consensus._resolve_template(Path(td), "verify")
        check("i6 a verify fan-out resolves consensus_verify.md, not the "
              "general arbitration wording",
              resolved_name == name, f"got={resolved_name}")
        for job in config.CONSENSUS_JOBS:
            _t, resolved = consensus._resolve_template(Path(td), job)
            check(f"i7 every fan-out job resolves its own template ({job})",
                  resolved == f"consensus_{job}.md", f"got={resolved}")
    reviewer = (shipped / "consensus_reviewer.md").read_text(encoding="utf-8")
    check("i8 the wording reuse would have contradicted is really there: the "
          "reviewer arbitrator declares a pinned rendering never an error",
          "not translation errors" in reviewer, "")
    check("i9 and consensus_verify.md does not inherit that declaration",
          "not translation errors" not in text, "")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    case_stage1_is_free_and_gates_stage2()
    case_status_flip_only_on_confirmation()
    case_stage1_hit_alone_never_flips()
    case_out_redirects_the_report()
    case_verdicts_outside_the_chapter_are_dropped()
    case_model_failure_is_reported_not_swallowed()
    case_chapter_without_translation_is_not_examined()
    case_stage1_matches_the_pipeline_advisory()
    case_job_and_template_coupling()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())