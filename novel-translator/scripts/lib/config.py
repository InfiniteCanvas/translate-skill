"""Configuration loading/saving and per-job LLM provider settings."""

import json
from pathlib import Path

PROVIDER_JOBS = ("translator", "glossary", "reviewer", "annotator", "recap", "profile", "consensus")

CONSENSUS_PREFIX = "consensus_"
CONSENSUS_JOBS = tuple(j for j in PROVIDER_JOBS if j != "consensus")

DEFAULTS: dict = {
    "seed_min_count": 3,
    "min_term_occurrences": 3,
    "min_term_coverage": 0.25,
    "fuzzy_max_distance": 2,
    "tn_gap_chapters": 10,
    "tn_keep_low_confidence": False,
    "max_attempts": 3,
    "contextual_glossary_cap": 200,
    "max_new_terms_per_chapter": 15,
    "max_notes_per_chapter": 10,
    "translate_max_output_tokens": 65536,
    "style_sample_chapters": 4,
    "style_sample_chars": 12000,
    "log_orchestration": True,
    "log_llm": True,
    "log_prompt_bodies": True,
    "log_llm_keep_runs": 10,
    "log_chapter_keep_runs": 3,
    "auto_build_epub": True,
    "glossary_auto_cleanup": True,
    "git_commits": True,
    "review_batch_size": 40,
    "review_report_path": "review-report.md",
}

DEFAULT_BASE_URL = "http://100.85.218.125:8888/v1"
DEFAULT_MAX_TOKENS = 65536

LOCAL_CONFIG_NAME = "config.local.json"

LOCAL_CONFIG_FORBIDDEN = ("version",)

PROVIDER_DEFAULTS: dict[str, dict] = {
    "translator": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.7, "top_p": 1.0, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "glossary": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "reviewer": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.0, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "annotator": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "recap": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.2, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
    "profile": {"base_url": DEFAULT_BASE_URL, "model": None, "temperature": 0.3, "max_tokens": DEFAULT_MAX_TOKENS, "thinking": False},
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


def _normalize_per_job_consensus(providers: dict, normalized: dict) -> None:
    """Validate and shape-normalize every authored `consensus_<job>` key into
    `normalized`, WITHOUT pre-filling its keys.

    Shape (dict -> one-element list, exactly one block) is normalized here for a
    reason beyond hygiene: the run_start event snapshots `provider_jobs` by
    filtering `isinstance(blocks, list)` (translate.py), so a bare-dict
    `consensus_translator` would silently drop out of the run ledger -- and the
    run ledger is where an operator reads which model arbitrated the translator.

    The keys are deliberately NOT filled. consensus_provider() layers the
    defaults at READ time instead, because the test suite -- and any in-process
    caller -- hands consensus.chat a hand-built cfg that never passed through
    load_config. Resolving at read time keeps one cfg resolving identically
    however it was built; pre-filling here would make the two worlds disagree.

    Mutates `normalized`; returns nothing.
    """
    for key in providers:
        if not key.startswith(CONSENSUS_PREFIX):
            continue
        job = key[len(CONSENSUS_PREFIX):]
        if job not in CONSENSUS_JOBS:
            raise ValueError(
                f"providers.{key} names no job: expected "
                f"{CONSENSUS_PREFIX}<job> where <job> is one of "
                f"{', '.join(CONSENSUS_JOBS)}")
        blocks = _provider_blocks(providers, key)
        if blocks is None:
            continue
        if len(blocks) > 1:
            raise ValueError(
                f"providers.{key} must list exactly one model (got {len(blocks)})")
        normalized[key] = blocks


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
    An authored `consensus_<job>` key (a per-job arbitrator -- see
    CONSENSUS_PREFIX) is shape-normalized by _normalize_per_job_consensus and
    left otherwise alone: it inherits nothing here, so an ABSENT key resolves
    to the global arbitrator at read time via consensus_provider. Unknown extra
    jobs pass through untouched.
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
                    authored = [translator[0]] if translator else None
                else:
                    authored = translator
        if authored is None:
            normalized[job] = [_with_defaults(job, {})]
            continue
        if job == "consensus" and len(authored) > 1:
            raise ValueError(
                f"providers.consensus must list exactly one model (got {len(authored)})")
        normalized[job] = [_with_defaults(job, element) for element in authored]
    _require_consensus_when_fanning_out(providers, normalized)
    _normalize_per_job_consensus(providers, normalized)
    for key, value in providers.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def _require_consensus_when_fanning_out(providers: dict,
                                        normalized: dict) -> None:
    """Refuse a config that fans out without authoring `providers.consensus`.

    A multi-block job merges its candidates through ONE arbitrator call, so the
    job that decides the final translation has to be chosen, not inferred. When
    `consensus` is unauthored the fallback is derived from
    `providers.translator[0]` (see the branch above), which quietly sends every
    merge -- to translator[0]'s endpoint, with its model, temperature,
    max_tokens, max_tokens_limit and extra_body -- and, in a translator array
    of two or more, that is a model judging the very candidates it is being
    handed. Inference must not decide where a paid merge call goes.

    Scoped to fan-out only. With every job at a single block nothing merges, so
    the fallback is never reached and the config loads unchanged -- a `consensus`
    key is required exactly when one is needed, and not before.

    Authorship is read from the RAW `providers`, never from `normalized`: the
    loop above materializes `consensus` for every config, so the normalized dict
    cannot distinguish "you wrote this" from "I filled this in for you". The
    fan-out set IS read from `normalized`, because omitted jobs inherit the
    translator's whole array -- a two-block translator means all six jobs fan
    out, and the message must name all six.

    Mutates nothing; raises ValueError, which load_config already surfaces as
    `[FAIL] cannot read config.json: ...` (exit 2) and which apply_overlay
    already validates before writing anything -- so this both refuses the run
    and leaves the no-lockout repair path open: an overlay that ADDS `consensus`
    merges into a valid config and applies cleanly.
    """
    if "consensus" in providers:
        return
    fanned = sorted(job for job in CONSENSUS_JOBS
                    if len(normalized.get(job) or ()) > 1)
    if not fanned:
        return
    raise ValueError(
        f"providers.consensus is required because {len(fanned)} job(s) fan out "
        f"to more than one model ({', '.join(fanned)}) and no consensus "
        f"provider is set. Add providers.consensus explicitly, or give those "
        f"jobs a single provider block.")


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
    if not isinstance(overlay, dict):
        raise ValueError("config.local.json overlay must be a JSON object")
    if "providers" in overlay and not isinstance(overlay["providers"], dict):
        raise ValueError("config.local.json: 'providers' must be a JSON object")
    merged = merge_overlay(raw, overlay)
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


def consensus_provider(cfg: dict, job: str) -> dict:
    """The single arbitrator that merges `job`'s fan-out candidates.

    Three layers, bottom-up, each filling only the keys the layer above left
    unset -- the same per-key semantics as _with_defaults:

        PROVIDER_DEFAULTS["consensus"]
        <- the resolved providers.consensus block (the global arbitrator)
        <- the authored providers.consensus_<job> block

    When `consensus_<job>` is absent the global block is returned UNCHANGED --
    the same dict object, not a copy -- which is what makes a project that
    authors no per-job arbitrator behave exactly as it did before this existed
    (tests/test_consensus.py case (b2) asserts that identity).

    Why the layer is the RESOLVED global and not PROVIDER_DEFAULTS: a user who
    writes {"model": "big-arbiter"} and nothing else must keep their working
    base_url and auth. Filling from the defaults instead would silently re-point
    the merge at the hard-coded DEFAULT_BASE_URL -- the exact failure
    merge_overlay exists to prevent ("a partial overlay must never quietly
    un-point a working provider").

    Resolved at READ time rather than in _normalize_providers, because the test
    suite and any in-process caller hand consensus.chat a hand-built cfg that
    never passed through load_config; resolving here keeps one cfg resolving
    identically however it was built. That is also why the normalizer stores
    `consensus_<job>` WITHOUT pre-filled keys.

    The optional key is read through _provider_blocks rather than
    provider_list so a malformed value raises the same contractual ValueError
    the other provider keys raise, with this key's own name in the message,
    instead of failing later as an IndexError. A cfg with no `consensus` key at
    all raises KeyError from provider() exactly as before: load_config
    guarantees the key, so that is reachable only from a hand-built cfg, where
    falling back to the defaults would aim the merge at the hard-coded
    DEFAULT_BASE_URL instead of failing loudly.
    """
    blocks = _provider_blocks(cfg["providers"], CONSENSUS_PREFIX + job)
    if not blocks:
        return provider(cfg, "consensus")
    merged = dict(PROVIDER_DEFAULTS["consensus"])
    merged.update(provider(cfg, "consensus"))
    merged.update(blocks[0])
    return merged


def block_cap(block: dict) -> int:
    """The largest output this provider block can actually return.

    `max_tokens` is what the block ASKS for; `max_tokens_limit` -- when
    authored -- is what its provider will ACCEPT. The effective cap is the
    smaller, and it is the single source of truth for every reader: what
    client.chat sends (including the consensus synthesis, whose floor is set
    elsewhere), and what pipeline packs a chapter into.

    The two keys exist because `max_tokens` alone cannot express both. A
    consensus block set to 65536 may be deliberately under-provisioned to
    merge much larger candidates (consensus.chat raises it with max()), or it
    may be the provider's real rejection threshold (Z.AI refuses anything
    above 131072). Same number, opposite intent, and only the second may not
    be exceeded. Absent the limit this is exactly `max_tokens`, so no existing
    project changes.

    `max_tokens_limit` is NOT schema-validated -- same as `max_tokens`, per
    references/file-formats.md -- so a non-numeric value raises the same
    ValueError from int() that a non-numeric `max_tokens` does. Absence, null
    and 0 all mean "no declared limit" via the repo-wide `or` idiom.
    """
    cap = int(block.get("max_tokens") or DEFAULT_MAX_TOKENS)
    limit = block.get("max_tokens_limit")
    return min(cap, int(limit)) if limit else cap


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
