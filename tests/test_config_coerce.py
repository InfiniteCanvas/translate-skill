# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "novel-translator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import config, pipeline

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


def raising(fn, *args, **kwargs):
    """fn(*args, **kwargs); the exception it raised, or None when it
    returned cleanly."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        return exc
    return None


def case_1_get_number_accepts() -> None:
    """get_number: int/float pass through unchanged, numeric strings parse
    (int-shaped as int, float-shaped as float)."""
    check("1a get_number: int passes through as int",
          config.get_number({"k": 5}, "k") == 5
          and isinstance(config.get_number({"k": 5}, "k"), int)
          and not isinstance(config.get_number({"k": 5}, "k"), bool))
    check("1b get_number: float passes through as float",
          config.get_number({"k": 0.25}, "k") == 0.25
          and isinstance(config.get_number({"k": 0.25}, "k"), float))
    check("1c get_number: int-shaped string parses as int",
          config.get_number({"k": "3"}, "k") == 3
          and isinstance(config.get_number({"k": "3"}, "k"), int))
    check("1d get_number: float-shaped string parses as float",
          config.get_number({"k": "2.5"}, "k") == 2.5
          and isinstance(config.get_number({"k": "2.5"}, "k"), float))
    check("1e get_number: negative int passes through",
          config.get_number({"k": -4}, "k") == -4)


def case_2_get_number_rejects() -> None:
    """get_number: None/bool/garbage -> the exact must-be-a-number
    ValueError text; a missing key -> the exact is-missing text."""
    exc = raising(config.get_number, {"k": None}, "k")
    check("2a get_number: None -> exact must-be-a-number text",
          isinstance(exc, ValueError)
          and str(exc) == "config key 'k' must be a number (got None)",
          f"exc={exc!r}")
    exc = raising(config.get_number, {"k": True}, "k")
    check("2b get_number: True rejected like any bool",
          isinstance(exc, ValueError)
          and str(exc) == "config key 'k' must be a number (got True)",
          f"exc={exc!r}")
    exc = raising(config.get_number, {"k": False}, "k")
    check("2c get_number: False rejected like any bool",
          isinstance(exc, ValueError)
          and str(exc) == "config key 'k' must be a number (got False)",
          f"exc={exc!r}")
    exc = raising(config.get_number, {"k": "garbage"}, "k")
    check("2d get_number: garbage string -> exact text",
          isinstance(exc, ValueError)
          and str(exc) == "config key 'k' must be a number (got 'garbage')",
          f"exc={exc!r}")
    exc = raising(config.get_number, {}, "k")
    check("2e get_number: missing key -> exact is-missing text",
          isinstance(exc, ValueError)
          and str(exc) == "config key 'k' is missing",
          f"exc={exc!r}")


def case_3_cfg_value() -> None:
    """pipeline._cfg_value: same validation as get_number but
    PipelineError, keeping the cfg -> DEFAULTS -> missing-key lookup."""
    check("3a _cfg_value: cfg value wins",
          pipeline._cfg_value({"max_attempts": 7}, "max_attempts") == 7)
    check("3b _cfg_value: DEFAULTS fallback",
          pipeline._cfg_value({}, "max_attempts")
          == config.DEFAULTS["max_attempts"])
    check("3c _cfg_value: float passes (min_term_coverage)",
          pipeline._cfg_value({"min_term_coverage": 0.5},
                              "min_term_coverage") == 0.5)
    check("3d _cfg_value: numeric string parses",
          pipeline._cfg_value({"max_attempts": "2"}, "max_attempts") == 2)
    exc = raising(pipeline._cfg_value, {"max_attempts": None}, "max_attempts")
    check("3e _cfg_value: None -> PipelineError with the must-be-a-number "
          "text",
          isinstance(exc, pipeline.PipelineError)
          and str(exc) == "config key 'max_attempts' must be a number "
                          "(got None)",
          f"exc={exc!r}")
    exc = raising(pipeline._cfg_value, {"max_attempts": "many"},
                  "max_attempts")
    check("3f _cfg_value: garbage string -> PipelineError",
          isinstance(exc, pipeline.PipelineError)
          and str(exc) == "config key 'max_attempts' must be a number "
                          "(got 'many')",
          f"exc={exc!r}")
    exc = raising(pipeline._cfg_value, {}, "no_such_key")
    check("3g _cfg_value: missing everywhere -> missing-key PipelineError",
          isinstance(exc, pipeline.PipelineError)
          and str(exc) == "missing config key: no_such_key",
          f"exc={exc!r}")


def case_4_cfg_bool_divergence() -> None:
    """The bool-only keys read through _cfg_value (tn_recheck reads
    tn_keep_low_confidence that way), so unlike get_number a bool passes --
    cross-checked against get_number rejecting the same value."""
    check("4a _cfg_value: bool-key default passes through",
          pipeline._cfg_value({}, "auto_build_epub")
          is config.DEFAULTS["auto_build_epub"])
    check("4b _cfg_value: False passes",
          pipeline._cfg_value({"tn_keep_low_confidence": False},
                              "tn_keep_low_confidence") is False)
    check("4c divergence: get_number rejects the bool _cfg_value accepts",
          isinstance(raising(config.get_number, {"k": True}, "k"),
                     ValueError)
          and pipeline._cfg_value({"k": True}, "k") is True)


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    case_1_get_number_accepts()
    case_2_get_number_rejects()
    case_3_cfg_value()
    case_4_cfg_bool_divergence()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
