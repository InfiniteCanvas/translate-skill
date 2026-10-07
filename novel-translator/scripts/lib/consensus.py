"""Multi-model consensus: fan a task prompt out to every provider block in a
job's array, then let the consensus provider synthesize the final response.

A job with a single provider block behaves exactly as the pre-consensus
pipeline did -- one client.chat call. Every call is trace-logged by the
_trace_hook below: an llm_call summary on the tier-1 orchestration timeline
(never a body), plus llm_request/llm_response in the calling chapter's
tier-2 bucket, or the project bucket when the call is chapter-less.
A job with two or more blocks runs the same prompt through every model in
parallel; each response becomes a labeled candidate, and one extra call to
the "consensus" job's provider (always exactly one block, enforced by
config._normalize_providers) merges the candidates into the final response
under the same JSON schema the task itself uses, so every downstream
validator treats it exactly like a single-model reply.

Failure policy: individual candidate failures are tolerated with a [warn]
while any survivor remains (all failed re-raises the last error); a failed
consensus call degrades to the first surviving candidate verbatim instead
of failing the task -- the synthesis is an enhancement, never a new single
point of failure.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lib import client, config, logger, project

# Console announce state: a multi-chapter run shares one config, so the
# "N model(s)" line prints once per (job, n) per process. Resettable for
# tests, like pipeline._TOKEN_CAP_WARNED.
_ANNOUNCED: set[tuple[str, int]] = set()

# Live fan-out count. While > 0, worker threads are mid-HTTP-call and a
# Ctrl-C must hard-exit the CLI (translate.main's KeyboardInterrupt handler
# checks this): SIGINT only interrupts the main thread, and the
# interpreter's atexit join would otherwise wait out each abandoned
# worker's full retry ladder (minutes against a hung endpoint).
_ACTIVE_FANS = 0

# consensus.md: the synthesize-over-candidates prompt. Loaded through
# pipeline's helpers (project templates/ first, skill assets fallback,
# no-rescan fill) via a lazy import -- pipeline imports this module, so a
# top-level import would be a cycle.
_CONSENSUS_TEMPLATE = "consensus.md"


def _model_label(block: dict) -> str:
    """Display label for a provider block: its explicit model id, else the
    endpoint's /models default (best effort -- an unreachable endpoint
    degrades to a placeholder instead of raising; the label is cosmetic)."""
    model = block.get("model")
    if model:
        return str(model)
    try:
        return client.resolve_model(str(block.get("base_url", "")),
                                    headers=client.auth_headers(block))
    except Exception:  # noqa: BLE001 - label only, never fails the task
        return "(unresolved model)"


def _trace_hook(project_dir: Path, job: str, extra: dict | None = None,
                chapter: str | None = None):
    """meta_hook that trace-logs with a job tag -- the single logging owner
    for every call routed through this module.

    `chapter` is bound HERE, on the calling (main) thread, before any fan-out
    worker starts; ThreadPoolExecutor does not copy context to workers and does
    not need to, because the value is already a closure constant.

    The hook reads no config and applies no gate: log_event routes both lines
    by tier and gates them against the per-project flags it resolved itself.
    That matters twice over -- the two lines have DIFFERENT gates (llm_call is
    orchestration, llm_request/llm_response are model IO), and reading the
    caller's cfg here would create a second flag source that disagrees with
    log_event's in any sandbox without a config.json."""
    def hook(meta: dict) -> None:
        if meta.get("event") == "llm_response":
            logger.log_event(project_dir, {
                "event": "llm_call", "job": job, "chapter": chapter,
                **(extra or {}),
                "model": meta.get("model"),
                "usage": meta.get("usage"),
                "finish_reason": meta.get("finish_reason"),
                "elapsed_s": meta.get("elapsed_s"),
                "error": meta.get("error"),
            })
        logger.log_event(project_dir, {"job": job, "chapter": chapter,
                                       **(extra or {}), **meta})

    return hook


def chat(project_dir: Path, cfg: dict, job: str, prompt: str,
         json_schema: dict | None = None,
         max_tokens: int | None = None,
         chapter: str | None = None) -> str:
    """client.chat for a job, with multi-model fan-out and consensus merging.

    `chapter` is the calling chapter's file stem, or None for a chapter-less
    job (profile, the review passes); it decides which tier-2 bucket the model
    exchange lands in and is threaded explicitly so the logger never has to
    infer it.

    Single-block job: one call, meta tagged {"job": job} -- byte-identical
    trace lines to the old pipeline._chat. Multi-block job: one parallel
    call per block (meta tagged {"job": job, "candidate": i, "candidates":
    n}, i 1-based), then one consensus-provider call over the original
    prompt plus all surviving candidates (meta tagged {"job": "consensus",
    "consensus_for": job}). The consensus call reuses the task's json_schema
    and never caps below either the task's explicit max_tokens or the
    consensus block's own max_tokens -- it is the one call site that passes
    enforce_ceiling=False to client.chat, so the ceiling clamp that bounds
    every other call cannot silently shrink a merge below what the surviving
    candidates need.

    The fan-out candidates themselves ARE clamped by client.chat to their own
    block's max_tokens, so a task cap above a block's limit can never raise
    that block past what its provider accepts.
    """
    blocks = config.provider_list(cfg, job)
    if len(blocks) == 1:
        return client.chat(blocks[0], prompt, json_schema=json_schema,
                           max_tokens=max_tokens,
                           meta_hook=_trace_hook(project_dir, job,
                                                 chapter=chapter))

    n = len(blocks)
    if (job, n) not in _ANNOUNCED:
        _ANNOUNCED.add((job, n))
        print(f"[consensus] {job}: {n} model(s) - merging results "
              "via the consensus provider")

    # Fan out: one worker thread per block (wall-clock ~= the slowest
    # model, not the sum). Outcomes are collected in block order so the
    # per-candidate warnings print deterministically; only the main thread
    # prints. A Ctrl-C cancels pending calls and abandons in-flight ones
    # (an HTTP request in flight cannot be interrupted); _ACTIVE_FANS
    # staying elevated tells the CLI to hard-exit rather than let the
    # interpreter's atexit join wait out the abandoned workers.
    global _ACTIVE_FANS
    _ACTIVE_FANS += 1
    outcomes: list[str | None] = [None] * n
    errors: list[Exception | None] = [None] * n
    pool = ThreadPoolExecutor(max_workers=n)
    try:
        futures = [
            pool.submit(client.chat, block, prompt,
                        json_schema=json_schema, max_tokens=max_tokens,
                        meta_hook=_trace_hook(project_dir, job,
                                              {"candidate": i, "candidates": n},
                                              chapter))
            for i, block in enumerate(blocks, start=1)
        ]
        for i, future in enumerate(futures):
            try:
                outcomes[i] = future.result()
            except Exception as exc:  # noqa: BLE001 - per-candidate tolerance
                errors[i] = exc
    except KeyboardInterrupt:
        # _ACTIVE_FANS deliberately stays elevated (no decrement): the
        # workers are being abandoned, and translate.main hard-exits the
        # process exactly because this count is nonzero.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    _ACTIVE_FANS -= 1

    survivors = [(i, outcomes[i - 1]) for i in range(1, n + 1)
                 if outcomes[i - 1] is not None]
    for i in range(1, n + 1):
        if errors[i - 1] is not None:
            print(f"[warn] consensus: {job} candidate {i}/{n} "
                  f"({_model_label(blocks[i - 1])}) failed: {errors[i - 1]} "
                  "- continuing with the remaining candidates")
    if not survivors:
        raise errors[-1]
    if len(survivors) == 1:
        logger.log_event(project_dir, {
            "event": "degraded", "chapter": chapter, "where": "consensus",
            "reason": f"{job}: only one candidate survived; used verbatim "
                      "without a consensus call",
        })
        print(f"[warn] consensus: {job}: only one candidate survived - "
              "using it without a consensus call")
        return survivors[0][1]

    # Synthesize: the consensus provider judges the candidates against the
    # original task prompt (which carries the output contract) and writes
    # the single final response in exactly that format.
    from lib import pipeline  # lazy: pipeline imports this module at top level
    cblock = config.provider(cfg, "consensus")
    sections = [
        f"### Candidate {i} (model: {_model_label(blocks[i - 1])})\n\n{text}"
        for i, text in survivors
    ]
    template = pipeline._load_template(
        project.paths(project_dir)["templates"], _CONSENSUS_TEMPLATE)
    c_prompt = pipeline.fill(
        template,
        {"task_prompt": prompt,
         "candidates_section": "\n\n".join(sections)},
        _CONSENSUS_TEMPLATE)
    # Never below the task's explicit cap (the synthesis must fit what the
    # task's own contract allows) nor the consensus block's declared cap
    # (a deliberately raised/lowered consensus max_tokens must win).
    # enforce_ceiling=False: this call is the ONE place allowed to exceed its
    # block's own max_tokens. c_max is already max(task, block), so clamping
    # it to the consensus block's declared cap would cap a full-chapter merge
    # at the arbitrator's own budget -- which is routinely smaller than the
    # candidates it has to merge (see config.local.example.mixed.json:
    # translator 256000, consensus 65536).
    c_max = max(max_tokens or 0, int(cblock.get("max_tokens") or 0)) or None
    try:
        return client.chat(
            cblock, c_prompt, json_schema=json_schema, max_tokens=c_max,
            enforce_ceiling=False,
            meta_hook=_trace_hook(project_dir, "consensus",
                                  {"consensus_for": job}, chapter))
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the task
        first = survivors[0][0]
        logger.log_event(project_dir, {
            "event": "degraded", "chapter": chapter, "where": "consensus",
            "reason": f"{job}: consensus call failed ({exc}); used candidate "
                      f"{first} without merging",
        })
        print(f"[warn] consensus: {job}: consensus call failed ({exc}) - "
              f"using candidate {first} without merging")
        return survivors[0][1]
