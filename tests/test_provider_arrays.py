"""Tests for the provider-array normalization in lib/config.py: the
multi-model shape of providers.<job> and the consensus job's rules.

Covered (against config._normalize_providers directly on raw provider
dicts, plus config.load_config round trips on TemporaryDirectory
projects): the "consensus" job is registered last in PROVIDER_JOBS with
temperature 0.2; a legacy single-block dict wraps into a one-element
list; an authored array passes through with per-element default fill
(missing keys come from PROVIDER_DEFAULTS[job], authored keys win); an
omitted non-translator job inherits the translator's whole list
element-wise at ITS OWN defaults (the translator's default temperature
0.7 does not leak; glossary's 0.2 applies); an omitted consensus job is
exactly the translator's FIRST block at the consensus temperature 0.2;
an explicitly authored 2-block consensus array raises the exact
contractual ValueError, as do an empty array, a non-object value, and a
non-dict array element; unknown extra jobs pass through untouched; a
missing translator leaves every job at plain defaults; provider() /
provider_list() tolerate hand-built dict-shaped cfgs (no load_config
pass) by wrapping a bare dict on the fly; and load_config normalizes a
dict-shaped config.json on disk into arrays (translator list of one with
user values inside, consensus inherited from translator[0]) and passes
an authored 2-element translator list through unchanged.

Self-contained PASS/FAIL script (no pytest). lib.config imports nothing
beyond the stdlib, but the header below keeps the house dependency set
so `uv run tests/test_provider_arrays.py` works from any environment:

    uv run tests/test_provider_arrays.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# scripts/ (and therefore lib/) lives at novel-translator/scripts
# relative to this file (CWD-independent).
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config  # noqa: E402

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


MINE = "http://mine:9999/v1"
OTHER = "http://other:7777/v1"


def raises_value_error(providers: dict) -> str | None:
    """Call _normalize_providers and return str(exc) when it raised a
    ValueError, None otherwise (any other exception type fails loudly by
    being re-raised)."""
    try:
        config._normalize_providers(providers)
    except ValueError as exc:
        return str(exc)
    return None


def case_registry() -> None:
    """The consensus job exists, is the LAST provider job, and carries the
    consensus sampling profile (temperature 0.2 like the annotator/glossary
    synthesis jobs)."""
    check("a1 registry: consensus is the last PROVIDER_JOBS entry",
          config.PROVIDER_JOBS[-1] == "consensus"
          and len(set(config.PROVIDER_JOBS)) == len(config.PROVIDER_JOBS),
          f"jobs={config.PROVIDER_JOBS}")
    check("a2 registry: consensus PROVIDER_DEFAULTS block at temperature 0.2",
          "consensus" in config.PROVIDER_DEFAULTS
          and config.PROVIDER_DEFAULTS["consensus"]["temperature"] == 0.2
          and config.PROVIDER_DEFAULTS["consensus"]["max_tokens"]
          == config.DEFAULT_MAX_TOKENS,
          f"defaults={config.PROVIDER_DEFAULTS.get('consensus')!r}")


def case_legacy_dict_wraps() -> None:
    """A bare dict (the legacy single-block shape) normalizes to a
    one-element list with every default key filled."""
    norm = config._normalize_providers(
        {"translator": {"base_url": MINE, "model": "m1"}})
    check("b1 dict wrap: translator becomes a one-element list",
          isinstance(norm["translator"], list) and len(norm["translator"]) == 1,
          f"translator={norm['translator']!r}")
    block = norm["translator"][0]
    check("b2 dict wrap: user keys preserved, defaults filled around them",
          block["base_url"] == MINE and block["model"] == "m1"
          and block["temperature"] == 0.7 and block["top_p"] == 1.0
          and block["max_tokens"] == config.DEFAULT_MAX_TOKENS
          and block["thinking"] is False,
          f"block={block!r}")
    check("b3 dict wrap: every job normalized to a non-empty list",
          sorted(norm) == sorted(config.PROVIDER_JOBS)
          and all(isinstance(v, list) and v for v in norm.values()),
          f"jobs={sorted(norm)}")


def case_list_passthrough() -> None:
    """An authored array passes through element-wise: each element merges
    onto PROVIDER_DEFAULTS[job] with authored keys winning, so a sparse
    block gets the defaults while a fully-specified sibling stays as
    authored."""
    norm = config._normalize_providers({
        "translator": [
            {"model": "a"},
            {"base_url": OTHER, "model": "b", "temperature": 0.1},
        ],
    })
    check("c1 list pass-through: both blocks kept in order with defaults filled",
          norm["translator"] == [
              dict(config.PROVIDER_DEFAULTS["translator"], model="a"),
              dict(config.PROVIDER_DEFAULTS["translator"],
                   base_url=OTHER, model="b", temperature=0.1),
          ],
          f"translator={norm['translator']!r}")


def case_inheritance() -> None:
    """Omitted jobs inherit the translator's list; each job's own sampling
    defaults apply (only AUTHORED keys inherit, so the translator's default
    0.7 does not leak into glossary), and consensus takes translator[0]
    only, at 0.2."""
    norm = config._normalize_providers({
        "translator": [
            {"base_url": MINE, "model": "m1"},
            {"base_url": OTHER, "model": "m2"},
        ],
    })
    glossary = norm["glossary"]
    check("d1 inheritance: glossary inherits the whole translator list element-wise",
          len(glossary) == 2
          and glossary[0]["base_url"] == MINE and glossary[0]["model"] == "m1"
          and glossary[1]["base_url"] == OTHER and glossary[1]["model"] == "m2",
          f"glossary={glossary!r}")
    check("d2 inheritance: glossary temperature 0.2 applies (translator 0.7 does not leak)",
          glossary[0]["temperature"] == 0.2 and glossary[1]["temperature"] == 0.2,
          f"temps={[b.get('temperature') for b in glossary]!r}")
    consensus = norm["consensus"]
    check("d3 inheritance: consensus is translator[0] only at temperature 0.2",
          len(consensus) == 1
          and consensus[0]["base_url"] == MINE and consensus[0]["model"] == "m1"
          and consensus[0]["temperature"] == 0.2,
          f"consensus={consensus!r}")
    # An authored temperature on the translator block DOES inherit (the
    # per-key override semantics apply to consensus like every other job:
    # authored keys win, job defaults fill only the unauthored ones).
    norm_pinned = config._normalize_providers({
        "translator": [{"base_url": MINE, "model": "m1", "temperature": 0.3}],
    })
    check("d4 inheritance: an authored translator temperature inherits everywhere",
          norm_pinned["glossary"][0]["temperature"] == 0.3
          and norm_pinned["consensus"][0]["temperature"] == 0.3,
          f"glossary={norm_pinned['glossary']!r} "
          f"consensus={norm_pinned['consensus']!r}")


def case_errors() -> None:
    """The four contractual ValueError texts, asserted verbatim (they are
    mirrored in references/file-formats.md)."""
    check("e1 error: explicit 2-block consensus array",
          raises_value_error({
              "translator": {"base_url": MINE, "model": "m1"},
              "consensus": [{"model": "c1"}, {"model": "c2"}],
          }) == "providers.consensus must list exactly one model (got 2)")
    check("e2 error: empty translator array",
          raises_value_error({"translator": []})
          == "providers.translator must not be an empty array")
    check("e3 error: string instead of a block or array",
          raises_value_error({"translator": "http://mine:9999/v1"})
          == "providers.translator must be a provider block (object) or an array of blocks")
    check("e4 error: non-object array element",
          raises_value_error({"translator": [{"model": "a"}, 5]})
          == "providers.translator[1] must be an object")


def case_unknown_and_missing() -> None:
    """Unknown extra jobs pass through untouched; a missing translator
    leaves every job at plain defaults."""
    weird = {"api_key_env": "MY_KEY", "note": "custom"}
    norm = config._normalize_providers(
        {"translator": {"base_url": MINE, "model": "m1"}, "weird": weird})
    check("f1 unknown job: passes through untouched",
          norm.get("weird") == weird
          and sorted(norm) == sorted((*config.PROVIDER_JOBS, "weird")),
          f"weird={norm.get('weird')!r}")
    empty = config._normalize_providers({})
    check("f2 missing translator: every job at plain one-block defaults",
          sorted(empty) == sorted(config.PROVIDER_JOBS)
          and all(value == [dict(config.PROVIDER_DEFAULTS[job])]
                  for job, value in empty.items()),
          f"empty={ {k: v for k, v in empty.items()} !r}")


def case_dict_tolerance() -> None:
    """provider()/provider_list() on a hand-built dict-shaped cfg (never
    passed through load_config): a bare dict wraps into [dict] on the fly."""
    tdict = {"base_url": MINE, "model": "m1"}
    cdict = {"base_url": MINE, "model": "c-merge", "temperature": 0.2}
    cfg = {"providers": {"translator": tdict, "consensus": cdict}}
    check("g1 dict tolerance: provider_list wraps a bare dict",
          config.provider_list(cfg, "translator") == [tdict]
          and config.provider_list(cfg, "consensus") == [cdict],
          f"t={config.provider_list(cfg, 'translator')!r}")
    check("g2 dict tolerance: provider returns the first (only) block",
          config.provider(cfg, "translator") == tdict
          and config.provider(cfg, "consensus") == cdict)


def case_load_config() -> None:
    """load_config on real temp projects: a dict-shaped config.json on disk
    comes back array-shaped with the consensus job materialized, and an
    authored multi-block translator list passes through unchanged."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "config.json").write_text(json.dumps({
            "source_lang": "zh",
            "target_lang": "en",
            "tn_gap_chapters": 5,
            "providers": {"translator": {"base_url": MINE, "model": "m1"}},
        }, indent=2) + "\n", encoding="utf-8")
        cfg = config.load_config(proj)
        check("h1 load_config: legacy dict on disk loads as a one-block array",
              isinstance(cfg["providers"]["translator"], list)
              and len(cfg["providers"]["translator"]) == 1
              and cfg["providers"]["translator"][0]["base_url"] == MINE
              and cfg["providers"]["translator"][0]["model"] == "m1",
              f"translator={cfg['providers']['translator']!r}")
        check("h2 load_config: consensus materialized from translator[0] at 0.2",
              len(cfg["providers"]["consensus"]) == 1
              and cfg["providers"]["consensus"][0]["base_url"] == MINE
              and cfg["providers"]["consensus"][0]["temperature"] == 0.2,
              f"consensus={cfg['providers']['consensus']!r}")
        check("h3 load_config: every job present as a list, user top-level key kept",
              all(isinstance(cfg["providers"][job], list)
                  for job in config.PROVIDER_JOBS)
              and cfg["tn_gap_chapters"] == 5,
              f"jobs={ {k: type(v).__name__ for k, v in cfg['providers'].items()} }")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "config.json").write_text(json.dumps({
            "providers": {"translator": [
                {"base_url": MINE, "model": "m1"},
                {"base_url": OTHER, "model": "m2"},
            ]},
        }, indent=2) + "\n", encoding="utf-8")
        cfg = config.load_config(proj)
        check("h4 load_config: authored 2-block translator list passes through",
              len(cfg["providers"]["translator"]) == 2
              and cfg["providers"]["translator"][0]["model"] == "m1"
              and cfg["providers"]["translator"][1]["model"] == "m2"
              and cfg["providers"]["glossary"][1]["base_url"] == OTHER,
              f"translator={cfg['providers']['translator']!r}")


def main() -> int:
    # CJK-free output, but keep the house reconfigure guard for consistency
    # with non-UTF-8 consoles/pipes (e.g. Windows cp1252).
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_registry()
    case_legacy_dict_wraps()
    case_list_passthrough()
    case_inheritance()
    case_errors()
    case_unknown_and_missing()
    case_dict_tolerance()
    case_load_config()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
