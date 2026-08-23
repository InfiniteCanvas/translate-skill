"""Configuration loading/saving and per-job LLM provider settings."""

import json
from pathlib import Path

PROVIDER_JOBS = ("translator", "glossary", "reviewer", "annotator")

DEFAULTS: dict = {
    "seed_min_count": 3,
    "balance_tolerance": 0.2,
    "fuzzy_max_distance": 2,
    "tn_gap_chapters": 10,
    "max_attempts": 3,
    "contextual_glossary_cap": 80,
    "max_new_terms_per_chapter": 15,
    "max_notes_per_chapter": 10,
    # Chapters longer than this many lines are translated in balanced chunks;
    # models cannot hold an exact line count over 100+ lines in one pass.
    "translate_chunk_size": 40,
}

_DEFAULT_BASE_URL = "http://100.85.218.125:8888/v1"
_DEFAULT_MAX_TOKENS = 16384

PROVIDER_DEFAULTS: dict[str, dict] = {
    "translator": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.3, "max_tokens": _DEFAULT_MAX_TOKENS},
    "glossary": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": _DEFAULT_MAX_TOKENS},
    "reviewer": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.0, "max_tokens": _DEFAULT_MAX_TOKENS},
    "annotator": {"base_url": _DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": _DEFAULT_MAX_TOKENS},
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
    """Guarantee all four PROVIDER_JOBS exist with every default key filled.

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
    guarantee cfg["providers"] contains all four PROVIDER_JOBS.

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
