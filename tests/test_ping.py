"""Tests for cmd_ping's per-job ARBITRATOR pass (v015).

cmd_ping had NO test coverage before this file: it performs live HTTP through
client.resolve_model and client.probe, and nothing in the suite swapped either.
This file builds that harness, so the one provider whose failure kills a run
AFTER the candidates were already paid for is finally checkable.

Covered:

  * back-compat -- a project that authors NO per-job arbitrator produces
    byte-identical output to before, because every job resolves to the global
    `consensus` block, which the PROVIDER_JOBS loop already probed. This is the
    check that fails if the dedupe set is built wrong (e.g. off the per-job
    `seen` dict, which is reset every iteration).
  * a `consensus_<job>` block at a SECOND endpoint is probed exactly once,
    labelled `consensus(<job>)`.
  * a `consensus_<job>` block pointing at the same endpoint as the global is
    deduplicated, not probed twice.
  * a per-job arbitrator for a SINGLE-block job is never probed: such a job
    never fans out, so it never merges.
  * a failing arbitrator prints the exact [FAIL] line and returns exit 2,
    the same path as any other unreachable provider.
  * the /models-falls-back-to-chat probe path still applies to an arbitrator.

Mechanics: client.resolve_model and client.probe are swapped by ATTRIBUTE on
the lib.client module singleton (orig/restore in try/finally, no
unittest.mock), so no network is touched. cmd_ping itself is driven through the
real parser, so the argv shape is exercised too.

Self-contained PASS/FAIL script (no pytest). Run from anywhere:

    uv run tests/test_ping.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

# scripts/ (and therefore lib/) lives at novel-translator/scripts relative to
# this file (CWD-independent).
SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import translate  # noqa: E402
from lib import client  # noqa: E402

PASSED = 0
FAILED: list[str] = []

MINE = "http://mine:9999/v1"
OTHER = "http://other:7777/v1"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))


class FakeNet:
    """Records every probe and answers resolve_model from a table of models.

    `broken` names the base_urls that answer neither /models nor chat, so the
    failure path can be driven without a socket.
    """

    def __init__(self, broken: tuple[str, ...] = (),
                 no_models: tuple[str, ...] = ()):
        self.resolved: list[str] = []
        self.probed: list[str] = []
        self.broken = set(broken)
        self.no_models = set(no_models)

    def resolve_model(self, base_url: str, headers=None) -> str:
        self.resolved.append(base_url)
        if base_url in self.broken or base_url in self.no_models:
            raise RuntimeError(f"cannot reach {base_url}")
        return f"model-at-{base_url}"

    def probe(self, provider_cfg: dict) -> None:
        base_url = str(provider_cfg.get("base_url", ""))
        self.probed.append(base_url)
        if base_url in self.broken:
            raise RuntimeError(f"cannot reach {base_url}")


@contextlib.contextmanager
def patched_net(fake: FakeNet):
    orig_resolve, orig_probe = client.resolve_model, client.probe
    client.resolve_model, client.probe = fake.resolve_model, fake.probe
    try:
        yield
    finally:
        client.resolve_model, client.probe = orig_resolve, orig_probe


def run_ping(providers: dict, net: FakeNet) -> tuple[int, str]:
    """Write a real config.json, run the real cmd_ping, return (exit, output).

    stderr is merged in because `_fail` -- the shared "one or more providers
    unreachable" summary -- writes there, not to stdout.
    """
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "config.json").write_text(json.dumps({
            "source_lang": "zh", "target_lang": "en",
            "providers": providers,
        }, indent=2) + "\n", encoding="utf-8")
        args = translate._build_parser().parse_args(
            ["ping", "--project", str(proj)])
        out, err = io.StringIO(), io.StringIO()
        with patched_net(net), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = translate.cmd_ping(args, proj)
        return code, out.getvalue() + err.getvalue()


def two_models() -> dict:
    """A translator array of two models plus one other job that also fans
    out (authored, so it does not depend on translator inheritance)."""
    return {"translator": [{"base_url": MINE, "model": "m1"},
                           {"base_url": OTHER, "model": "m2"}],
            "annotator": [{"base_url": MINE, "model": "m1"},
                          {"base_url": OTHER, "model": "m2"}],
            "consensus": [{"base_url": MINE, "model": "global-arb"}]}


def case_back_compat() -> None:
    """No per-job arbitrator: output identical to pre-v015, no extra line."""
    net = FakeNet()
    code, out = run_ping(two_models(), net)
    check("a1 back-compat: exit 0 and every provider reachable",
          code == 0 and "[ok] all providers reachable" in out,
          f"code={code} out={out!r}")
    check("a2 back-compat: NO consensus(...) line is printed (every arbitrator "
          "resolves to the global block, already probed as job 'consensus')",
          "consensus(" not in out, f"out={out!r}")
    # The global consensus block is on MINE, which translator[0] also uses, so
    # /models was asked about MINE exactly once per distinct endpoint and the
    # second arbiter line still appears as its own job.
    check("a3 back-compat: the global consensus job is still probed as a job",
          "[ok] consensus  " in out, f"out={out!r}")


def case_dedicated_arbitrator() -> None:
    """A `consensus_translator` on a THIRD endpoint is probed exactly once."""
    providers = {**two_models(),
                 "consensus_translator": [{"base_url": "http://arb:1/v1",
                                          "model": "translator-arb"}]}
    net = FakeNet()
    code, out = run_ping(providers, net)
    line = [ln for ln in out.splitlines() if ln.startswith("[ok] consensus(")]
    check("b1 dedicated: exactly one extra probe line, labelled with the job",
          len(line) == 1 and line[0].startswith(
              "[ok] consensus(translator) http://arb:1/v1"),
          f"lines={line!r}")
    check("b2 dedicated: the arbitrator's model is shown, not just the endpoint",
          "(config model: translator-arb)" in (line[0] if line else ""),
          f"lines={line!r}")
    check("b3 dedicated: only the fan-out jobs' arbitrators are probed -- the "
          "annotator has no key, so it resolves to the global already probed",
          len(line) == 1, f"lines={line!r}")
    check("b4 dedicated: exit 0", code == 0, f"code={code} out={out!r}")
    check("b5 dedicated: the third endpoint was resolved exactly once",
          net.resolved.count("http://arb:1/v1") == 1,
          f"resolved={net.resolved!r}")


def case_dedup_same_endpoint() -> None:
    """A per-job arbitrator naming the SAME endpoint/model is deduplicated."""
    providers = {**two_models(),
                 "consensus_translator": [{"base_url": MINE, "model": "global-arb"}]}
    net = FakeNet()
    code, out = run_ping(providers, net)
    check("c1 dedup: a per-job block identical to the global one adds no line",
          "consensus(" not in out, f"out={out!r}")
    check("c2 dedup: exit 0", code == 0, f"code={code} out={out!r}")


def case_single_block_job_ignored() -> None:
    """A per-job key on a job that never fans out is never probed.

    A single-block job makes exactly one client.chat call and never merges, so
    its arbitrator is unreachable code at runtime too.
    """
    providers = {**two_models(),
                 "recap": [{"base_url": MINE, "model": "m1"}],
                 "consensus_recap": [{"base_url": "http://unused:1/v1",
                                      "model": "recap-arb"}]}
    net = FakeNet()
    code, out = run_ping(providers, net)
    check("d1 single-block: consensus(recap) is not probed (recap never fans "
          "out, so its arbitrator never runs)",
          "consensus(recap)" not in out, f"out={out!r}")
    check("d2 single-block: the unused endpoint is never contacted",
          "http://unused:1/v1" not in net.resolved, f"resolved={net.resolved!r}")
    check("d3 single-block: exit 0", code == 0, f"code={code} out={out!r}")


def case_failure_path() -> None:
    """A dead arbitrator is reported and lands on the shared exit-2 path."""
    providers = {**two_models(),
                 "consensus_translator": [{"base_url": "http://dead:1/v1",
                                          "model": "translator-arb"}]}
    net = FakeNet(broken=("http://dead:1/v1",))
    code, out = run_ping(providers, net)
    check("e1 failure: one [FAIL] naming the arbitrator and the job",
          out.count("[FAIL] consensus(translator) http://dead:1/v1 ->") == 1,
          f"out={out!r}")
    check("e2 failure: the summary refuses and returns exit 2, the same as any "
          "other unreachable provider",
          code == 2 and "ping: one or more providers unreachable" in out,
          f"code={code} out={out!r}")
    check("e3 failure: /models AND chat both failed for a block that names a "
          "model, so both errors are reported",
          "/models: RuntimeError" in out and "chat: RuntimeError" in out,
          f"out={out!r}")


def case_probe_fallback() -> None:
    """An arbitrator that auth-gates /models still passes via the chat probe."""
    providers = {**two_models(),
                 "consensus_translator": [{"base_url": "http://gated:1/v1",
                                          "model": "translator-arb"}]}
    net = FakeNet(no_models=("http://gated:1/v1",))
    code, out = run_ping(providers, net)
    check("f1 fallback: /models failure is not fatal for a block that names a "
          "model; the chat probe carries it",
          "[ok] consensus(translator) http://gated:1/v1 -> translator-arb "
          "(chat ok; /models failed: RuntimeError" in out, f"out={out!r}")
    check("f2 fallback: the chat probe really was attempted",
          net.probed.count("http://gated:1/v1") == 1, f"probed={net.probed!r}")
    check("f3 fallback: exit 0", code == 0, f"code={code} out={out!r}")


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_back_compat()
    case_dedicated_arbitrator()
    case_dedup_same_endpoint()
    case_single_block_job_ignored()
    case_failure_path()
    case_probe_fallback()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())