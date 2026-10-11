# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import balance, client, config, consensus, glossary, pipeline, project, verify
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

QI_SOURCE = (
    "灵气 filled the hall.\n"
    "He drew 灵气 into his body.\n"
    "Cold 灵气 crept along the floor.\n"
    "The 灵气 vanished before dawn.\n"
)

QI_TRANSLATION_A = (
    "spirit qi filled the hall.\n"
    "He drew it into his body.\n"
    "Cold stuff crept along the floor.\n"
    "It vanished before dawn.\n"
)

QI_TRANSLATION_B = (
    "spirit qi filled the hall.\n"
    "He drew it into his body.\n"
    "Cold stuff crept along the floor.\n"
    "It vanished before dawn.\n"
)

STRICT_CFG = dict(CFG, min_term_coverage=0.5)

TEN_HIT_SOURCE = "".join(
    f"The 天道 spoke again, line {n}.\n" for n in range(1, 11))

TEN_HIT_TRANSLATION = ("Heavenly Dao spoke again.\n" + "".join(
    f"It spoke again, line {n}.\n" for n in range(1, 10)))


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
                 translated: list[bool] | None = None,
                 cfg: dict | None = None) -> Path:
    """A sandbox project. `chapters` is (file, source body, translated body);
    `translated[i] is False` writes the source only, so that chapter has
    nothing to verify."""
    proj = root / "proj"
    write(proj / "config.json", json.dumps(cfg or CFG, indent=2) + "\n")
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
    recorded as (job, chapter). This is the RIGHT level for testing what the
    module does with a RESPONSE -- see PromptCapture for what it sends."""

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


class PromptCapture:
    """Swaps client.chat -- the boundary the prompt actually crosses to reach a
    provider -- and records the exact prompt text.

    Deliberately NOT consensus.chat: the whole point of these cases is that the
    real consensus.chat, the real shipped template and the real fill() all run,
    so what lands in `prompts` is what a model would have been told. A suite that
    stubs consensus.chat and asserts on the RESPONSE path cannot tell a template
    carrying its data from one carrying none at all, which is exactly how a
    zero-placeholder rubric shipped green.

    The stub sits below the seam, so it decides nothing about whether verify sent
    its terms; it only replaces the network call."""

    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def __enter__(self):
        self.orig = client.chat
        client.chat = self.chat
        return self

    def __exit__(self, *exc):
        client.chat = self.orig
        return False

    def chat(self, block, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.response


def run_verify(proj: Path, args: list[str], responder) -> tuple[int, str, Stub]:
    ns = translate._build_parser().parse_args(
        ["verify", "--project", str(proj)] + args)
    stub = Stub(responder)
    out, err = io.StringIO(), io.StringIO()
    with stub, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = translate.cmd_verify(ns, proj)
    return code, out.getvalue() + err.getvalue(), stub


def capture_prompts(proj: Path, response: str) -> tuple[int, str, PromptCapture]:
    ns = translate._build_parser().parse_args(
        ["verify", "--project", str(proj)])
    cap = PromptCapture(response)
    out, err = io.StringIO(), io.StringIO()
    with cap, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = translate.cmd_verify(ns, proj)
    return code, out.getvalue() + err.getvalue(), cap


def records(proj: Path, relative: str = "logs/verify.jsonl") -> list[dict] | None:
    path = proj / relative
    if not path.is_file():
        return None
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def chapter_records(proj: Path, relative: str = "logs/verify.jsonl") -> list[dict]:
    return [r for r in (records(proj, relative) or []) if r.get("kind") == "chapter"]


def contamination_records(proj: Path, relative: str = "logs/verify.jsonl") -> list[dict]:
    return [r for r in (records(proj, relative) or [])
            if r.get("kind") == "contamination"]


def statuses(proj: Path) -> list[str]:
    return [e["status"] for e in project.load_manifest(proj)]


def confirms(source: str, chapter: str = "CHAPTER_0001.md",
             reason: str = "a different sense in this chapter"):
    return lambda prompt, ch: json.dumps({"verdicts": [
        {"source": source, "chapter": chapter,
         "mistranslates": True, "reason": reason}]})


def clears(source: str, chapter: str = "CHAPTER_0001.md"):
    return lambda prompt, ch: json.dumps({"verdicts": [
        {"source": source, "chapter": chapter, "mistranslates": False,
         "reason": "the chapter uses the term in the pinned sense"}]})


def case_the_prompt_actually_carries_its_data() -> None:
    """The regression guard for the shipped template: assert what the MODEL is
    told, on the real path, with the stub below the seam.

    Filling the shipped template is not enough on its own -- a template that
    declares no {{...}} placeholder fills to itself, unchanged, and every
    response-shaped assertion downstream still passes. So these cases run the
    whole command and read the prompt that crossed to client.chat."""
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        response = json.dumps({"verdicts": [
            {"source": "天道", "chapter": "CHAPTER_0001.md",
             "mistranslates": False, "reason": "pinned sense throughout"}]})
        code, out, cap = capture_prompts(proj, response)
        check("p1 the command reached a model call at all",
              len(cap.prompts) == 1, f"prompts={len(cap.prompts)}")
        prompt = cap.prompts[0]
        check("p2 THE TERM REACHES THE MODEL: the glossary term is in the "
              "prompt text",
              "天道" in prompt, f"len={len(prompt)}")
        check("p3 and so is its pinned rendering and definition, not just its "
              "name",
              "Heavenly Dao" in prompt and "the way of heaven" in prompt, "")
        check("p4 THE CHAPTER REACHES THE MODEL: the chapter file name is in "
              "the prompt text",
              "CHAPTER_0001.md" in prompt, "")
        check("p5 and the language names are too (the repo's convention fills "
              "source_lang/target_lang through _lang_name)",
              "Chinese" in prompt and "English" in prompt, "")
        check("p6 the evidence is PAIRED: the same line numbers appear against "
              "both the source and the translation",
              bool(re.search(r"1: .*[\u4e00-\u9fff]", prompt))
              and "  source:" in prompt and "  translation:" in prompt, "")
        check("p7 NO UNFILLED PLACEHOLDER SURVIVES into the filled prompt",
              not re.findall(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}", prompt),
              f"leftovers={re.findall(r'{{.*?}}', prompt)!r}")


def case_every_shipped_verify_template_is_filled() -> None:
    """Both shipped verify templates must carry the keys their fillers supply.

    pipeline.fill RAISES on a template key the mapping lacks, so a typo cannot
    ship silently -- but a template that uses NO key at all is the failure mode
    that raises nothing and fills to itself. Asserting the mapping's keys appear
    in the shipped text is what closes it, for both the task template and the
    arbitrator that wraps it."""
    shipped = pipeline._SKILL_TEMPLATES
    task = (shipped / "glossary_verify.md").read_text(encoding="utf-8")
    arb = (shipped / "consensus_verify.md").read_text(encoding="utf-8")
    for key in ("source_lang", "target_lang", "term", "contexts",
                "context_count"):
        check("q1 glossary_verify.md carries " + "{{" + key + "}}",
              "{{" + key + "}}" in task, "")
    check("q2 consensus_verify.md still carries both arbitration keys and "
          "nothing else (a third key would raise at fill time)",
          "{{task_prompt}}" in arb and "{{candidates_section}}" in arb
          and not [k for k in re.findall(r"\{\{\s*([A-Za-z_]+)\s*\}\}", arb)
                   if k not in ("task_prompt", "candidates_section")], "")
    mapping = {"source_lang": "Chinese", "target_lang": "English",
               "term": "{}", "contexts": "{}", "context_count": "1"}
    filled = pipeline.fill(task, mapping, "glossary_verify.md")
    check("q3 the shipped task template fills to something DIFFERENT from "
          "itself -- the check a zero-placeholder template cannot pass",
          filled != task and not re.findall(
              r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}", filled), "")


def case_stage1_is_free_and_gates_stage2() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
            ("CHAPTER_0002.md", CLEAN_SOURCE, CLEAN_TRANSLATION),
        ])
        code, out, stub = run_verify(proj, [], clears("天道"))
        rows = chapter_records(proj)
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
              and rows[0]["findings"][0]["mistranslates"] is False
              and rows[0]["findings"][0]["chapter"] == "CHAPTER_0001.md",
              f"row={rows[0]!r}")
        check("a7 nothing confirmed means exit 0", code == 0,
              f"code={code} out={out!r}")
        check("a8 and no chapter was flipped", statuses(proj)
              == ["translated", "translated"], f"{statuses(proj)!r}")


def case_stage2_batches_by_term_across_chapters() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", QI_SOURCE, QI_TRANSLATION_A),
            ("CHAPTER_0002.md", QI_SOURCE, QI_TRANSLATION_B),
        ], cfg=STRICT_CFG)
        code, out, cap = capture_prompts(proj, json.dumps({"verdicts": [
            {"source": "灵气", "chapter": "CHAPTER_0001.md",
             "mistranslates": False, "reason": "pinned sense"},
            {"source": "灵气", "chapter": "CHAPTER_0002.md",
             "mistranslates": True, "reason": "a different referent"}]}))
        check("n1 the SAME term flagged in two chapters is judged in ONE call, "
              "so the two chapters' usages can be laid against each other -- "
              "the batch is the term, not the chapter",
              len(cap.prompts) == 1, f"prompts={len(cap.prompts)}")
        check("n2 and that one prompt really does carry BOTH chapters' "
              "contexts",
              "CHAPTER_0001.md" in cap.prompts[0]
              and "CHAPTER_0002.md" in cap.prompts[0], "")
        check("n3 the chapter with the real sense collision is the one flipped",
              statuses(proj) == ["translated", "needs-review"]
              and code == 1, f"statuses={statuses(proj)!r} code={code}")
        check("n4 both chapters were scanned and recorded",
              [r["chapter"] for r in chapter_records(proj)]
              == ["CHAPTER_0001.md", "CHAPTER_0002.md"], "")
        check("n5 the clean chapter's finding is kept alongside the confirmed "
              "one, not dropped",
              chapter_records(proj)[0]["findings"][0]["mistranslates"] is False
              and chapter_records(proj)[1]["findings"][0]["mistranslates"] is True,
              "")


def case_contexts_are_split_into_follow_up_calls() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", TEN_HIT_SOURCE, TEN_HIT_TRANSLATION),
        ])
        hits = verify._hit_lines(TEN_HIT_SOURCE.splitlines(),
                                 {"source": "天道", "variants": []})
        batches = -(-len(hits) // verify.MAX_CONTEXTS_PER_CALL)
        code, out, cap = capture_prompts(proj, json.dumps({"verdicts": [
            {"source": "天道", "chapter": "CHAPTER_0001.md",
             "mistranslates": False, "reason": "pinned sense"}]}))
        check("o1 a term with more contexts than one call carries is SPLIT into "
              "follow-up calls rather than truncated to fit",
              len(hits) > verify.MAX_CONTEXTS_PER_CALL
              and len(cap.prompts) == batches,
              f"hits={len(hits)} cap={verify.MAX_CONTEXTS_PER_CALL} "
              f"prompts={len(cap.prompts)}")
        sent = sum(p.count("  source:") for p in cap.prompts)
        check("o2 NO CONTEXT IS DROPPED to make the cap fit: every hit line is "
              "offered to some call, and none is sent twice",
              sent == len(hits), f"sent={sent} hits={len(hits)}")
        check("o3 the split is visible -- the console reports the call count, "
              "so a term that cost extra is never silent",
              f"across {len(cap.prompts)} call(s)" in out, f"out={out!r}")
        check("o4 and the chapter is still judged ONCE, however many batches "
              "its contexts took -- a batch boundary is a payload limit, not a "
              "second opinion",
              len(chapter_records(proj)[0]["findings"]) == 1
              and chapter_records(proj)[0]["findings"][0]["chapter"]
              == "CHAPTER_0001.md", f"row={chapter_records(proj)[0]!r}")


def case_a_confirmed_term_also_flags_the_glossary_entry() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        code, out, stub = run_verify(proj, [], confirms("天道"))
        terms = contamination_records(proj)
        check("r1 a confirmed term is ALSO recorded as a per-term "
              "contamination record, distinct from the chapter records",
              len(terms) == 1 and terms[0]["source"] == "天道"
              and terms[0]["pinned"] == "Heavenly Dao"
              and terms[0]["chapters"] == ["CHAPTER_0001.md"],
              f"terms={terms!r}")
        check("r2 the console names the term AND the chapters it affects",
              "glossary contamination" in out and "天道" in out
              and "CHAPTER_0001.md" in out, f"out={out!r}")
        check("r3 glossary.json is NEVER written: the entry feeds the "
              "translator's pinned-terms list, so a flag stored there would "
              "change translation behaviour",
              json.loads((proj / "glossary.json").read_text(encoding="utf-8"))
              == TERMS, "verify mutated glossary.json")
        check("r4 a cleared term produces no contamination record",
              run_verify(proj, [], clears("天道"))[1].count(
                  "glossary contamination") == 0, "")


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
        record = chapter_records(proj)[0]
        check("c2 the JSONL still shows the flagged row: the scan happened, "
              "the confirmation just came back negative",
              len(record["terms"]) == 1
              and record["findings"][0]["mistranslates"] is False,
              f"row={record!r}")
        check("c3 and no glossary entry is flagged, because nothing was "
              "confirmed",
              contamination_records(proj) == [], "")


def case_out_redirects_the_report() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0002.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        code, out, stub = run_verify(proj, ["--out", "reports/drift.jsonl"],
                                     confirms("天道", "CHAPTER_0002.md"))
        check("d1 --out redirects the JSONL away from logs/verify.jsonl",
              records(proj, "reports/drift.jsonl") is not None
              and records(proj) is None, "")
        finding = chapter_records(proj, "reports/drift.jsonl")[0]["findings"][0]
        check("d2 the record carries the counts stage 1 flagged on, so the "
              "verdict can be read without re-running the scan",
              finding["source"] == "天道" and finding["source_count"] == 8
              and finding["rendered_count"] == 1 and finding["floor"] == 2,
              f"finding={finding!r}")


def case_verdicts_outside_the_batch_are_dropped() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ])
        code, out, stub = run_verify(
            proj, [], confirms("天道", "CHAPTER_9999.md"))
        check("e1 a verdict naming a chapter that was not offered cannot "
              "confirm it",
              code == 0 and statuses(proj) == ["translated"],
              f"code={code} statuses={statuses(proj)!r}")
        record = chapter_records(proj)[0]
        check("e2 the chapter is still recorded, with the row it was flagged on "
              "and no findings",
              record["terms"] and record["findings"] == [], f"row={record!r}")
        code2, _out2, _stub2 = run_verify(proj, [], confirms("长老"))
        check("e3 a verdict naming a term that was not offered cannot confirm "
              "anything either",
              code2 == 0 and statuses(proj) == ["translated"]
              and chapter_records(proj)[0]["findings"] == [],
              f"code={code2} statuses={statuses(proj)!r}")


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
        record = chapter_records(proj)[0]
        check("f3 the JSONL keeps the record and carries the error",
              bool(record["error"]) and "provider exploded" in record["error"],
              f"row={record!r}")
        check("f4 no confirmation means no status flip",
              statuses(proj) == ["translated"], f"{statuses(proj)!r}")
        check("f5 and no glossary entry is flagged off a failed call",
              contamination_records(proj) == [], "")


def case_unpairable_chapter_is_reported_not_guessed() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", DRIFT_SOURCE, "Only one line.\n"),
        ])
        code, out, stub = run_verify(proj, [], confirms("天道"))
        record = chapter_records(proj)[0]
        check("u1 a translation whose line count differs from its source is "
              "reported, because the paired indices stage 2 relies on cannot "
              "be trusted",
              bool(record["error"]) and "line" in record["error"],
              f"row={record!r}")
        check("u2 it is never guessed at: no model call, no finding, no flip",
              stub.calls == [] and record["findings"] == []
              and statuses(proj) == ["translated"], f"calls={stub.calls!r}")
        check("u3 and it is not silent -- it exits non-zero and says so",
              code == 1 and "cannot be trusted" in out, f"code={code}")


def case_chapter_without_translation_is_not_examined() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = make_project(Path(td), [
            ("CHAPTER_0001.md", CLEAN_SOURCE, CLEAN_TRANSLATION),
            ("CHAPTER_0002.md", DRIFT_SOURCE, DRIFT_TRANSLATION),
        ], translated=[True, False])
        code, out, stub = run_verify(proj, [], clears("天道"))
        check("g1 a source chapter with no translation contributes no record",
              [r["chapter"] for r in chapter_records(proj)] == ["CHAPTER_0001.md"],
              f"rows={chapter_records(proj)!r}")
        check("g2 and the scan carries on to the rest of the book",
              code == 0 and len(chapter_records(proj)) == 1, f"code={code}")


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
    hits = verify._hit_lines(DRIFT_SOURCE.splitlines(),
                             {"source": "天道", "variants": []})
    check("h4 every source line the term occurs on is located, so no usage is "
          "left unjudged", len(hits) == 8, f"hits={hits!r}")
    context = verify._context("CHAPTER_0001.md", DRIFT_SOURCE.splitlines(),
                              DRIFT_TRANSLATION.splitlines(), 4, scanned[0])
    check("h5 a context pairs the source and translation at THE SAME line "
          "numbers -- the line-indexed protocol the whole pipeline rests on",
          [line.split(":")[0] for line in context["source"]]
          == [line.split(":")[0] for line in context["translation"]]
          and context["lines"] == "3-7", f"context={context!r}")
    check("h6 the window is bounded to CONTEXT_WINDOW either side of the hit",
          len(context["source"]) == 2 * verify.CONTEXT_WINDOW + 1
          and "天道" in context["source"][verify.CONTEXT_WINDOW],
          f"context={context!r}")
    check("h7 the context carries the per-chapter counts the flag was raised "
          "on", context["source_count"] == 8 and context["rendered_count"] == 1
          and context["floor"] == 2, f"context={context!r}")


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
    check("i10 the arbitrator describes the task the code actually sends -- a "
          "term judged across contexts, with a per-chapter verdict",
          "CONTEXTS" in text and "chapter" in text, "")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    case_the_prompt_actually_carries_its_data()
    case_every_shipped_verify_template_is_filled()
    case_stage1_is_free_and_gates_stage2()
    case_stage2_batches_by_term_across_chapters()
    case_contexts_are_split_into_follow_up_calls()
    case_a_confirmed_term_also_flags_the_glossary_entry()
    case_status_flip_only_on_confirmation()
    case_stage1_hit_alone_never_flips()
    case_out_redirects_the_report()
    case_verdicts_outside_the_batch_are_dropped()
    case_model_failure_is_reported_not_swallowed()
    case_unpairable_chapter_is_reported_not_guessed()
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