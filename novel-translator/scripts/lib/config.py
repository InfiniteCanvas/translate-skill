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

# Skill-level settings overlay, copied into every project `init` creates and
# merged on demand by `sync-config`. It holds the same shape as config.json
# (any subset of its keys) and is gitignored, so per-machine details -- most
# usefully the provider endpoint, model, and auth of whoever runs the skill --
# live in exactly one place instead of being retyped per novel.
#
# Deliberately NOT a config.DEFAULTS key: an overlay that is absent must be a
# silent no-op, and a boolean gate would have to be materialized into every
# existing project's config.json (and carried by every migration). The file's
# presence IS the opt-in, so nothing is added to the project schema and no
# migration is required.
LOCAL_CONFIG_NAME = "config.local.json"

# Keys the overlay may never carry. `version` is the project's own migration
# stamp: taking it from the overlay would make `migrate` replay steps the
# project has already run (or skip ones it has not), and the stamp is written
# by `init`/`migrate` alone.
LOCAL_CONFIG_FORBIDDEN = ("version",)

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


def local_config_path(skill_root: Path) -> Path:
    """Where the skill looks for the optional overlay: config.local.json
    directly under the skill root (the novel-translator/ package dir)."""
    return Path(skill_root) / LOCAL_CONFIG_NAME


def load_local_config(skill_root: Path) -> dict | None:
    """Read the skill-level overlay, or None when it does not exist.

    An ABSENT file is the normal case (the overlay is optional) and returns
    None rather than raising, so callers treat it as a silent no-op. A file
    that exists but cannot be read or parsed raises ValueError: silently
    ignoring a malformed overlay would drop the user's endpoint and send the
    next translate at a different model, which is far worse than a clear
    failure. The `version` key is rejected here rather than silently dropped
    (see LOCAL_CONFIG_FORBIDDEN)."""
    path = local_config_path(skill_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    # `providers` is the one key whose shape the merge special-case reaches
    # into directly, so a non-object here would raise AttributeError deep in
    # merge_overlay -- which main() does not catch, turning a typo in the
    # user's own overlay into a traceback and exit 1. Reject it here, where
    # the message can name the file.
    if "providers" in raw and not isinstance(raw["providers"], dict):
        raise ValueError(f"{path}: 'providers' must be a JSON object")
    forbidden = [key for key in LOCAL_CONFIG_FORBIDDEN if key in raw]
    if forbidden:
        raise ValueError(
            f"{path} must not contain {', '.join(forbidden)}: the project "
            "version stamp belongs to init/migrate")
    return raw


def merge_overlay(base: dict, overlay: dict) -> dict:
    """Deep-merge the overlay ONTO base, overlay values winning.

    The project's own keys survive anything the overlay does not mention --
    which is the whole point: source_lang/target_lang, the version stamp and
    every threshold the user set stay put while their endpoint, model and auth
    are replaced from the overlay.

    Lists are replaced wholesale, never merged element-wise. providers.<job>
    is an array of blocks (the consensus fan-out), and splicing two arrays
    index-by-index would produce a Frankenstein of both machines' models with
    no way to tell which block came from where; an overlay naming a job as an
    ARRAY therefore takes the job over completely.

    The one deliberate exception is a providers job given as a single block
    object (the legacy shape, e.g. {"model": "x"}): that merges key-wise into
    EVERY block the project already has. Replacing the array outright here
    would drop the project's base_url and auth for the job, and the missing
    endpoint would then fall back to PROVIDER_DEFAULTS' hard-coded
    DEFAULT_BASE_URL -- silently sending the next run at a different server.
    A partial overlay must never quietly un-point a working provider."""
    merged = _deep_merge(base, overlay)
    base_providers = base.get("providers") if isinstance(base, dict) else None
    over_providers = overlay.get("providers") if isinstance(overlay, dict) else None
    if isinstance(base_providers, dict) and isinstance(over_providers, dict):
        for job, over_block in over_providers.items():
            project_blocks = base_providers.get(job)
            if not isinstance(over_block, dict) or not isinstance(project_blocks, list):
                continue
            merged["providers"][job] = [_deep_merge(block, over_block)
                                        if isinstance(block, dict) else block
                                        for block in project_blocks]
    return merged


def apply_overlay(project_dir: Path, overlay: dict) -> tuple[dict, list[str]]:
    """Merge the overlay onto the project's RAW config.json and save the
    result. Returns (changed_keys, report_lines).

    Works from the raw on-disk file rather than load_config's merged form, so
    the write is minimal: the file keeps its shape and gains only the
    overlay's keys, instead of being rewritten with every DEFAULTS value
    expanded into it. That keeps the diff reviewable and keeps `version` (which
    load_config would never fabricate, and which the overlay may not set)
    exactly as the project left it.

    Validates the merged form through load_config BEFORE writing, so a
    malformed overlay -- a providers block of the wrong shape, say -- fails
    with a clear error instead of leaving a config.json that every later
    command rejects.

    The base is shape-checked too, not just the result: this reads the file
    RAW, and a config.json that is itself corrupt (a bare number or null
    body, or a `providers` that is not an object) would otherwise reach
    _deep_merge and raise AttributeError/TypeError, which main() does not
    catch -- the CLI would die with a traceback and exit 1 instead of the
    documented exit 2. Mirrors load_config's own guards, raising the same
    ValueError shape so the caller reports it identically."""
    project_dir = Path(project_dir)
    cfg_path = project_dir / "config.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path} must contain a JSON object")
    if "providers" in raw and not isinstance(raw["providers"], dict):
        raise ValueError(f"{cfg_path}: 'providers' must be a JSON object")
    # Same guard on the overlay side: apply_overlay is a public helper, and a
    # hand-built overlay dict (not one from load_local_config) can still carry
    # a non-object `providers`. Checked here so no caller can reach the
    # AttributeError inside merge_overlay.
    if not isinstance(overlay, dict):
        raise ValueError("config.local.json overlay must be a JSON object")
    if "providers" in overlay and not isinstance(overlay["providers"], dict):
        raise ValueError("config.local.json: 'providers' must be a JSON object")
    merged = merge_overlay(raw, overlay)
    # Prove the result is loadable before it replaces a working config.json.
    # Validation only: the value written below is `merged`, not this form.
    _normalize_providers(merged.get("providers") or {})
    changed = sorted(_changed_keys(raw, merged))
    if merged != raw:
        save_config(project_dir, merged)
    lines = [f"[ok] config.local.json: applied {len(changed)} key(s): "
             + ", ".join(changed)] if changed else [
        "[ok] config.local.json: no changes (project already matches)"]
    return changed, lines


def _changed_keys(before: dict, after: dict) -> list[str]:
    """Dotted paths whose values differ between two config dicts.

    Reports the paths the overlay actually moved, so the command can name the
    settings it changed instead of making the user diff the file. A container
    that was replaced (a providers array, say) reports as its own leaf rather
    than as every index inside it."""
    keys: set[str] = set()

    def walk(prefix: str, old: object, new: object) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key in set(old) | set(new):
                walk(f"{prefix}.{key}" if prefix else str(key),
                     old.get(key), new.get(key))
            return
        if old != new:
            keys.add(prefix)

    walk("", before, after)
    return sorted(keys)


def inline_api_keys(overlay: dict) -> list[str]:
    """Dotted paths of every inline api_key in the overlay.

    An inline key is a credential written to disk in a file the skill git
    IGNORES -- but `sync-config` merges it into the project's config.json,
    which the project repo DOES commit. Callers warn about these so the user
    can move to api_key_env (or drop the key) before the secret reaches a
    project history."""
    found: list[str] = []

    def walk(prefix: str, node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{prefix}.{key}" if prefix else str(key)
                if key == "api_key" and value:
                    found.append(child)
                else:
                    walk(child, value)
        elif isinstance(node, list):
            for index, element in enumerate(node):
                walk(f"{prefix}[{index}]", element)

    walk("", overlay)
    return sorted(found)


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
