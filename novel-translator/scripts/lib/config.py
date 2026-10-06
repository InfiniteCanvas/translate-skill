"""Configuration loading/saving and per-job LLM provider settings."""

import json
from pathlib import Path

PROVIDER_JOBS = ("translator", "glossary", "reviewer", "annotator", "recap", "profile", "consensus")

DEFAULTS: dict = {
    "seed_min_count": 3,
    # Minimum novel-wide occurrences for a glossary-expansion proposal to be
    # added as a brand-new term (0 disables the gate).
    "min_term_occurrences": 3,
    # Canonical glossary rendering must appear >= ceil(coverage*src) times;
    # zero occurrences with src >= 2 is the drift signal handed to the FAITH reviewer.
    "min_term_coverage": 0.25,
    "fuzzy_max_distance": 2,
    "tn_gap_chapters": 10,
    # Keep model self-assessed low-comprehension (threshold "low") notes
    # instead of dropping them; some models (e.g. Qwen) self-assess too
    # harshly and their low notes are still useful.
    "tn_keep_low_confidence": False,
    "max_attempts": 3,
    # Runaway safety valve only: every glossary term present in the chapter
    # goes into the prompt; this caps the rendered list if it ever explodes.
    "contextual_glossary_cap": 200,
    "max_new_terms_per_chapter": 15,
    "max_notes_per_chapter": 10,
    # Per-call OUTPUT cap for translation (model card recommends 4k-8k) and
    # the packing budget: long chapters split into parts sized so each
    # part's expected output (per-line cost, 0.8 headroom) fits this cap;
    # smaller ones translate whole. Input context is never limited by this.
    "translate_max_output_tokens": 8192,
    # Style-profile generation at init: how many chapters to sample and
    # roughly how many source characters to include in the prompt.
    "style_sample_chapters": 4,
    "style_sample_chars": 12000,
    # Full request/response trace, one log per CLI invocation (see log_llm_keep_runs).
    "log_llm": True,
    # One logs/llm-*.jsonl per CLI invocation; at each run's start older
    # logs are pruned to the newest log_llm_keep_runs files (by mtime).
    "log_llm_keep_runs": 5,
    # Rebuild the epub in a parallel subprocess after every chapter that
    # finishes translation (serialized; one final build at batch end
    # guarantees completeness). Set false to build only via build-epub.
    "auto_build_epub": True,
    # On balance drift signals, one glossary-job call judges whether each
    # flagged term truly belongs in the glossary; mundane terms are removed
    # and retired (never re-added; the retirement is applied only after the
    # translation passes the faithfulness gate). Set false to skip the
    # judgment.
    "glossary_auto_cleanup": True,
    # Commit every mutating action to the project's git repository (created
    # by `init`, backfilled by migrate v003). Set false to keep a project
    # un-versioned; lib/vcs.commit is the single gate.
    "git_commits": True,
    # `review glossary` / `review notes` batching: entries per model review
    # call; also settable per run with `--batch-size`.
    "review_batch_size": 40,
    # Filename of the advisory review report written by `review glossary` /
    # `review notes` and read back by `review fix`; relative to the project
    # dir.
    "review_report_path": "review-report.md",
}

DEFAULT_BASE_URL = "http://100.85.218.125:8888/v1"
DEFAULT_MAX_TOKENS = 16384

# translator temperature/top_p follow the Hy-MT2 model card recommendation
# (0.7 / 1.0); every other job keeps the server default for its sampling
# knobs (no top_p key sent). `thinking` maps to sglang's
# chat_template_kwargs.enable_thinking -- false spends the output budget on
# the answer instead of a reasoning chain (recommended for this pipeline);
# set true per job to experiment.
PROVIDER_DEFAULTS: dict[str, dict] = {
    "translator": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.7, "top_p": 1.0, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "glossary": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "reviewer": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.0, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "annotator": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    # Rolling story-so-far recap generation (one cheap call per translated
    # chapter, story_state.json); point it at a cheap model.
    "recap": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "profile": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.3, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    # The consensus job merges the multi-model candidates of a fan-out into
    # the final response (one call per fan-out); temperature 0.2 like the
    # annotator/glossary synthesis jobs.
    "consensus": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override onto base, returning new dicts."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _provider_blocks(providers: dict, job: str) -> list[dict] | None:
    """A job's authored providers value as a list of blocks, or None when the
    job is absent. A bare dict is the legacy single-block shape and wraps into
    a one-element list; an array is the multi-model shape. The three
    ValueError texts are contractual (mirrored in references/file-formats.md
    and asserted verbatim by the tests)."""
    value = providers.get(job)
    if value is None:
        return None
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        raise ValueError(
            f"providers.{job} must be a provider block (object) or an array of blocks")
    if not value:
        raise ValueError(f"providers.{job} must not be an empty array")
    for i, element in enumerate(value):
        if not isinstance(element, dict):
            raise ValueError(f"providers.{job}[{i}] must be an object")
    return value


def _with_defaults(job: str, element: dict) -> dict:
    """One provider block with PROVIDER_DEFAULTS[job] filling the keys the
    block does not set (authored keys win -- per-key override semantics)."""
    merged = dict(PROVIDER_DEFAULTS[job])
    merged.update(element)
    return merged


def _normalize_providers(providers: dict) -> dict:
    """Guarantee every job in PROVIDER_JOBS exists as a LIST of provider
    blocks with every default key filled.

    providers.<job> is now an array of blocks -- the consensus fan-out
    (lib/consensus.py) runs one model per block. A bare dict is the legacy
    single-block shape and normalizes to a one-element list, so
    pre-consensus config.json files load unchanged. A missing non-translator
    job inherits the translator's whole list, but only its authored keys:
    each element merges onto PROVIDER_DEFAULTS[job], so a single-model
    project keeps every job on its model while each job's own sampling
    defaults (temperature et al.) still apply. The consensus job never
    inherits the array -- it synthesizes ONE final response, so when absent
    it inherits only the translator's FIRST block: that block's authored
    keys win (base_url/model/auth from the project's endpoint, and its
    temperature if it sets one), with the consensus defaults (temperature
    0.2) filling only what it leaves unset -- deliberately not the
    hard-coded DEFAULT_BASE_URL. An explicitly authored consensus array
    with more than one block is rejected; inherited consensus is always
    exactly one block, so legacy configs can never trip that check.
    Unknown extra jobs pass through untouched.
    """
    translator = _provider_blocks(providers, "translator")
    normalized: dict[str, list[dict]] = {}
    for job in PROVIDER_JOBS:
        if job == "translator":
            authored = translator
        else:
            authored = _provider_blocks(providers, job)
            if authored is None:
                if job == "consensus":
                    # The arbitrator is ONE model: first translator block only.
                    authored = [translator[0]] if translator else None
                else:
                    authored = translator
        if authored is None:  # nothing authored to inherit from: plain defaults
            normalized[job] = [_with_defaults(job, {})]
            continue
        if job == "consensus" and len(authored) > 1:
            raise ValueError(
                f"providers.consensus must list exactly one model (got {len(authored)})")
        normalized[job] = [_with_defaults(job, element) for element in authored]
    for key, value in providers.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def load_config(project_dir: Path) -> dict:
    """Read <project_dir>/config.json, deep-merge onto DEFAULTS, and
    guarantee cfg["providers"] contains every job in PROVIDER_JOBS.

    Raises FileNotFoundError with a clear message if config.json is absent.
    """
    cfg_path = Path(project_dir) / "config.json"
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"config.json not found in project directory '{project_dir}' (expected at {cfg_path})"
        )
    user = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    if not isinstance(user, dict):
        raise ValueError(f"{cfg_path} must contain a JSON object")
    cfg = _deep_merge(DEFAULTS, user)
    providers = cfg.get("providers")
    cfg["providers"] = _normalize_providers(providers if isinstance(providers, dict) else {})
    return cfg


def save_config(project_dir: Path, cfg: dict) -> None:
    """Write cfg to <project_dir>/config.json as pretty UTF-8 JSON."""
    cfg_path = Path(project_dir) / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")


def provider(cfg: dict, job: str) -> dict:
    """Return the job's FIRST provider block from a loaded config: the block
    single-model callers always used, and the one block the exactly-one
    consensus job resolves to."""
    return provider_list(cfg, job)[0]


def provider_list(cfg: dict, job: str) -> list:
    """Return the job's provider blocks (>= 1 after load_config
    normalization): one block per model -- what the consensus fan-out
    iterates.

    Dict-tolerant at read time: cfg["providers"][job] may still be a bare
    dict -- hand-built cfg dicts in tests never pass through load_config's
    normalizer, so a dict is wrapped into [dict] on the fly to keep them
    working."""
    blocks = cfg["providers"][job]
    return [blocks] if isinstance(blocks, dict) else blocks


def _as_number(key: str, value: object) -> int | float:
    """Shared numeric validation behind get_number(): int/float (bool
    excluded -- bool is an int subclass, and a JSON true/false in a numeric
    key is always a mistake) or a string that int() then float() can parse.
    The ValueError text is contractual (test_config_coerce.py asserts it
    verbatim)."""
    if isinstance(value, bool):
        raise ValueError(f"config key '{key}' must be a number (got {value!r})")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
    raise ValueError(f"config key '{key}' must be a number (got {value!r})")


def get_number(cfg: dict, key: str) -> int | float:
    """cfg[key] as a number, for the config keys the pipeline does
    arithmetic on. Accepts int/float (bool excluded) and numeric strings
    ("3" -> 3, "2.5" -> 2.5); anything else and a missing key raise
    ValueError naming the key, so a bad config.json maps to a clean CLI
    failure instead of a bare TypeError from int()/float(). No DEFAULTS
    fallback: callers reading a load_config() result always find the key."""
    if key not in cfg:
        raise ValueError(f"config key '{key}' is missing")
    return _as_number(key, cfg[key])
