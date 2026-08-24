"""Configuration loading/saving and per-job LLM provider settings."""

import json
from pathlib import Path

PROVIDER_JOBS = ("translator", "glossary", "reviewer", "annotator", "profile")

DEFAULTS: dict = {
    "seed_min_count": 3,
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
    # the chunking threshold: chapters whose expected output exceeds it split
    # into balanced parts; smaller ones translate whole. Input context is
    # never limited by this.
    "translate_max_output_tokens": 8192,
    # Style-profile generation at init: how many chapters to sample and
    # roughly how many source characters to include in the prompt.
    "style_sample_chapters": 4,
    "style_sample_chars": 12000,
    # Full request/response trace to logs/llm-YYYYMMDD.jsonl.
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
    # and retired (never re-added). Set false to skip the judgment.
    "glossary_auto_cleanup": True,
}

_DEFAULT_BASE_URL = "http://100.85.218.125:8888/v1"
_DEFAULT_MAX_TOKENS = 16384

# translator temperature/top_p follow the Hy-MT2 model card recommendation
# (0.7 / 1.0); every other job keeps the server default for its sampling
# knobs (no top_p key sent). `thinking` maps to sglang's
# chat_template_kwargs.enable_thinking -- false spends the output budget on
# the answer instead of a reasoning chain (recommended for this pipeline);
# set true per job to experiment.
PROVIDER_DEFAULTS: dict[str, dict] = {
    "translator": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.7, "top_p": 1.0, "max_tokens": _DEFAULT_MAX_TOKENS, "thinking": False},
    "glossary": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": _DEFAULT_MAX_TOKENS, "thinking": False},
    "reviewer": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.0, "max_tokens": _DEFAULT_MAX_TOKENS, "thinking": False},
    "annotator": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": _DEFAULT_MAX_TOKENS, "thinking": False},
    "profile": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.3, "max_tokens": _DEFAULT_MAX_TOKENS, "thinking": False},
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


def _normalize_providers(providers: dict) -> dict:
    """Guarantee every job in PROVIDER_JOBS exists with every default key filled.

    A missing job inherits the "translator" block if present; any key still
    missing is filled from PROVIDER_DEFAULTS[job]. Unknown extra jobs are
    passed through untouched.
    """
    translator_block = providers.get("translator")
    normalized: dict[str, dict] = {}
    for job in PROVIDER_JOBS:
        block = providers.get(job)
        if block is None and job != "translator":
            block = translator_block
        merged = dict(PROVIDER_DEFAULTS[job])
        if isinstance(block, dict):
            merged.update(block)
        normalized[job] = merged
    for key, block in providers.items():
        if key not in normalized:
            normalized[key] = block
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
    user = json.loads(cfg_path.read_text(encoding="utf-8"))
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
    """Return the provider block for a job from a loaded config."""
    return cfg["providers"][job]
