# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import glossary

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


LINGEN_ENTRY = {
    "source": "灵根", "variants": ["靈根"],
    "translation": "spirit root", "alt_translations": [],
    "definition": "Innate aptitude for cultivation.", "category": "level",
    "origin": "seeded", "first_seen_chapter": None,
}


def keep_entry() -> dict:
    return {
        "source": "灵砂", "variants": [], "translation": "spirit sand",
        "alt_translations": [], "definition": "Sand of the spirit world.",
        "category": "item", "origin": "seeded", "first_seen_chapter": 1,
    }


def remove_entry() -> dict:
    return {
        "source": "灵石", "variants": ["靈石"],
        "translation": "spirit stone", "alt_translations": ["Spirit gem"],
        "definition": "Currency of the spirit world.",
        "category": "item", "origin": "seeded", "first_seen_chapter": 2,
    }


def make_g(keep: dict, remove: dict) -> dict:
    return {"terms": [dict(keep), dict(remove)]}


def case_1_happy_path() -> None:
    """Pin the merge contract nothing covered before: variant/alt union
    (keep-first, deduped), definition filled when the kept entry lacks one,
    kept fields preserved verbatim, removed entry dropped, canonical source
    recorded in "retired", exact return tuple, and no warn on the fill
    path."""
    g = make_g(keep_entry(), remove_entry())
    del g["terms"][0]["definition"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        kept, removed_key, variants_added, alt_added, def_filled = (
            glossary.merge_entries(g, "灵砂", "灵石")
        )
    out = buf.getvalue()
    check("1a merge: exact return tuple",
          (removed_key, variants_added, alt_added, def_filled)
          == ("灵石", 1, 1, True),
          f"key={removed_key} v={variants_added} a={alt_added} "
          f"def={def_filled}")
    check("1b merge: removed entry dropped, kept entry stays",
          [e.get("source") for e in g["terms"]] == ["灵砂"],
          f"terms={[e.get('source') for e in g['terms']]}")
    check("1c merge: variants unioned keep-first",
          kept.get("variants") == ["靈石"], f"variants={kept.get('variants')}")
    check("1d merge: alt_translations unioned",
          kept.get("alt_translations") == ["Spirit gem"],
          f"alts={kept.get('alt_translations')}")
    check("1e merge: definition filled from the removed entry",
          kept.get("definition") == "Currency of the spirit world.",
          f"definition={kept.get('definition')!r}")
    check("1f merge: kept fields preserved verbatim",
          kept.get("translation") == "spirit sand"
          and kept.get("category") == "item" and kept.get("origin") == "seeded"
          and kept.get("first_seen_chapter") == 1, f"kept={kept}")
    check("1g merge: canonical source recorded in retired",
          g.get("retired") == ["灵石"], f"retired={g.get('retired')}")
    check("1h merge: definition fill prints no warn",
          "[warn]" not in out, f"out={out!r}")


def case_2_definition_discard_warn() -> None:
    """Both entries have a definition: the removed one is dropped with one
    exact [warn] line naming both canonical sources; the kept definition is
    unchanged and the rest of the merge still applies."""
    g = make_g(keep_entry(), remove_entry())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        kept, removed_key, variants_added, alt_added, def_filled = (
            glossary.merge_entries(g, "灵砂", "灵石")
        )
    out = buf.getvalue()
    check("2a merge: exact warn line",
          "[warn] glossary: definition from '灵石' discarded "
          "('灵砂' already has one)" in out, f"out={out!r}")
    check("2b merge: kept definition unchanged",
          kept.get("definition") == "Sand of the spirit world.",
          f"definition={kept.get('definition')!r}")
    check("2c merge: merge still applied (def_filled False, union + retire "
          "done)",
          def_filled is False and variants_added == 1 and alt_added == 1
          and g.get("retired") == ["灵石"] and len(g["terms"]) == 1,
          f"retired={g.get('retired')} terms={len(g['terms'])}")
    check("2d merge: exactly one warn line printed",
          out.count("[warn]") == 1, f"out={out!r}")


def case_3_no_removed_definition_no_warn() -> None:
    """The removed entry lacks a definition: nothing to discard, no warn,
    kept definition untouched."""
    g = make_g(keep_entry(), remove_entry())
    del g["terms"][1]["definition"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        kept, _key, _v, _a, def_filled = glossary.merge_entries(
            g, "灵砂", "灵石")
    out = buf.getvalue()
    check("3a merge: no warn when the removed entry has no definition",
          "[warn]" not in out, f"out={out!r}")
    check("3b merge: kept definition untouched, nothing filled",
          kept.get("definition") == "Sand of the spirit world."
          and def_filled is False, f"def={kept.get('definition')!r}")


def case_4_remove_literal_recording() -> None:
    """merge_entries(remove_literal=...) records the raw --remove spelling
    alongside the canonical source so the CLI's literal-comparing
    idempotency gate recognizes a re-run; a canonical-equal literal is not
    duplicated, and the default (no kwarg) keeps the canonical-only
    recording."""
    g = make_g(keep_entry(), remove_entry())
    glossary.merge_entries(g, "灵砂", "靈石", remove_literal="靈石")
    check("4a merge: variant literal recorded next to the canonical source",
          g.get("retired") == ["灵石", "靈石"], f"retired={g.get('retired')}")
    check("4b merge: retired_sources sees both spellings (the CLI gate)",
          glossary.retired_sources(g) == {"灵石", "靈石"},
          f"retired={g.get('retired')}")

    g = make_g(keep_entry(), remove_entry())
    glossary.merge_entries(g, "灵砂", "灵石", remove_literal="灵石")
    check("4c merge: canonical-equal literal not duplicated",
          g.get("retired") == ["灵石"], f"retired={g.get('retired')}")

    g = make_g(keep_entry(), remove_entry())
    glossary.merge_entries(g, "灵砂", "靈石")
    check("4d merge: without the kwarg only the canonical source is recorded",
          g.get("retired") == ["灵石"], f"retired={g.get('retired')}")


def case_5_retire_source_literal() -> None:
    """retire(source_literal=...) mirrors the merge contract: the raw
    --source spelling that matched a removed entry is recorded next to the
    canonical source; a canonical-equal literal is not duplicated, the
    default keeps canonical-only recording, and a no-match call records
    nothing."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [dict(LINGEN_ENTRY)]})
        removed = glossary.retire(root, ["靈根"], source_literal="靈根")
        data = json.loads(
            (root / "glossary.json").read_text(encoding="utf-8"))
        check("5a retire: removed list still echoes the caller key",
              removed == ["靈根"], f"removed={removed}")
        check("5b retire: canonical + literal recorded in order",
              data.get("retired") == ["灵根", "靈根"],
              f"retired={data.get('retired')}")
        check("5c retire: retired_sources sees both (the CLI gate)",
              glossary.retired_sources(data) == {"灵根", "靈根"},
              f"retired={data.get('retired')}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [dict(LINGEN_ENTRY)]})
        glossary.retire(root, ["灵根"], source_literal="灵根")
        data = json.loads(
            (root / "glossary.json").read_text(encoding="utf-8"))
        check("5d retire: canonical-equal literal not duplicated",
              data.get("retired") == ["灵根"], f"retired={data.get('retired')}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [dict(LINGEN_ENTRY)]})
        glossary.retire(root, ["靈根"])
        data = json.loads(
            (root / "glossary.json").read_text(encoding="utf-8"))
        check("5e retire: without the kwarg only the canonical source is "
              "recorded",
              data.get("retired") == ["灵根"], f"retired={data.get('retired')}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        glossary.save(root, {"terms": [dict(LINGEN_ENTRY)]})
        removed = glossary.retire(root, ["道基"], source_literal="道基")
        data = json.loads(
            (root / "glossary.json").read_text(encoding="utf-8"))
        check("5f retire: no matching entry -> nothing recorded",
              removed == [] and "retired" not in data,
              f"removed={removed} retired={data.get('retired')}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_happy_path()
    case_2_definition_discard_warn()
    case_3_no_removed_definition_no_warn()
    case_4_remove_literal_recording()
    case_5_retire_source_literal()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
