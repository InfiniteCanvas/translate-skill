"""The glossary entry's story context.

    uv run tests/test_glossary_context.py

The definition says what a term means; the CONTEXT says how THIS story
uses it, who uses it, and what it must not be confused with. It is the
one piece of glossary knowledge the translator never saw, and it is what a
term pinned before the field existed is missing.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "requests>=2.31", "openai>=1.60", "anthropic>=1.0"]
# ///
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary, pipeline, verify

PASSED = 0
FAILED: list[str] = []

ENTRY = {
    "source": "厢房",
    "translation": "side room",
    "variants": ["廂房"],
    "alt_translations": ["wing room"],
    "definition": "the room she sleeps in",
    "category": "place",
    "origin": "model",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def case_setting_the_context() -> None:
    """A context is metadata: the pin and the rest are not touched."""
    new, changes = glossary.set_fields(ENTRY, context="the room behind the hall")
    check("the context is set",
          new["context"] == "the room behind the hall", repr(new))
    check("the change is reported",
          changes == [("context", None, "the room behind the hall")], repr(changes))
    check("the pin is untouched", new["translation"] == "side room", repr(new))
    check("the variants are untouched", new["variants"] == ["廂房"], repr(new))
    check("the alternative renderings are untouched",
          new["alt_translations"] == ["wing room"], repr(new))


def case_clearing_the_context() -> None:
    """An empty context clears it."""
    entry = dict(ENTRY, context="something")
    new, changes = glossary.set_fields(entry, context="")
    check("an empty context clears it", new["context"] == "", repr(new))
    check("the clearing is reported", len(changes) == 1, repr(changes))


def case_no_context_is_valid() -> None:
    """An entry written before the field existed stays valid."""
    new, changes = glossary.set_fields(ENTRY, definition="another definition")
    check("an entry without a context is valid",
          "context" not in new or not new.get("context"), repr(new))
    check("the other edit still lands", len(changes) == 1, repr(changes))


def case_the_context_reaches_the_translator() -> None:
    """The context travels with the term into the prompt."""
    entry = dict(ENTRY, context="the room behind the hall, not the storeroom")
    rendered = glossary.render_contextual([(entry, 3)])
    check("the context is in the translator's context",
          "the room behind the hall" in rendered, rendered)
    check("the term and its rendering are still there",
          rendered.startswith("厢房 translates to \"side room\""), rendered)
    plain = glossary.render_contextual([(ENTRY, 1)])
    check("an entry without a context renders as before",
          plain == "厢房 translates to \"side room\"", plain)


def case_a_proposal_does_not_erase_it() -> None:
    """A proposal that carries no context leaves the existing one alone."""
    g = {"terms": [dict(ENTRY, context="the room behind the hall")]}
    glossary.upsert(g, {"source": "厢房", "translation": "side room",
                        "variants": [], "definition": "x", "category": "place"})
    check("the context survives a proposal without one",
          g["terms"][0].get("context") == "the room behind the hall", repr(g["terms"][0]))


def case_a_proposal_carries_its_own() -> None:
    """A proposal that carries a context sets it."""
    g = {"terms": [dict(ENTRY)]}
    glossary.upsert(g, {"source": "厢房", "translation": "side room",
                        "variants": [], "definition": "x",
                        "context": "the room behind the hall", "category": "place"})
    check("the proposed context is written",
          g["terms"][0].get("context") == "the room behind the hall",
          repr(g["terms"][0]))


def case_merging() -> None:
    """The kept entry's context wins; the removed one's fills an empty one."""
    g = {"terms": [dict(ENTRY, context="the room behind the hall"),
                   {"source": "库房", "translation": "storeroom", "variants": [],
                    "alt_translations": [], "definition": "d", "category": "place",
                    "context": "another context"}]}
    glossary.merge_entries(g, "厢房", "库房")
    check("a merge keeps the kept entry's context",
          g["terms"][0].get("context") == "the room behind the hall", repr(g["terms"][0]))

    g2 = {"terms": [dict(ENTRY),
                    {"source": "库房", "translation": "storeroom", "variants": [],
                     "alt_translations": [], "definition": "d", "category": "place",
                     "context": "the storehouse"}]}
    glossary.merge_entries(g2, "厢房", "库房")
    check("a merge fills an empty context from the removed entry",
          g2["terms"][0].get("context") == "the storehouse", repr(g2["terms"][0]))


def case_the_backfill_applier() -> None:
    """The backfill only fills the entries that lack a context."""
    g = {"terms": [dict(ENTRY), dict(ENTRY, source="库房", context="the storehouse")]}
    written, skipped = pipeline.apply_contexts(g, [
        {"source": "厢房", "context": "the room behind the hall"},
        {"source": "库房", "context": "a new context"},
        {"source": "not a term", "context": "x"},
    ])
    check("one context is written", written == 1, repr(written))
    check("the unknown term is skipped", skipped == ["not a term"], repr(skipped))
    check("the entry that lacked one has it now",
          g["terms"][0].get("context") == "the room behind the hall", repr(g["terms"][0]))
    check("the entry that had one keeps it",
          g["terms"][1].get("context") == "the storehouse", repr(g["terms"][1]))


def case_the_pending_terms() -> None:
    """The backfill knows which entries it is looking for."""
    g = {"terms": [dict(ENTRY), dict(ENTRY, source="库房", context="the storehouse")]}
    pending = pipeline.pending_context_terms(g)
    check("only the entries without a context are pending",
          [e["source"] for e in pending] == ["厢房"], repr(pending))


def case_the_verifier_sees_it() -> None:
    """The judge sees the context when the entry carries one."""
    term = {"source": "厢房", "pinned": "side room", "definition": "d",
            "category": "place", "context": "the room behind the hall"}
    block = verify._term_block(term)
    check("the verifier's block carries the context",
          block.get("context") == "the room behind the hall", repr(block))
    plain = verify._term_block({"source": "厢房", "pinned": "side room",
                                 "definition": "d", "category": "place"})
    check("an entry without a context adds no empty field",
          "context" not in plain, repr(plain))


def case_the_model_may_propose_it() -> None:
    """The proposal schema accepts a context, and does not require it."""
    props = pipeline.TERMS_SCHEMA["properties"]["terms"]["items"]
    check("the schema knows the context", "context" in props["properties"], repr(props))
    check("the schema does not require it",
          "context" not in props["required"], repr(props))


def main() -> int:
    for case in (case_setting_the_context, case_clearing_the_context,
                 case_no_context_is_valid, case_the_context_reaches_the_translator,
                 case_a_proposal_does_not_erase_it, case_a_proposal_carries_its_own,
                 case_merging, case_the_backfill_applier, case_the_pending_terms,
                 case_the_verifier_sees_it, case_the_model_may_propose_it):
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
