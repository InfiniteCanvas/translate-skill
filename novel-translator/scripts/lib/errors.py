"""Shared exception base for the pipeline's fatal channel.

`PipelineError` lives here, not in `pipeline.py`, so `client.py` can build on it
without an import cycle: `pipeline` imports `client` (for every provider call),
so `client` may not import `pipeline`.

The reason it is a separate leaf module at all is one property. The pipeline's
stage guards are all written as

    except PipelineError:
        raise
    except Exception as exc:
        ... degrade, retry, or skip ...

so `PipelineError` already means "this is fatal, do not absorb me" at every
stage boundary. `client.LLMFatal` inherits from BOTH `LLMError` and
`PipelineError`, which routes a dead provider key through all six of those
guards with no edits to any of them — see `client.LLMFatal`.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Fatal pipeline error (bad spec, missing template, unfilled placeholder).

    Also the base of `client.LLMFatal`, so anything that must never be absorbed
    by a stage's broad handler inherits it and passes through untouched.
    """