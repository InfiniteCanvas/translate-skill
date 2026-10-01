"""Tests for lib/story.py: the rolling story-so-far recap.

Covers predecessor() (first chapter -> None, manifest ORDER decides rather
than list position, unknown file -> None); load_state leniency (missing ->
empty, malformed JSON / non-object document / non-object "chapters" ->
{"chapters": {}} plus exactly one '[warn] story_state.json unreadable
(<reason>) - recaps start fresh' line, BOM-prefixed file parses silently);
save/load round-trip (entries preserved, trailing newline, CJK readable in
the raw bytes); story_part() (empty -> "", non-empty -> the exact labeled
prefix line + text); ensure_recap (existing entry returned with ZERO LLM
calls, first chapter -> "" with zero calls, missing predecessor backfilled
with exactly ONE call whose {{previous_recap}} is the nearest EARLIER
existing entry, the no-chain rule -- a book whose only entry sits on a
LATER chapter backfills with the empty anchor, never the later recap and
never a recursive chain -- and the failure paths: predecessor's translated
file missing, LLM error -> "" + the warn line); record_recap (overwrites
the chapter's own entry unconditionally while neighbors stay intact,
prompt carries prev_recap_text / title / body, failure warns and leaves
the prior state byte-unchanged); and RECAP_SCHEMA shape sanity (flat,
single required "recap" string key, additionalProperties false).

Every case builds a sandbox project (translated/ chapters, chapters.json,
optional story_state.json) inside tempfile.TemporaryDirectory() -- repo
fixtures are never touched. The recap LLM is a stub returning a canned
{"recap": ...} JSON string (story runs client.extract_json over it), with
a sink counting calls so "exactly ONE call" is observable; the template is
the real shipped assets/templates/recap.md (the project-fallback path).
stdout around warn/print-ing calls is captured with contextlib.redirect_stdout.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    python tests/test_story_recap.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import project  # noqa: E402
from lib import story  # noqa: E402

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


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def capture(fn, *args, **kwargs):
    """fn(*args, **kwargs) with stdout captured; returns (result, output)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


# ------------------------------------------------------------------ fixtures

CFG = {"source_lang": "zh", "target_lang": "en"}

TRANSLATED_MD = (
    "---\n"
    "chapter_title: 第一章 灵根\n"
    "title: Spirit Root Awakening\n"
    "---\n"
    "\n"
    "Lin Feng awakened his spirit root.\n"
    "The elders gasped.\n"
)


def make_manifest(files: list[str]) -> list[dict]:
    """Manifest entries in the given (already ordered) file order."""
    return [
        {"file": f, "number": i + 1, "suffix": "", "order": i,
         "status": "translated", "title": "Title"}
        for i, f in enumerate(files)
    ]


def make_project(td: str, files: list[str], translated: list[str] | None = None) -> tuple[Path, list[dict]]:
    """Sandbox project: chapters.json over `files` (in order) plus the
    translated/ copies named in `translated` (all get the same fixture body)."""
    root = Path(td)
    manifest = make_manifest(files)
    write_lf(root / "chapters.json",
             json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    for name in translated or []:
        write_lf(root / "translated" / name, TRANSLATED_MD)
    return root, manifest


def fake_recap_chat(text: str = "A running recap.", sink: list | None = None):
    """Stub recap provider: returns the canned payload as a JSON string;
    optionally records prompts (both the call count and the filled
    {{previous_recap}} are observable through it)."""
    def chat(prompt: str) -> str:
        if sink is not None:
            sink.append(prompt)
        return json.dumps({"recap": text}, ensure_ascii=False)
    return chat


def broken_chat(prompt: str) -> str:
    raise RuntimeError("endpoint down")


def entry(recap: str) -> dict:
    return {"recap": recap, "updated_at": "2026-09-30T00:00:00+00:00"}


# ---------------------------------------------------------------------- cases


def case_1_predecessor() -> None:
    """Manifest ORDER (the `order` field) decides, not list position."""
    manifest = make_manifest(["Chapter_0001.md", "Chapter_0002.md", "Chapter_0003.md"])
    shuffled = [manifest[2], manifest[0], manifest[1]]  # deliberately out of order
    check("1a predecessor: first chapter -> None",
          story.predecessor(shuffled, "Chapter_0001.md") is None, "")
    check("1b predecessor: second chapter's predecessor is chapter 1",
          story.predecessor(shuffled, "Chapter_0002.md") == "Chapter_0001.md", "")
    check("1c predecessor: third chapter's predecessor is chapter 2",
          story.predecessor(shuffled, "Chapter_0003.md") == "Chapter_0002.md", "")
    check("1d predecessor: file absent from the manifest -> None",
          story.predecessor(shuffled, "Chapter_9999.md") is None, "")


def case_2_load_state() -> None:
    """Missing -> empty; malformed -> warn + empty; BOM parses."""
    with tempfile.TemporaryDirectory() as td:
        root, _manifest = make_project(td, ["Chapter_0001.md"])
        state, out = capture(story.load_state, root)
        check("2a load: missing file -> {'chapters': {}} silently",
              state == {"chapters": {}} and out == "", f"state={state} out={out!r}")

        write_lf(root / "story_state.json", "{not json")
        state, out = capture(story.load_state, root)
        check("2b load: malformed JSON -> empty",
              state == {"chapters": {}}, f"state={state}")
        check("2c load: exactly one unreadable warn (JSONDecodeError)",
              out == "[warn] story_state.json unreadable (JSONDecodeError)"
                     " - recaps start fresh\n", f"out={out!r}")

        write_lf(root / "story_state.json", "[]")
        state, out = capture(story.load_state, root)
        check("2d load: non-object document -> empty + warn names the type",
              state == {"chapters": {}}
              and out == "[warn] story_state.json unreadable (list)"
                         " - recaps start fresh\n", f"out={out!r}")

        write_lf(root / "story_state.json",
                 json.dumps({"chapters": []}, ensure_ascii=False))
        state, out = capture(story.load_state, root)
        check("2e load: non-object 'chapters' -> empty + warn",
              state == {"chapters": {}}
              and out == "[warn] story_state.json unreadable (invalid chapters)"
                         " - recaps start fresh\n", f"out={out!r}")

        # BOM-prefixed hand edit: utf-8-sig read keeps the entries, silent.
        seeded = {"chapters": {"Chapter_0001": entry("Plot so far.")}}
        (root / "story_state.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps(seeded, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        state, out = capture(story.load_state, root)
        check("2f load: BOM-prefixed file parses with its entries, silent",
              state == seeded and out == "", f"state={state} out={out!r}")


def case_3_roundtrip() -> None:
    """save_state -> load_state preserves entries; file shape."""
    with tempfile.TemporaryDirectory() as td:
        root, _manifest = make_project(td, ["Chapter_0001.md", "Chapter_0002.md"])
        state = {"chapters": {"Chapter_0001": entry("灵根 awakened."),
                              "Chapter_0002": entry("Second recap.")}}
        capture(story.save_state, root, state)
        path = root / "story_state.json"
        check("3a roundtrip: file written at the project root", path.is_file(), "")
        raw = path.read_bytes()
        check("3b roundtrip: trailing newline", raw.endswith(b"\n"), f"tail={raw[-12:]!r}")
        check("3c roundtrip: ensure_ascii=False keeps the CJK in raw bytes",
              "灵根".encode("utf-8") in raw, "")
        loaded, out = capture(story.load_state, root)
        check("3d roundtrip: entries preserved on reload",
              loaded == state and out == "", f"loaded={loaded}")


def case_4_story_part() -> None:
    """Pure formatting helper: empty -> "", else the exact prefix + text."""
    check("4a part: empty -> \"\"", story.story_part("") == "", "")
    text = "Lin Feng joined the sect."
    got = story.story_part(text)
    check("4b part: exact labeled prefix line + recap text",
          got == "Story so far (auto-generated recap of the preceding chapters):\n" + text,
          f"got={got!r}")


def case_5_ensure_recap_hit() -> None:
    """Existing predecessor entry returned with ZERO LLM calls; first chapter
    -> "" with zero calls."""
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, ["Chapter_0001.md", "Chapter_0002.md",
                                           "Chapter_0003.md"])
        write_lf(root / "story_state.json", json.dumps(
            {"chapters": {"Chapter_0002": entry("Stored recap.")}}, ensure_ascii=False) + "\n")
        recap, out = capture(story.ensure_recap, root, CFG, manifest,
                             "Chapter_0003.md", "[Chapter_0003]", chat=broken_chat)
        check("5a hit: stored predecessor entry returned verbatim",
              recap == "Stored recap.", f"recap={recap!r}")
        check("5b hit: no LLM call (broken stub would have raised), no console",
              out == "", f"out={out!r}")

        recap, out = capture(story.ensure_recap, root, CFG, manifest,
                             "Chapter_0001.md", "[Chapter_0001]", chat=broken_chat)
        check("5c hit: first chapter -> \"\" with no call, no console",
              recap == "" and out == "", f"recap={recap!r} out={out!r}")


def case_6_ensure_recap_backfill() -> None:
    """Missing predecessor entry: exactly ONE call anchored on the nearest
    EARLIER existing entry (chapter 2 of 1..5), state saved, console line."""
    with tempfile.TemporaryDirectory() as td:
        files = [f"Chapter_000{n}.md" for n in range(1, 6)]
        root, manifest = make_project(td, files, translated=["Chapter_0004.md"])
        write_lf(root / "story_state.json", json.dumps(
            {"chapters": {"Chapter_0002": entry("Earlier plot.")}}, ensure_ascii=False) + "\n")
        prompts: list[str] = []
        recap, out = capture(
            story.ensure_recap, root, CFG, manifest, "Chapter_0005.md",
            "[Chapter_0005]", chat=fake_recap_chat("Backfilled recap.", sink=prompts))
        check("6a backfill: generated recap returned",
              recap == "Backfilled recap.", f"recap={recap!r}")
        check("6b backfill: exactly ONE LLM call (no chain)",
              len(prompts) == 1, f"calls={len(prompts)}")
        check("6c backfill: prompt anchored on the nearest earlier entry (ch2)",
              "Earlier plot." in prompts[0], f"prompt={prompts[0][:200]!r}")
        check("6d backfill: prompt carries the predecessor's translated body",
              "Lin Feng awakened his spirit root." in prompts[0], "")
        state = story.load_state(root)
        check("6e backfill: predecessor's entry saved under its stem",
              state["chapters"].get("Chapter_0004", {}).get("recap") == "Backfilled recap."
              and "Chapter_0002" in state["chapters"],
              f"state={state}")
        check("6f backfill: console prints the init line naming the backfilled chapter",
              out == "[Chapter_0005] [init] recap (backfill Chapter_0004.md)\n",
              f"out={out!r}")


def case_7_ensure_recap_no_chain() -> None:
    """A book whose ONLY entry sits on a LATER chapter (40) backfills the
    predecessor (39) with the EMPTY anchor -- the later recap is never used
    and no recursive chain runs (still exactly one call)."""
    with tempfile.TemporaryDirectory() as td:
        files = [f"Chapter_00{n}.md" for n in (37, 38, 39, 40)]
        root, manifest = make_project(td, files, translated=["Chapter_0039.md"])
        write_lf(root / "story_state.json", json.dumps(
            {"chapters": {"Chapter_0040": entry("Future plot.")}}, ensure_ascii=False) + "\n")
        prompts: list[str] = []
        recap, out = capture(
            story.ensure_recap, root, CFG, manifest, "Chapter_0040.md",
            "[Chapter_0040]", chat=fake_recap_chat("Ch39 recap.", sink=prompts))
        check("7a no-chain: exactly ONE call (no 38/37 backfill cascade)",
              len(prompts) == 1, f"calls={len(prompts)}")
        check("7b no-chain: the LATER chapter's recap is not the anchor",
              "Future plot." not in prompts[0], f"prompt={prompts[0][:200]!r}")
        check("7c no-chain: recap still returned and recorded for ch39",
              recap == "Ch39 recap."
              and story.load_state(root)["chapters"]["Chapter_0039"]["recap"] == "Ch39 recap.",
              f"recap={recap!r}")
        check("7d no-chain: ch40's own entry untouched",
              story.load_state(root)["chapters"]["Chapter_0040"]["recap"] == "Future plot.",
              "")


def case_8_ensure_recap_failure() -> None:
    """Advisory failure: missing translated predecessor and LLM error both
    return "" with one warn line, never raise."""
    files = ["Chapter_0001.md", "Chapter_0002.md"]

    # predecessor listed as translated but its file is missing
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, files)  # no translated/ files
        recap, out = capture(story.ensure_recap, root, CFG, manifest,
                             "Chapter_0002.md", "[Chapter_0002]", chat=broken_chat)
        check("8a fail: missing predecessor file -> \"\"",
              recap == "", f"recap={recap!r}")
        check("8b fail: one warn naming the predecessor and the reason",
              out.startswith("[Chapter_0002] [warn] recap backfill failed"
                             " for Chapter_0001.md: ")
              and "translated chapter missing" in out and out.count("\n") == 1,
              f"out={out!r}")

    # the LLM call itself dies
    with tempfile.TemporaryDirectory() as td:
        root, manifest = make_project(td, files, translated=["Chapter_0001.md"])
        recap, out = capture(story.ensure_recap, root, CFG, manifest,
                             "Chapter_0002.md", "[Chapter_0002]", chat=broken_chat)
        check("8c fail: LLM error -> \"\" with the warn line",
              recap == ""
              and out == "[Chapter_0002] [warn] recap backfill failed"
                         " for Chapter_0001.md: endpoint down\n",
              f"recap={recap!r} out={out!r}")
        check("8d fail: no state written on failure",
              not (root / "story_state.json").exists(), "")


def case_9_record_recap() -> None:
    """Overwrites the chapter's own entry unconditionally; neighbors intact;
    prompt carries prev_recap_text + title + body; failure leaves state
    byte-unchanged."""
    files = ["Chapter_0001.md", "Chapter_0002.md", "Chapter_0003.md"]
    with tempfile.TemporaryDirectory() as td:
        root, _manifest = make_project(td, files)
        write_lf(root / "story_state.json", json.dumps(
            {"chapters": {"Chapter_0001": entry("R1."),
                          "Chapter_0002": entry("Old own recap.")}}, ensure_ascii=False) + "\n")
        prompts: list[str] = []
        _none, out = capture(
            story.record_recap, root, CFG, "Chapter_0002.md",
            "The Second Chapter", "Fresh body line.", "R1.",
            "[Chapter_0002]", chat=fake_recap_chat("New own recap.", sink=prompts))
        state = story.load_state(root)
        check("9a record: chapter's own entry overwritten",
              state["chapters"]["Chapter_0002"]["recap"] == "New own recap.",
              f"state={state}")
        check("9b record: neighbor entries intact",
              state["chapters"]["Chapter_0001"]["recap"] == "R1.", "")
        check("9c record: exactly ONE LLM call",
              len(prompts) == 1, f"calls={len(prompts)}")
        check("9d record: prompt carries the passed-in previous recap",
              "R1." in prompts[0], "")
        check("9e record: prompt carries the translated title and body",
              '"The Second Chapter"' in prompts[0]
              and "Fresh body line." in prompts[0], f"prompt={prompts[0][:200]!r}")
        check("9f record: console prints the init line",
              out == "[Chapter_0002] [init] recap\n", f"out={out!r}")

    # failure: prior state byte-unchanged
    with tempfile.TemporaryDirectory() as td:
        root, _manifest = make_project(td, files)
        seeded = {"chapters": {"Chapter_0001": entry("R1."),
                               "Chapter_0002": entry("Old own recap.")}}
        write_lf(root / "story_state.json",
                 json.dumps(seeded, ensure_ascii=False, indent=2) + "\n")
        before = (root / "story_state.json").read_bytes()
        _none, out = capture(
            story.record_recap, root, CFG, "Chapter_0002.md",
            "T", "B", "R1.", "[Chapter_0002]", chat=broken_chat)
        check("9g record fail: warn line names the file and the reason",
              out == "[Chapter_0002] [warn] recap generation failed"
                     " for Chapter_0002.md: endpoint down\n", f"out={out!r}")
        check("9h record fail: prior state byte-unchanged",
              (root / "story_state.json").read_bytes() == before, "")


def case_10_schema() -> None:
    """RECAP_SCHEMA: flat, single required 'recap' string key (the
    VERDICT_SCHEMA construction style)."""
    schema = story.RECAP_SCHEMA
    check("10a schema: single required 'recap' key",
          schema.get("required") == ["recap"] and schema.get("type") == "object",
          f"schema={schema}")
    check("10b schema: flat properties, recap is a string, strict",
          set(schema.get("properties", {})) == {"recap"}
          and schema["properties"]["recap"].get("type") == "string"
          and schema.get("additionalProperties") is False,
          f"schema={schema}")


def main() -> int:
    # CJK output must survive non-UTF-8 consoles/pipes (e.g. Windows cp1252)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_predecessor()
    case_2_load_state()
    case_3_roundtrip()
    case_4_story_part()
    case_5_ensure_recap_hit()
    case_6_ensure_recap_backfill()
    case_7_ensure_recap_no_chain()
    case_8_ensure_recap_failure()
    case_9_record_recap()
    case_10_schema()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
