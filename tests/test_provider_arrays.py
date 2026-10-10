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
an authored 2-element translator list through unchanged. Per-job arbitrators
(v015): an absent `consensus_<job>` resolves to the global consensus block
IDENTICALLY (same dict object), an authored one wins key-wise while inheriting
the global's base_url and auth, a bare-dict one is shape-normalized to a
one-element list (which is what keeps it in the run_start provider_jobs
snapshot) but NOT key-prefilled, every PROVIDER_JOBS member except consensus is
a legal suffix, and a two-block arbitrator, a typo'd suffix,
`consensus_consensus` and an empty array each raise.

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
        # Authored only because a two-block translator fans out, and a
        # fanning-out config must name its arbitrator (v015). This case is
        # about array passthrough, not about consensus.
        "consensus": {"model": "arb"},
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
    0.7 does not leak into glossary).

    `consensus` is authored here deliberately. As of v015 a config that fans
    out without one is REFUSED at load, so the inheritance rule that decides
    how many jobs fan out can only be exercised alongside an explicit
    arbitrator. That is also why d3 now asserts the authored block is left
    alone rather than derived -- see case_consensus_derived_fallback for the
    derived path and why load_config can no longer reach it with a fan-out."""
    norm = config._normalize_providers({
        "translator": [
            {"base_url": MINE, "model": "m1"},
            {"base_url": OTHER, "model": "m2"},
        ],
        "consensus": {"base_url": MINE, "model": "arb"},
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
    check("d3 inheritance: an authored consensus is used verbatim and does not "
          "pick up the translator array",
          len(consensus) == 1
          and consensus[0]["base_url"] == MINE and consensus[0]["model"] == "arb"
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


def case_consensus_derived_fallback() -> None:
    """`consensus` still resolves to translator[0] when unauthored -- but only
    a config with NO fan-out can reach that state through load_config, because
    a fanning-out config without an explicit arbitrator is now refused.

    The derivation is kept, not deleted: consensus_provider's absent-key path
    calls provider(cfg, "consensus"), and a hand-built cfg bypasses
    load_config entirely, so removing it would trade a harmless fallback for a
    KeyError. What the refusal changes is that it is no longer a live routing
    rule -- it cannot be reached with a multi-block job, which is the only
    situation in which an arbitrator is ever called.
    """
    # Single block: nothing fans out, so nothing needs an arbitrator and the
    # derived block is still what gets built.
    norm = config._normalize_providers(
        {"translator": [{"base_url": MINE, "model": "m1", "temperature": 0.3}]})
    derived = norm["consensus"][0]
    check("g1 derived fallback: with no fan-out, consensus still takes "
          "translator[0]'s authored keys (url, model) at the consensus "
          "temperature for the rest",
          len(norm["consensus"]) == 1
          and derived["base_url"] == MINE and derived["model"] == "m1"
          and derived["temperature"] == 0.2,
          f"consensus={norm['consensus']!r}")

    # Two blocks with no `consensus`: refused, and the message names every job
    # that was going to fan out -- all six, since omitted jobs inherit the
    # translator's whole array.
    two = {"translator": [{"base_url": MINE, "model": "m1"},
                          {"base_url": OTHER, "model": "m2"}]}
    refusal = raises_value_error(two)
    check("g2 refusal: a fan-out with no authored consensus is refused",
          refusal is not None
          and refusal.startswith("providers.consensus is required because"),
          f"got {refusal!r}")
    check("g3 refusal: the message names EVERY job that fans out, including "
          "the ones the file never mentioned (they inherit the array)",
          all(job in refusal for job in ("translator", "glossary", "reviewer",
                                         "annotator", "recap", "profile")),
          f"got {refusal!r}")
    check("g4 refusal: it offers both remedies -- author one, or fan out less",
          "Add providers.consensus" in refusal
          and "single provider block" in refusal, f"got {refusal!r}")
    check("g5 refusal: a per-job arbitrator does NOT substitute for the global",
          raises_value_error({**two, "consensus_translator": [{"model": "x"}]})
          is not None,
          "a lone consensus_<job> was accepted")
    check("g6 no refusal once the global is authored",
          raises_value_error({**two, "consensus": [{"model": "arb"}]}) is None,
          "an explicit consensus was still refused")


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
            ],
                # A two-block translator fans out, so the arbitrator must be
                # named explicitly (v015) or the load is refused.
                "consensus": {"model": "arb"}},
        }, indent=2) + "\n", encoding="utf-8")
        cfg = config.load_config(proj)
        check("h4 load_config: authored 2-block translator list passes through",
              len(cfg["providers"]["translator"]) == 2
              and cfg["providers"]["translator"][0]["model"] == "m1"
              and cfg["providers"]["translator"][1]["model"] == "m2"
              and cfg["providers"]["glossary"][1]["base_url"] == OTHER,
              f"translator={cfg['providers']['translator']!r}")


def case_per_job_consensus() -> None:
    """Per-job arbitrators (v015): an authored `consensus_<job>` overrides the
    global `consensus` for that job's merge, and an ABSENT key resolves to the
    global block UNCHANGED (identity, not a copy).

    The identity half is the whole backward-compatibility contract: every
    project that predates per-job arbitrators must reach the exact same block
    object it reached before, which is what tests/test_consensus.py case (b2)
    asserts with `c["block"] is cblock`."""
    two = {"translator": [{"base_url": MINE, "model": "m1"},
                          {"base_url": OTHER, "model": "m2"}],
           # A two-block translator fans out, so the global arbitrator is now
           # REQUIRED (v015). It is authored here rather than relied on from
           # translator[0], which load_config can no longer reach with a
           # fan-out -- see case_consensus_derived_fallback.
           "consensus": [{"base_url": MINE, "model": "global-arb"}]}

    # --- absent: identity with the global block, for every job ------------
    norm = config._normalize_providers(json.loads(json.dumps(two)))
    cfg = {"providers": norm}
    global_block = norm["consensus"][0]
    check("i1 absent: every job resolves to the global block object itself",
          all(config.consensus_provider(cfg, job) is global_block
              for job in config.CONSENSUS_JOBS),
          f"global={global_block!r}")
    check("i2 absent: a hand-built (unnormalized) cfg resolves identically",
          config.consensus_provider(
              {"providers": {"translator": [{"base_url": MINE}], "consensus": [global_block]}},
              "translator") is global_block)

    # --- authored: wins key-wise, inherits endpoint and auth ---------------
    authored = config._normalize_providers(json.loads(json.dumps({
        **two,
        "consensus": [{"base_url": OTHER, "model": "global-arb",
                       "api_key_env": "ARB_KEY", "max_tokens": 128000}],
        "consensus_translator": [{"model": "big-arbiter"}],
    })))
    acfg = {"providers": authored}
    t = config.consensus_provider(acfg, "translator")
    check("i3 partial block: authored model wins",
          t["model"] == "big-arbiter", f"got {t['model']!r}")
    check("i4 partial block: base_url and auth INHERITED from the global "
          "(never re-pointed at the hard-coded DEFAULT_BASE_URL)",
          t["base_url"] == OTHER and t["api_key_env"] == "ARB_KEY",
          f"block={t!r}")
    check("i5 partial block: sampling defaults still filled in",
          t["temperature"] == config.PROVIDER_DEFAULTS["consensus"]["temperature"]
          and t["max_tokens"] == 128000, f"block={t!r}")
    check("i6 routing: another job is untouched by the translator's arbitrator",
          config.consensus_provider(acfg, "annotator")["model"] == "global-arb",
          f"got {config.consensus_provider(acfg, 'annotator')['model']!r}")

    # --- layering always builds a NEW dict, never the global's own object ---
    # The identity guarantee in i1 covers the ABSENT-key path only. When a
    # per-job key IS authored, the result is a merged copy, so a caller cannot
    # mutate the global block through the job's arbitrator.
    layered = config.consensus_provider(acfg, "translator")
    check("i7 layered: an authored per-job key yields a NEW merged block, never "
          "the global block's own object",
          layered is not acfg["providers"]["consensus"][0]
          and layered["model"] == "big-arbiter"
          and acfg["providers"]["consensus"][0]["model"] == "global-arb",
          f"block={layered!r}")

    # --- shape normalization: a bare dict becomes a list, so the run_start
    # --- provider_jobs snapshot (which filters isinstance(blocks, list)) keeps it
    bare = config._normalize_providers(json.loads(json.dumps({
        **two, "consensus_translator": {"model": "bare-dict-shape"}})))
    check("i8 shape: a bare-dict consensus_<job> is normalized to a one-element "
          "list so it survives the run ledger's isinstance(list) filter",
          isinstance(bare["consensus_translator"], list)
          and len(bare["consensus_translator"]) == 1,
          f"got {bare['consensus_translator']!r}")
    check("i9 shape: keys are NOT pre-filled by the normalizer (resolution is "
          "at read time, so hand-built cfgs agree with loaded ones)",
          bare["consensus_translator"][0] == {"model": "bare-dict-shape"},
          f"got {bare['consensus_translator'][0]!r}")

    # --- every real job is a legal suffix ---------------------------------
    ok = True
    for job in config.CONSENSUS_JOBS:
        n = config._normalize_providers(json.loads(json.dumps({
            **two, f"consensus_{job}": [{"model": f"arb-{job}"}]})))
        ok = ok and config.consensus_provider(
            {"providers": n}, job)["model"] == f"arb-{job}"
    check("i10 suffixes: every PROVIDER_JOBS member except consensus is accepted",
          ok and config.CONSENSUS_JOBS == (
              "translator", "glossary", "reviewer", "annotator", "recap", "profile"),
          f"jobs={config.CONSENSUS_JOBS}")

    # --- validation --------------------------------------------------------
    two_own = raises_value_error({**two, "consensus_annotator": [
        {"model": "a"}, {"model": "b"}]})
    check("i11 errors: a two-block per-job arbitrator is rejected with the "
          "exactly-one message naming that key",
          two_own == "providers.consensus_annotator must list exactly one model "
          "(got 2)", f"got {two_own!r}")
    typo = raises_value_error({**two, "consensus_translatior": [{"model": "x"}]})
    check("i12 errors: a typo'd suffix is rejected rather than silently ignored "
          "(an unknown providers key is otherwise passed through untouched, so "
          "the job would quietly keep merging through the global arbitrator)",
          typo is not None and "consensus_translatior" in typo
          and "names no job" in typo and "translator" in typo, f"got {typo!r}")
    meta = raises_value_error({**two, "consensus_consensus": [{"model": "x"}]})
    check("i13 errors: consensus_consensus is refused (consensus is the global "
          "arbitrator and can never itself merge candidates)",
          meta is not None and "names no job" in meta, f"got {meta!r}")
    try:
        config.consensus_provider(
            {"providers": {"consensus": [dict(global_block)],
                           "consensus_translator": []}}, "translator")
        empty = None
    except ValueError as exc:
        empty = str(exc)
    check("i14 errors: a malformed per-job value raises the contractual "
          "message instead of an IndexError",
          empty == "providers.consensus_translator must not be an empty array",
          f"got {empty!r}")

    # --- on-disk round trip: the run ledger keeps the arbitrator ----------
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "config.json").write_text(json.dumps({
            "source_lang": "zh", "target_lang": "en",
            "providers": {
                "translator": [{"base_url": MINE, "model": "m1"},
                               {"base_url": OTHER, "model": "m2"}],
                "consensus": {"base_url": MINE, "model": "global-arb"},
                "consensus_translator": {"base_url": OTHER, "model": "disk-arb"},
            },
        }, indent=2) + "\n", encoding="utf-8")
        cfg = config.load_config(proj)
        check("i15 load_config: a per-job arbitrator survives as a list and "
              "resolves, inheriting temperature 0.2 from the consensus defaults",
              isinstance(cfg["providers"]["consensus_translator"], list)
              and config.consensus_provider(cfg, "translator")["model"] == "disk-arb"
              and config.consensus_provider(cfg, "translator")["temperature"] == 0.2,
              f"got {cfg['providers']['consensus_translator']!r}")


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
    case_per_job_consensus()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
