"""Tests for the v012 output-cap ceiling: a caller-supplied `max_tokens` is a
CEILING the pipeline may lower but never raise above a provider block's own
`max_tokens`.

The defect this closes: `client.chat` used to send `max_tokens or
provider_cfg["max_tokens"]`, so `translate_max_output_tokens` silently
OVERRODE every translator block. A project whose ceiling sat above the
provider's real limit packed chapters to 256k and sent them to a model that
returns 128k -- silent truncation, and a corrective retry that re-sent at the
same oversized cap.

What is pinned here:

  * the clamp is per block -- a 256000 task cap reaches a 256000 block intact
    and is lowered to 128000 for a 128000 block, in the same fan-out;
  * provider `max_tokens` is NOT schema-validated (references/file-formats.md),
    so null / 0 / non-int must not raise -- the repo-wide `or DEFAULT` idiom;
  * `extra_body` still merges AFTER the clamp and can still override, which is
    the documented escape hatch;
  * the consensus synthesis is the ONE call site allowed past the clamp, and it
    is exercised through the REAL client.chat (test_consensus.py's FakeChat
    replaces client.chat outright, so it cannot see the clamp at all);
  * a translator block that OMITS max_tokens still contributes
    DEFAULT_MAX_TOKENS to the packing minimum -- an omission means "unset",
    never "unlimited";
  * a translator block below pipeline.MIN_TRANSLATOR_MAX_TOKENS cannot be
    packed into and says so;
  * `max_tokens_limit` (v013) -- a block's provider HARD ceiling -- bounds every
    call including the synthesis, survives the enforce_ceiling opt-out, is a
    no-op when set above the block, and reaches the packing budget so chapters
    cannot be sized for a response the provider will truncate.

The defect the limit closes, in the shape it actually occurs: a consensus block
declaring `max_tokens: 128000` under a 256000 task cap was sent 256000 and came
back `HTTP 400 code 1210 [1,131072]` from Z.AI, after which the merge degraded
to candidate 1. `max_tokens` cannot express "this block may be
under-provisioned on purpose" and "the provider refuses above 128000" at once,
which is why the second key exists rather than a tighter clamp. Cases i/j
reproduce both the failure and the fix through the real client.

Hermetic: client.requests is swapped for a fake whose post() records the JSON
body and returns a canned 200. No network, restore in finally.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_token_cap_ceiling.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml"]
# ///
from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import requests

# lib/ lives at novel-translator/scripts relative to this file (CWD-independent)
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import client, config, consensus, logger, pipeline  # noqa: E402

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


class FakeResponse:
    status_code = 200
    text = '{"choices": [{"message": {"content": "ok"}}]}'

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class PostLog:
    """client.requests stand-in: post() records each JSON body and answers
    a canned 200, so the test reads the cap that would actually go out."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(json)
        return FakeResponse()


@contextmanager
def patched_post():
    log = PostLog()
    orig = client.requests
    client.requests = SimpleNamespace(post=log.post,
                                      RequestException=requests.RequestException)
    try:
        yield log
    finally:
        client.requests = orig


def block(max_tokens=128000, model="glm-5.3", extra=None,
          limit=None) -> dict:
    b = {"base_url": "http://fake:1/v1", "model": model,
         "temperature": 0.7, "max_tokens": max_tokens, "thinking": False}
    if extra:
        b["extra_body"] = extra
    if limit is not None:
        b["max_tokens_limit"] = limit
    return b


def sent(log: PostLog) -> list:
    return [b["max_tokens"] for b in log.bodies]


# --- the clamp ---------------------------------------------------------------

def case_a_clamp_directions() -> None:
    """The cap may be lowered by the caller, never raised above the block."""
    with patched_post() as log:
        client.chat(block(128000), "hi", max_tokens=256000)
    check("a1 clamp: a caller cap above the block is lowered to the block",
          sent(log) == [128000], f"sent={sent(log)}")

    with patched_post() as log:
        client.chat(block(256000), "hi", max_tokens=128000)
    check("a2 clamp: a caller cap below the block is honored verbatim",
          sent(log) == [128000], f"sent={sent(log)}")

    with patched_post() as log:
        client.chat(block(256000), "hi")
    check("a3 clamp: no caller cap -> the block's own value",
          sent(log) == [256000], f"sent={sent(log)}")

    with patched_post() as log:
        client.chat(block(131072, model="m1"), "hi", max_tokens=256000)
        client.chat(block(262144, model="m2"), "hi", max_tokens=256000)
    # One caller cap, two different blocks, two different results: m1 sits
    # BELOW the cap and is lowered to its own limit; m2 sits above it and keeps
    # the caller's ceiling. Neither borrows the other's number.
    check("a4 clamp: per block inside one fan-out, not a shared scalar",
          sent(log) == [131072, 256000], f"sent={sent(log)}")


def case_b_unvalidated_block_values() -> None:
    """Provider max_tokens is NOT schema-validated, so null / 0 / string must
    fall back to the default rather than raise. `min(int, None)` is the bug
    this pins."""
    for label, value in (("null", None), ("zero", 0), ("string", "128000")):
        with patched_post() as log:
            try:
                client.chat(block(value), "hi", max_tokens=4096)
                got = sent(log)
                err = ""
            except Exception as exc:  # noqa: BLE001 - the failure IS the check
                got, err = [], f"{type(exc).__name__}: {exc}"
        # The block's own value falls back to DEFAULT_MAX_TOKENS, and the
        # caller's 4096 then applies on top. The point is that it does NOT
        # raise: a bare min(int, None) would.
        check(f"b1 unvalidated max_tokens ({label}) falls back, never raises",
              got == [4096] and not err, f"sent={got} err={err}")

    with patched_post() as log:
        client.chat(block(None), "hi")
        client.chat(block(0), "hi")
    check("b2 unvalidated max_tokens with no caller cap -> default",
          sent(log) == [config.DEFAULT_MAX_TOKENS] * 2, f"sent={sent(log)}")


def case_c_extra_body_still_wins() -> None:
    """extra_body merges AFTER the clamp, so it remains the escape hatch and
    can still raise past the clamped value -- documented, not accidental."""
    with patched_post() as log:
        client.chat(block(128000, extra={"max_completion_tokens": 65536}),
                    "hi", max_tokens=256000)
    body = log.bodies[0]
    check("c1 extra_body: merges after the clamp (documented escape hatch)",
          body["max_tokens"] == 128000
          and body["max_completion_tokens"] == 65536,
          f"body={ {k: v for k, v in body.items() if 'token' in k} }")

    with patched_post() as log:
        client.chat(block(128000, extra={"max_tokens": 4096}), "hi",
                    max_tokens=256000)
    check("c2 extra_body: may still override the clamp explicitly",
          sent(log) == [4096], f"sent={sent(log)}")


def case_d_enforce_ceiling_opt_out() -> None:
    """enforce_ceiling=False is the single documented bypass."""
    with patched_post() as log:
        client.chat(block(512), "hi", max_tokens=4096, enforce_ceiling=False)
    check("d1 opt-out: enforce_ceiling=False sends the caller cap verbatim",
          sent(log) == [4096], f"sent={sent(log)}")
    with patched_post() as log:
        client.chat(block(512), "hi", max_tokens=4096)
    check("d2 opt-out: default is clamped, so the flag is what changed it",
          sent(log) == [512], f"sent={sent(log)}")


# --- the consensus carve-out, through the REAL client.chat ------------------

def case_e_consensus_synthesis_is_not_clamped() -> None:
    """The synthesis call is the ONE place allowed past the clamp.

    config.local.example.mixed.json runs the translator at 256000 with a
    consensus block at 65536. Clamping the synthesis would merge two
    full-chapter candidates under a third of the budget they need. This runs
    the real client.chat (not a fake) so the clamp is actually exercised.
    """
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        (proj / "templates").mkdir(parents=True)
        cfg = {"providers": {"translator": [block(256000, "m1"),
                                            block(256000, "m2")],
                             "consensus": [block(65536, "arbiter")]},
               "log_llm": True}
        consensus._ANNOUNCED.clear()
        logger._run_path = None
        with patched_post() as log:
            consensus.chat(proj, cfg, "translator", "translate these lines",
                           json_schema=None, max_tokens=256000)
        # 2 fan-out candidates + 1 synthesis.
        check("e1 consensus: synthesis is not clamped to the arbitrator's cap",
              sent(log)[-1] == 256000, f"sent={sent(log)}")
        check("e2 consensus: candidates keep the task ceiling",
              sent(log)[:2] == [256000, 256000], f"sent={sent(log)}")

        # And the same shape where the arbitrator is the tightest block: the
        # candidates must be lowered per block, the synthesis must not.
        cfg["providers"]["translator"] = [block(131072, "m1"),
                                          block(262144, "m2")]
        consensus._ANNOUNCED.clear()
        with patched_post() as log:
            consensus.chat(proj, cfg, "translator", "translate these lines",
                           json_schema=None, max_tokens=256000)
        check("e3 consensus: candidates clamped per block, synthesis exempt",
              sent(log)[:2] == [131072, 256000] and sent(log)[-1] == 256000,
              f"sent={sent(log)}")


# --- pipeline-side contracts -------------------------------------------------

def case_f_provider_max_floor() -> None:
    """A translator block that OMITS max_tokens contributes DEFAULT_MAX_TOKENS
    to the packing minimum. An omission is 'unset', never 'unlimited': one
    forgotten key must not silently resize the whole job.

    Goes through load_config: provider_list is dict-tolerant and does NOT fill
    defaults -- that happens at normalization, which is what production reads.
    """
    import json

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        proj.mkdir()
        (proj / "config.json").write_text(json.dumps({
            "providers": {"translator": [
                {"base_url": "http://fake:1/v1", "model": "m1",
                 "max_tokens": 256000},
                {"base_url": "http://fake:1/v1", "model": "m2"},
            ],
                # A two-block translator fans out, so the arbitrator must be
                # named explicitly or load_config refuses (v015). This case is
                # about max_tokens defaults, not about consensus.
                "consensus": {"base_url": "http://fake:1/v1", "model": "arb"}}
        }, indent=2) + "\n", encoding="utf-8")
        cfg = config.load_config(proj)
        blocks = config.provider_list(cfg, "translator")
        pmax = min(int(b.get("max_tokens") or config.DEFAULT_MAX_TOKENS)
                   for b in blocks)
        check("f1 omitted max_tokens: the block resolves to DEFAULT_MAX_TOKENS",
              blocks[1]["max_tokens"] == config.DEFAULT_MAX_TOKENS,
              f"normalized={blocks[1].get('max_tokens')!r}")
    check("f2 omitted max_tokens: it lowers the packing minimum to the default",
              pmax == config.DEFAULT_MAX_TOKENS,
              f"provider_max={pmax} default={config.DEFAULT_MAX_TOKENS}")


def case_g_low_cap_and_reasoning_guards() -> None:
    """The two runtime guards: an unpackable block, and a reasoning block the
    ceiling squeezes below its own declared budget."""
    import io
    import contextlib as _c

    def run(blocks: list, wire_cap: int) -> str:
        buf = io.StringIO()
        with _c.redirect_stdout(buf):
            pipeline._check_translator_caps(blocks, wire_cap)
        return buf.getvalue()

    out = run([block(100, "tiny")], 256000)
    check("g1 guard: a block below MIN_TRANSLATOR_MAX_TOKENS warns",
          "[warn]" in out and str(pipeline.MIN_TRANSLATOR_MAX_TOKENS) in out,
          f"out={out!r}")

    out = run([block(256000, "reasoner",
                     extra={"reasoning_effort": "max"})], 65536)
    check("g2 guard: a reasoning block squeezed by the ceiling warns",
          "[warn]" in out and "converging" in out, f"out={out!r}")

    out = run([block(256000, "reasoner",
                     extra={"reasoning_effort": "max"})], 256000)
    check("g3 guard: no warning when the ceiling does not squeeze it",
          out.strip() == "", f"out={out!r}")

    out = run([block(256000, "plain")], 65536)
    check("g4 guard: a non-reasoning block squeezed by the ceiling is fine",
          out.strip() == "", f"out={out!r}")


def case_h_pack_cap_vs_wire_cap() -> None:
    """_pack_chunks takes the sizing budget and the sent ceiling as SEPARATE
    arguments. Passing pack_cap for both is what let a chapter be sized for a
    budget the tightest block cannot return."""
    lines = ["中" * 20] * 10          # cost 30/line
    # Sized generously, sent conservatively.
    plan = pipeline._pack_chunks(lines, 200000, 200000, 200000)
    check("h1 pack: room comes from pack_cap, not wire_cap",
          [(lo, hi) for lo, hi, _c in plan] == [(0, 10)],
          f"plan={plan}")
    check("h2 pack: the sent cap comes from wire_cap",
          all(cap == 200000 for _lo, _hi, cap in plan), f"plan={plan}")
    # Sized conservatively: must split even though wire_cap is huge.
    plan_small = pipeline._pack_chunks(lines, 500, 750, 200000)
    check("h3 pack: a small pack_cap splits even with a large wire_cap",
          len(plan_small) > 1
          and all(cap == 200000 for _lo, _hi, cap in plan_small),
          f"plan={plan_small}")


# --- the provider HARD ceiling (max_tokens_limit) ----------------------------

def case_i_max_tokens_limit() -> None:
    """`max_tokens_limit` is the provider's rejection threshold, not a budget,
    and it binds EVERY call to the block -- including the consensus synthesis,
    which is otherwise allowed past its own max_tokens.

    The defect this closes, from a real run: a consensus block declaring
    max_tokens 128000 under a 256000 task cap was sent 256000 and came back
    `HTTP 400 code 1210 [1,131072]` from Z.AI. The merge then degraded to
    candidate 1 -- a silent quality loss. max_tokens alone cannot express "the
    provider refuses above 128000" while still meaning "this block may be
    under-provisioned on purpose", so it is a second key.
    """
    # --- backward compatibility: absence means UNCHANGED, both directions ---
    with patched_post() as log:
        client.chat(block(128000), "hi", max_tokens=256000)
        client.chat(block(256000), "hi", max_tokens=128000)
        client.chat(block(256000), "hi")
    check("i1 no limit: every clamp direction is byte-identical to v012",
          sent(log) == [128000, 128000, 256000], f"sent={sent(log)}")

    with patched_post() as log:
        client.chat(block(128000, "m1"), "hi", max_tokens=256000)
        client.chat(block(256000, "m2"), "hi", max_tokens=256000)
    check("i2 no limit: per-block clamping inside one fan-out still holds",
          sent(log) == [128000, 256000], f"sent={sent(log)}")

    # --- the limit lowers the block's effective cap --------------------------
    with patched_post() as log:
        client.chat(block(256000, limit=131072), "hi", max_tokens=256000)
    check("i3 limit: a caller's cap is clamped down to the declared limit",
          sent(log) == [131072], f"sent={sent(log)}")

    with patched_post() as log:
        client.chat(block(256000, limit=131072), "hi")
    check("i4 limit: with no caller cap the limit still binds",
          sent(log) == [131072], f"sent={sent(log)}")

    # The direction a user is most likely to get wrong: writing a limit
    # believing it RAISES the block. min() does nothing here. Pinned in both
    # call shapes -- with and without the synthesis opt-out, since that flag
    # turns the block's max_tokens into a floor rather than a ceiling.
    with patched_post() as log:
        client.chat(block(65536, limit=131072), "hi", max_tokens=4096,
                    enforce_ceiling=False)
        client.chat(block(65536, limit=131072), "hi")
        client.chat(block(65536, limit=131072), "hi", max_tokens=4096)
    check("i5 limit above the block is a no-op, never a raise",
          sent(log) == [4096, 65536, 4096], f"sent={sent(log)}")

    # Under the synthesis opt-out the BLOCK is not a ceiling, so a caller cap
    # above it is legal -- but the limit still bounds it. This is the floor
    # working as designed, not a raise past the block.
    with patched_post() as log:
        client.chat(block(65536, limit=131072), "hi", max_tokens=256000,
                    enforce_ceiling=False)
    check("i6 synthesis may exceed the block but never the limit",
          sent(log) == [131072], f"sent={sent(log)}")

    # Absent / null / 0 all mean "no declared limit" (repo-wide `or` idiom).
    with patched_post() as log:
        client.chat(block(128000, limit=None), "hi", max_tokens=256000)
        b0 = block(128000)
        b0["max_tokens_limit"] = 0
        client.chat(b0, "hi", max_tokens=256000)
    check("i7 unvalidated limit: null and 0 mean unset, never raise",
          sent(log) == [128000, 128000], f"sent={sent(log)}")


def case_j_limit_bounds_the_consensus_synthesis() -> None:
    """The regression, through the REAL client.chat.

    consensus.chat raises the synthesis to max(task cap, block cap) so an
    under-provisioned arbitrator can still merge large candidates. That raise is
    a FLOOR. A declared max_tokens_limit is the one thing it may not cross --
    and this is the exact shape that produced the HTTP 400.
    """
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        (proj / "templates").mkdir(parents=True)
        logger._run_path = None

        # The failing run: ceiling 256000, consensus block 128000 on a provider
        # whose real limit is 131072. Without a declared limit: sent 256000.
        cfg = {"providers": {"translator": [block(128000, "m1"),
                                            block(256000, "m2")],
                             "consensus": [block(128000, "arbiter")]},
               "log_llm": True}
        consensus._ANNOUNCED.clear()
        with patched_post() as log:
            consensus.chat(proj, cfg, "translator", "translate these lines",
                           json_schema=None, max_tokens=256000)
        check("j1 regression shape: without a limit the synthesis overran",
              sent(log)[-1] == 256000, f"sent={sent(log)}")

        # Same shape, one key added.
        cfg["providers"]["consensus"] = [block(128000, "arbiter",
                                               limit=131072)]
        consensus._ANNOUNCED.clear()
        with patched_post() as log:
            consensus.chat(proj, cfg, "translator", "translate these lines",
                           json_schema=None, max_tokens=256000)
        check("j2 fix: the synthesis is clamped to the declared limit",
              sent(log)[-1] == 131072, f"sent={sent(log)}")

        # The fan-out candidates are bounded by it too: it is a provider limit,
        # not a synthesis rule. m1's own 128000 is the tighter of the two here,
        # so it must be UNCHANGED -- the limit must not become a second, lower
        # budget for a block that never asked for more.
        check("j3 the limit does not disturb a block already below it",
              sent(log)[:2] == [128000, 256000], f"sent={sent(log)}")

        # ...and it does bound a block that asked for more than the provider
        # allows, which is the other half of "provider limit, not budget".
        cfg["providers"]["translator"] = [block(256000, "m1",
                                                limit=131072),
                                          block(256000, "m2")]
        consensus._ANNOUNCED.clear()
        with patched_post() as log:
            consensus.chat(proj, cfg, "translator", "translate these lines",
                           json_schema=None, max_tokens=256000)
        check("j4 a translator block over its own limit is clamped too",
              sent(log)[:2] == [131072, 256000], f"sent={sent(log)}")

        # The under-provisioned arbitrator keeps its FLOOR: consensus.chat raises
        # the merge to max(task, block) so a 65536 arbitrator can still merge
        # 256000 candidates, and a limit ABOVE the task cap must not suppress
        # that. This is the config.local.example.mixed.json shape and it must
        # not regress -- j2 is the case where the limit DOES bind.
        cfg["providers"]["translator"] = [block(256000, "m1"),
                                          block(256000, "m2")]
        cfg["providers"]["consensus"] = [block(65536, "arbiter",
                                               limit=262144)]
        consensus._ANNOUNCED.clear()
        with patched_post() as log:
            consensus.chat(proj, cfg, "translator", "translate these lines",
                           json_schema=None, max_tokens=256000)
        check("j5 a limit above the task cap leaves the floor intact",
              sent(log)[-1] == 256000, f"sent={sent(log)}")

        # ...and the same arbitrator whose provider only reaches 131072 gets
        # exactly that, not the 256000 it asked for.
        cfg["providers"]["consensus"] = [block(65536, "arbiter",
                                               limit=131072)]
        consensus._ANNOUNCED.clear()
        with patched_post() as log:
            consensus.chat(proj, cfg, "translator", "translate these lines",
                           json_schema=None, max_tokens=256000)
        check("j6 the floor is then clamped to the provider's real limit",
              sent(log)[-1] == 131072, f"sent={sent(log)}")


def case_k_limit_reaches_packing() -> None:
    """The limit must reach the PACKING budget, not just the wire.

    If it did not, a block declaring max_tokens 256000 with a 131072 limit would
    have chapters sized for 256000 and every call truncated at 131072 -- the
    exact silent-truncation trap the v012 clamp exists to prevent. block_cap is
    what both readers share, so this pins the two agreeing rather than the
    formula twice.
    """
    check("k1 block_cap folds the limit into the block's effective cap",
          config.block_cap(block(256000, limit=131072)) == 131072
          and config.block_cap(block(256000)) == 256000
          and config.block_cap(block(65536, limit=131072)) == 65536,
          "block_cap disagrees with min(max_tokens, limit)")

    # What pipeline computes for the packing minimum.
    blocks = [block(256000, "m1"), block(256000, "m2", limit=131072)]
    pmax = min(config.block_cap(b) for b in blocks)
    check("k2 the packing minimum follows the limit, not the declaration",
          pmax == 131072, f"provider_max={pmax}")

    # And it must actually split: the same lines packed at 131072 vs 256000.
    # 600 lines x ~210 output tokens = 126000, which sits between the two
    # budgets (floor(0.8*256000)-256 = 204544, floor(0.8*131072)-256 = 104601)
    # so the limited pack must split where the unlimited one does not.
    lines = ["中" * 200] * 600
    big = pipeline._pack_chunks(lines, 256000, 256000, 256000)
    limited = pipeline._pack_chunks(lines, pmax, 256000, 256000)
    check("k3 a limited pack_cap splits a chapter an unlimited one does not",
          len(big) == 1 and len(limited) > 1,
          f"big={len(big)} limited={len(limited)}")

    import io
    import contextlib as _c

    def warn(bs: list, wire: int) -> str:
        buf = io.StringIO()
        with _c.redirect_stdout(buf):
            pipeline._check_translator_caps(bs, wire)
        return buf.getvalue()

    # A limit below the packing floor is the same hard error as a low
    # max_tokens: the chapter cannot be packed into it at all.
    out = warn([block(256000, "m1", limit=4096)], 256000)
    check("k4 a limit below MIN_TRANSLATOR_MAX_TOKENS warns as unpackable",
          "[warn]" in out and "cannot be packed into" in out, f"out={out!r}")

    # The squeeze warning must compare against the DECLARED budget, not the
    # limit -- otherwise a limit and the ceiling look identical to it and the
    # measured Flash-Preview failure (max effort under a small cap returns
    # nothing) goes undiagnosed. A limit BELOW the declaration is exactly that
    # undiagnosed case, so it must warn even when the ceiling does not squeeze.
    out = warn([block(256000, "reasoner", limit=65536,
                      extra={"reasoning_effort": "max"})], 256000)
    check("k5 a limit squeezing a reasoning block still warns",
          "[warn]" in out and "converging" in out, f"out={out!r}")

    # A limit AT the declaration is not a squeeze -- nothing is lost.
    out = warn([block(256000, "reasoner", limit=256000,
                      extra={"reasoning_effort": "max"})], 256000)
    check("k6 a limit at the declared cap is not a squeeze",
          out.strip() == "", f"out={out!r}")

    # The limit is about capacity, not reasoning: a plain model pinned by one
    # is fine, same as a plain model squeezed by the ceiling (case g4).
    out = warn([block(256000, "plain", limit=65536)], 256000)
    check("k7 a limit on a non-reasoning block is not a squeeze",
          out.strip() == "", f"out={out!r}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_a_clamp_directions()
    case_b_unvalidated_block_values()
    case_c_extra_body_still_wins()
    case_d_enforce_ceiling_opt_out()
    case_e_consensus_synthesis_is_not_clamped()
    case_f_provider_max_floor()
    case_g_low_cap_and_reasoning_guards()
    case_h_pack_cap_vs_wire_cap()
    case_i_max_tokens_limit()
    case_j_limit_bounds_the_consensus_synthesis()
    case_k_limit_reaches_packing()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())