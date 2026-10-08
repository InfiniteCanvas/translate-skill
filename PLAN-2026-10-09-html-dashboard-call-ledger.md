# Plan — call ledger: every log, parsed, embedded, latest run per chapter

**Date:** 2026-10-09
**Status:** ✅ **IMPLEMENTED.** Shipped in `scripts/lib/logdashboard.py`,
`scripts/translate.py`, `tests/test_log_dashboard.py` and the four doc mirrors.
49/49 suites, 2243 checks green. A "what actually shipped" section is at the
end, including four defects the build surfaced that the plan had not
anticipated.
**Author scope:** `D:\Repos\vibed\translate-skill` @ working tree.

## 1. Goal

The current `logs/report.html` fails the maintainer on four counts, all
observed in the generated page rather than theorised:

1. **Unreadable.** Every tier-2 `response` is a **JSON string inside JSON**. The
   page prints it raw, so a reviewer verdict renders as
   `{"verdict":"SUCCESS","reasons":[]}` on one line and a 70-line chapter
   translation renders as one wall of `{"i":1,"t":"..."}`.
2. **Incomplete.** A 200 KB cap dropped ~64% of the bodies. The maintainer's
   *"I only saw some of the logs"* is that cap.
3. **One-sided.** Only `response` is shown. The `prompt` that produced it — with
   `call_id`, `url`, `params`, `candidate`, timings — is never surfaced.
4. **Not human.** Bodies are pre-rendered into one giant HTML blob; every
   expand re-serialises megabytes, which is why the cap existed.

This replaces "Model exchanges" with a **Call ledger**: one row per model call,
every call, bodies embedded, JSON parsed into shape-specific views, rendered on
click.

## 2. Evidence — measured on `example_project/`

Reproduce with `python probe-artifacts/remeasure.py` →
`probe-artifacts/measure-report.txt`.

### 2.1 The call record is pairable

```
CHAPTER_0003 newest file:  8 llm_request, 8 llm_response, 4 result
All 8 call_ids match exactly (request↔response), 0 orphans.
Across 11 buckets: 117 requests, 116 responses.
```

`call_id` is the join key and it is lossless. The one request without a
response is an in-flight or killed call — a real state, kept and marked.

### 2.2 Responses are JSON — but 4% are **fenced**

```
111  plain JSON
  5  ```json fenced code blocks      <-- json.loads() FAILS on these
  0  genuinely non-JSON prose
```

A parser that only tries `json.loads(body)` degrades to raw text on 5 of 116
calls, including a whole glossary term set. **Fence-stripping is required.**

### 2.3 Eight schemas exist — and a **job name does not determine which ran**

The first draft treated a five-shape table as the contract. It is only what the
*sample* happens to contain. There are **8 schemas in the codebase**, and the
maintainer's correction is the important part — this is not just a consensus
problem:

| Schema (source) | Shape | job | task |
|---|---|---|---|
| `pipeline.py:60` | `{title, lines[{i,t}]}` | translator | translate |
| `TERMS_SCHEMA` `pipeline.py:74` | `{terms[…]}` | glossary | expand |
| `MERGE_SCHEMA` `pipeline.py:136` | `{source, translation, definition, category}` — **flat, not a list** | glossary | **merge** |
| `CLEANUP_SCHEMA` `pipeline.py:151` | `{decisions[{source, keep, reason}]}` | glossary | **cleanup** |
| `VERDICT_SCHEMA` `pipeline.py:97` | `{verdict, reasons[]}` | reviewer | faithfulness |
| `NOTES_SCHEMA` `pipeline.py:107` | `{notes[…]}` | annotator | tn_generate |
| `RECAP_SCHEMA` `story.py:39` | `{recap}` | recap | recap |
| `PROFILE_SCHEMA` `profile.py:13` | `{style_summary, background}` | profile | profile |

**`glossary` alone drives three different schemas** (expand → `terms`, merge →
flat object, cleanup → `decisions`). So the maintainer's point generalises past
consensus: *any* job name can run more than one schema. And `consensus_for` is
set to the *job name* (`consensus.py:268`), so `consensus_for: "glossary"` cannot
tell you whether it arbitrated a merge, an expansion or a cleanup — while
`consensus_for: "annotator"` does not distinguish the pipeline's `TN_GENERATE`
from a `tn` re-check. **Job and `consensus_for` are display labels, never a
parsing key.**

Three consequences, all of which the first draft got wrong:

1. Dispatch must match the **parsed key set** against the registry in §4.3, not
   the alphabetically-first key. `MERGE_SCHEMA`'s first sorted key is
   `category`, which is meaningless.
2. `MERGE_SCHEMA` is a **flat object**, so the "array → table" renderer that
   covers the other seven does not apply to it.
3. `PROFILE_SCHEMA` never appears in this sample — `profile` is chapter-less, so
   its calls are written to the orchestration tier, not a chapter bucket. A
   shape the dashboard has never seen must still render.

So the shape list is a **set of fast paths, not an enumeration**. §4.3 makes the
generic renderer the default and every named shape an optimisation over it.

### 2.3a The fan-out width is config-driven — never assume 2

**Corrected after maintainer input.** An earlier draft of this plan wrote
"model A beside model B" and hardcoded two candidates, because the sample
happens to fan out to 2. `providers.<job>` is an **array of provider blocks** and
`config.provider_list(cfg, job)` iterates one model per block, so the width is
whatever the project configured — 1, 2, 3, 5+. `providers.consensus` is
constrained to **exactly one** model (`config.py:229-231`).

Worse for any positional assumption: **candidate index is not a model
identity.** Each job carries its own array, and the sample has three different
ones:

| job | candidate 1 | candidate 2 | consensus runs? |
|---|---|---|---|
| translator | glm-5.3 | MiniMax-M3.1-Flash-Preview | yes |
| reviewer | MiniMax-M3.1-Flash-Preview | glm-5.3 (**reversed**) | yes |
| annotator | glm-5.3-flash | MiniMax-M3.1-Flash-Preview | yes |
| glossary | — single call — | | no |
| recap | — single call — | | no |

So `candidate == 1` means three different models depending on the job, and
**reviewer's order is the reverse of translator's**. Every model name rendered
anywhere in the ledger must come from that call's own `model` field, never from
a positional or per-job default.

`consensus` runs for `reviewer`, `annotator` and `translator` in this sample —
verified: every consensus prompt embeds exactly N `### Candidate k (model: X)`
blocks matching its job's declared `candidates` count. `glossary` and `recap`
are single-shot with no consensus.

### 2.4 Prompts are 6× the size of responses

```
prompt bytes    2.59 MB      (UTF-8 encoded, newest run per chapter)
params bytes    0.014 MB
response bytes  0.42 MB
prompt/response 6.20x
newest files    3.21 MB      (all retained runs: 3.82 MB)
```

### 2.5 Embedding everything costs ~36 ms

```
collect(io=False)  metadata            36.7 ms
collect(io=True)   capped today        74.1 ms
uncapped body scan (same parser)       31.1 ms
json.dumps equivalent (2.4 MB)          4.7 ms
'<' -> < replace                       0.6 ms
render(metadata page)                   0.8 ms
                              ----------------------------
uncapped end-to-end                   ~36 ms
```

**One CHAPTER_0003 run took 981.19 s.** The full-embed render is **0.004%** of a
single chapter. `--html-io` bought nothing but a confusing second mode — it is
deleted.

### 2.6 Two independent bugs hide "I only saw some of the logs"

**(a) The cap silently drops the OLDEST logs.** `_collect_io` walks chapters
and files in **ascending** order, accumulating into a 200 KB budget. Once the
budget is gone, everything later is discarded. Measured: **84 of 131** bodies
dropped (64%), and the 47 survivors span 5 different `run_id`s. Old runs eat the
budget first, so the maintainer loses recent chapters and keeps ancient ones.

**(b) The page mixes runs.** `_calls_from_tier2` reads `files[-1]`;
`_collect_io` walks every file. The index table describes one run while the
exchange list shows three. Measured: 116 responses in newest files vs 135 across
all retained.

These are separate defects with separate fixes. (a) is what removed data; (b) is
what made the remainder untrustworthy.

## 3. Decisions locked

| Fork | Decision | Basis |
|---|---|---|
| Blob vs `fetch()` | **Embed in the HTML** | Owner call; §2.5 |
| Which runs | **Latest run per chapter** | Owner call; §2.6b |
| Body cap | **None. `IO_BYTE_CAP` deleted** | §2.5 |
| `--html-io` | **Flag deleted; bodies always embedded** | §2.5. Kills the §2.6a failure mode outright |
| Fenced JSON | **Strip fences, then parse** | §2.2 |
| Prompt parsing | **One prompt only: the translator's source array** | §4.5 |
| Shape dispatch | **Parsed key set vs a registry; generic fallback** | §4.3. Maintainer correction |
| Config key | **None. No migration** | `AGENTS.md` |
| Fan-out width | **Read per call; never assumed** | §2.3a. Maintainer correction |

## 4. Design

### 4.1 Latest-run selection — must agree with the ledger bar

**Corrected after audit.** The first draft preferred "the run_id of the last
**close** line". That is wrong: `collect()` defines latest as `runs[-1]`, the
last **open** (`logdashboard.py:489`). With `log_chapter_keep_runs: 3`, a closed
older run's file coexists with a newer run's file — so preferring the last close
picks the *older* run while the ledger bar shows the newer one, which is the
exact inconsistency this rule exists to prevent.

The rule is therefore simply:

> **The call ledger reports the same run the ledger bar reports — `runs[-1]`,
> the most recently opened run in `index.jsonl`.**

`_latest_run_file(bucket, runs)` returns that run's file when present; otherwise
the newest file on disk (a run that opened then died before writing its body),
otherwise `(None, "none")`. The chosen basis is rendered on the row so a
fallback is visible, not inferred.

`run_id` is `YYYYMMDD-HHMMSS-cmd-pid`, so lexicographic file order is
chronological; two runs in the same second tie-break on pid, which is why the
index — not the filename — is authoritative.

### 4.2 The call record

Built in one pass over the chosen file, keyed on `call_id`:

```python
{
  "call_id", "run_id", "chapter", "job", "model", "url",
  "candidate",        # int position in THIS job's fan-out, or None
  "candidates",       # declared fan-out width for this job, or None
  "consensus_for",    # the job this synthesises, or None
  "ts", "elapsed_s", "finish_reason", "error",
  "prompt_tok", "completion_tok", "reasoning_tok",   # 0 / None, never guessed
  "prompt",          # str, or None
  "params",          # dict, or None
  "body",            # response str, or None
  "shape",           # "lines"|"verdict"|"terms"|"notes"|"recap"|"text"|"unknown"
  "paired",          # bool — a request existed for this call_id
}
```

`shape` comes from `json.loads` on the **fence-stripped** body, then **key-set
matching** against the schema registry — never from `job` or `consensus_for`
(§2.3). A job with **no** `candidate` is single-shot, not "candidate 1".

`reasoning_tok` is `usage.completion_tokens_details.reasoning_tokens`, present
on **116/116** responses. **Corrected after audit:** the aggregate share is
**92.8%** (1,476,506 / 1,590,440), not the ~95% first claimed from a single
cherry-picked call. Per job: reviewer 99.7%, recap 98.5%, glossary 96.8%,
annotator 96.4%, consensus 90.7%, **translator 86.2%**. The translator is the
*least* reasoning-dominated job and by far the largest consumer, which makes the
split more interesting than the aggregate — so it gets its own column rather
than a footnote.

`params` carries no secrets — the only keys ever written are `temperature`,
`max_tokens`, `top_p`, `chat_template_kwargs.enable_thinking`,
`extra_body.max_completion_tokens`, `extra_body.reasoning_effort`. Shown
unfiltered.

### 4.3 Shape dispatch — the generic renderer is the default

**Corrected after maintainer input.** The first draft shipped a hardcoded
five-shape switch keyed on the alphabetically-first JSON key. Both halves of
that are wrong: the key is meaningless for `MERGE_SCHEMA`, and a schema added
later would silently fall through to raw text.

**The rule: parse, match the key set against a registry, fall back to a generic
renderer that handles anything.** The registry is *data*, not code, so a new
schema gets a readable rendering without touching the dashboard.

| test on the parsed object's keys | shape | renderer |
|---|---|---|
| ⊇ `{title, lines}` | `lines` | side-by-side matrix (§4.5) |
| ⊇ `{terms}` | `terms` | `source · translation · category · definition` table |
| ⊇ `{decisions}` | `cleanup` | `source · keep · reason` table |
| ⊇ `{verdict, reasons}` | `verdict` | badge + reason list |
| ⊇ `{notes}` | `notes` | `line · term · category · note` table |
| ⊇ `{recap}` | `recap` | prose |
| ⊇ `{style_summary, background}` | `profile` | two labelled prose blocks |
| `== {source, translation, definition, category}` | `merge` | one term card (flat) |
| parse failed, or nothing matched | `text` / `generic` | **generic renderer** |

**Generic renderer** — total, never raises, and the reason a future schema is
not a future bug:

- object → key/value list, scalars inline, nested objects indented
- array of objects → a table over the **union** of their keys, in first-seen
  order, `—` where a row lacks one
- array of scalars → bulleted list
- scalar / null → the value as text

Test order matters: the specific supersets must be checked **before** the
generic object path, or every shape falls into the generic renderer and the
named paths become dead code. `merge` is matched by **equality**, since
`MERGE_SCHEMA` forbids additional properties and no other schema has that exact
key set.

Both a recognised-but-unrenderable shape and a wholly unrecognised one end at
the generic renderer, and the page says which path was used — so a maintainer
can tell "no special view for this" from "this is not JSON".

`notes` carry a `line` index into the chapter's translation, so once the `lines`
view exists a note's line number can link into it. Cheap once both views work;
explicitly out of scope for the first pass.

### 4.4 Rendering

- **Index table: always fully rendered.** 116 rows × ~200 B ≈ 25 KB. Every call
  is listed — run, job, model, candidate, tokens, reasoning, elapsed, finish
  reason, `call_id`. Nothing is dropped, nothing is capped.
- **Bodies: one JSON blob** in `<script type="application/json" id="dl-bodies">`.

> #### ⚠ Blob encoding — corrected after audit, and proven in a real browser
>
> **Do NOT `html.escape` the blob.** `<script>` is a *rawtext* element: the HTML
> parser never decodes character references inside it, so `textContent` hands
> `&quot;` back **literally** and `JSON.parse` fails at position 1.
>
> The first draft of this plan prescribed `html.escape(json.dumps(...))`, and a
> Python check using `html.unescape` "confirmed" it round-trips. That check
> simulated the *opposite* of browser behaviour and passed for the wrong reason.
>
> Measured in Chrome (`probe-artifacts/blob-escape-browser-test3.html`):
>
> | Encoding | `JSON.parse` |
> |---|---|
> | `html.escape(json)` | **FAIL** — `Expected property name or '}' at position 1` |
> | `json.replace("<", "\u003c")` | **OK** — round-trips exactly |
>
> Impact of getting this wrong is total, not edge-case: **116/116** response
> bodies contain `"`, so every single record fails to parse.
>
> **The rule:** `blob = json.dumps(records, ensure_ascii=False).replace("<", "\\u003c")`.
> Nothing else is escaped. `<` is the only character that can terminate a script
> element, and `\u003c` is a valid JSON string escape, so this is lossless — the
> round-trip test recovers `a<b>&c` and a literal `</script><img src=x>` verbatim.
>
> **Related trap:** a literal `</script>` anywhere in the generated file — even
> inside a JS comment — silently ends the element and hands the rest to the HTML
> parser. Both probe pages in `probe-artifacts/` were broken by this before the
> final one built every such sequence from `String.fromCharCode`.
>
> **Template safety:** `render()` returns an `f"""..."""` containing `_CSS`.
> The blob must be passed as an f-string **expression** (`{blob}`), never pasted
> into the template text — **116/116** bodies and **117/117** prompts contain
> `{` and `}`, and a `ValueError` there is swallowed by `write_dashboard`'s
> blanket `except` (`logdashboard.py:1411`), which would silently produce **no
> page at all**.

- **On click**, `renderCall(call)` builds:
  - **shape `lines`** → a **side-by-side matrix**: `i` · source · one column per
    fan-out candidate · one column for consensus (**N+1**, §2.3a). Column
    headers carry the model name from each call's own `model` field.
    **Verified 20/20** on translator calls
    (`probe-artifacts/sidebyside-report.txt`): every source array is recoverable
    and its `i` indices match the response's `lines[].i` exactly.
  - **the other registry shapes** → their tables/badges per §4.3.
  - **anything else** → the generic renderer. Never an error, never raw text
    when the body did parse.

### 4.5 Source extraction — balanced scan, **not** regex

The one prompt that is parsed. A regex such as
`\[(?:\{[^{}]*\}|\"(?:[^\"\\]|\\.)*\")*\]` **fails on all 20 translator calls**
(`probe-artifacts/sidebyside-check.py`): novel prose contains `[`, `]`, `{` and
`}` inside `t` strings, which desyncs bracket counting.

**Corrected after audit:** the array sits under `### Source Data`
(`assets/templates/translation.md:26`), **not** `### Task` (line 7) as the
first draft claimed. Keying on `### Task` matches prose first and grabs the
wrong bracket — the audit reproduced a 30/30 failure that way.

The working algorithm walks forward from each `[` with a counter that respects
string literals and backslash escapes, accepts the first balanced span that
`json.loads` into a list of `{i, t}` objects, then takes the **last** such span.
It does not depend on the marker text, so a template edit that moves the heading
cannot silently break it; recovery failure renders the translation alone with a
stated reason.

Because each fan-out chapter is translated by **N candidates plus one
consensus call** against a single source array, the view is a matrix with
**N+1 translation columns**, not two:

```
 i  | source | cand 1 (glm-5.3) | cand 2 (MiniMax-M3.1) | … | cand N | consensus (glm-5.3)
```

Every column header takes its model name from that call's own `model` field
(§2.3a), so a project with five models renders five columns and a project with
one renders one plus consensus. When a job has no consensus call, the column is
absent rather than empty — absence is not a zero.

### 4.6 Why only this one prompt is parsed

Every job's prompt uses different markers — `[Source Text]` (reviewer,
annotator), `### Task` / `### Source Data` (translator), `Source lines:`
(glossary), `[Recap so far]` (recap) — and the bodies are prompt **templates**
under `assets/templates/`. Parsing them all would couple dashboard correctness
to template edits, failing silently rather than loudly. One parse, verified
20/20, with a stated fallback; every other prompt shown **raw**.

This matters more given §2.3: because one job name can run several schemas, and
`consensus_for` carries only the job name, the **prompt is the only place the
task identity is actually recoverable**. The log records which *job* ran, never
which *schema* or template. That is a limitation of the logs themselves, so the
page must not imply otherwise — it shows the job label and the observed shape,
and leaves the pairing between them as inference.

### 4.7 `unpaired` events

`result` and `chunk` rows carry no `call_id` and are not calls — measured 42 and
10 respectively in the sample's newest files. They move to their own small table
(`kind`, verdict, counts) so the call ledger contains only calls and nothing is
lost. `result` kinds observed: `faithfulness`, `glossary_expand`, `tn_generate`,
`tn_dedup`.

## 5. Files

| File | Change |
|---|---|
| `scripts/lib/logdashboard.py` | New `_latest_run_file`, `_shape_of`, `_parse_body`, `_encode_blob`. Replace `_calls_from_tier2` + `_collect_io` with one pass producing `calls` + `unpaired`. New `_call_ledger`, `_ledger_js`, CSS. Delete `IO_BYTE_CAP` and `_collect_io`. |
| `scripts/translate.py` | Remove `--html-io` from parser and help; `write_dashboard(project_dir)` loses `io`. |
| `tests/test_log_dashboard.py` | **Delete** `17c` ("names the flag that would include them") and `17e` ("does not nag about `--html-io` there") — they assert the removed flag string. Then extend per §6. |
| `references/maintenance.md` | `--html-io` references at lines 22, 63, 66 |
| `references/file-formats.md` | `--html-io` / cap references at lines 1349, 1351; add the blob-encoding rule and the run-selection rule |
| `README.md` | `--html-io` references at lines 883, 895 |
| `SKILL.md` | `--html-io` reference at line 701 |
| `logdashboard.py` | `--html-io` strings at lines 1281, 1339 |
| `probe-artifacts/blob-escape-browser-test3.html` | already written — the passing browser proof |
| `probe-artifacts/remeasure.py` | already written — §2.4/§2.5 numbers |
| `probe-artifacts/candidates-check.py` | already written — §2.3a fan-out width per job |

**No migration** — no `config.json` key, no template change.

## 6. Validation

1. **Blob encoding** — a body containing `</script>`, `<img onerror=…>` and a
   `"quoted"` string round-trips through the blob **unchanged**, and the literal
   `</script>` never appears in the output. *Must not be tested with
   `html.unescape` — that is the bug, and it passes green.* Assert the raw blob
   string equals `json.dumps(...).replace("<", "\u003c")`.
2. **Template safety** — a body full of `{` and `}` still renders a page (guards
   the swallowed-`ValueError` path).
3. **Latest-run agreement** — with three retained runs and an index naming the
   newest, the call ledger and the ledger bar report the **same** run; a chapter
   whose latest run has no file falls back and says so.
4. **No run mixing** — a response from a non-selected run appears nowhere.
   *Regression test for §2.6b.*
5. **No cap** — 300 synthetic calls × 2 KB all appear, index and blob complete;
   `IO_BYTE_CAP` gone; `--html-io` rejected as an unknown flag.
6. **Fence parsing** — a ```` ```json ````-fenced glossary response renders as a
   `terms` table. *Regression test for §2.2.*
7. **Shape dispatch** — every **one of the 8 registered schemas** (§2.3)
   produces its named view, not just the five the sample contains. `MERGE_SCHEMA`
   renders as a flat term card, not an array table; `CLEANUP_SCHEMA` as a
   `decisions` table; `PROFILE_SCHEMA` as labelled prose.
8. **Job independence** — the same `job` name (`glossary`) with three different
   schemas routes to three different views, and two calls sharing
   `consensus_for: "glossary"` but different shapes do **not** share a renderer.
   *Guards the "dispatch on job" error this plan used to make.*
9. **Generic fallback** — a synthetic response with keys matching no registered
   schema still renders readably (object, array-of-objects with ragged keys,
   array-of-scalars, scalar, `null`), and says it used the generic path. *This
   is what keeps a future schema from becoming a future bug.*
10. **Side-by-side** — a `lines` call renders N paired rows; a prompt containing
    `[`/`]`/`{` inside novel prose still parses (the §4.5 regex trap); with an
    unparseable source it renders the translation alone and states why.
11. **Candidate comparison** — a fan-out of N renders N+1 translation columns
    (candidates plus consensus), each headed by the model name from its own call.
    Fixtures must cover **N=1, N=2 and N=5**, and a job whose candidate order is
    **reversed** relative to another job on the same chapter (§2.3a) — the
    columns must follow each call's `model`, never the index. *Guards the
    "candidate 1 is model X" assumption this plan used to make.*
12. **Single-shot jobs** — a job with no `candidate` and no consensus renders one
    column and no phantom empty ones.
13. **No invented numbers** — `usage: null` → 0 tokens; absent
    `completion_tokens_details` → `—`, not 0; real zero renders `0`.
14. **Never-raise** — a torn JSONL tail, an unreadable bucket, and a locked
    destination each yield a page or `None`, raising nothing.
15. **No network** — no `fetch(`, `XMLHttpRequest`, `src=`/`href=` to `http`,
    `@import`, `<link>`, `<iframe>`.

## 7. Open questions

None blocking. Three defaults chosen, all cheap to flip:

- **Reasoning tokens as a dedicated column** — 92.8% of completion spend, so it
  is information, not noise.
- **`unpaired` events as a separate small table** rather than dropped.
- **Side-by-side is best-effort**; when the prompt does not parse, the row says
  so instead of guessing a line offset.

## 8. Deferred: make the log shape self-describing

**Maintainer note, 2026-10-09 — record this for later.** The dashboard above
*infers* the response shape by parsing the body and matching its key set against
a hand-written registry (§4.3). That inference is the most fragile part of the
whole design, and it exists only because the log does not say what it is.

The root cause is in §2.3: `logger.log_event` records `job` but never the
**schema** or **template** that produced the call, and one job name runs several
schemas. So the reader — the dashboard today, `report.md`, any future tool — has
to re-derive task identity from the response body, or from the prompt's
markdown headings, or not at all.

The fix belongs at the write site, not the read site:

1. **Log the schema name.** Every call already passes a `json_schema`
   (`pipeline.py:1650`, `:1756`, `story.py:173`, `profile.py`, …). Give each
   schema a name at its definition and log it on `llm_request`/`llm_response`
   alongside `job`. One field; removes all guessing.
2. **Log the template name.** The call sites already know which `.md` they
   filled (`tpl_tn_generate`, `tpl_faithfulness`, …). Logging it makes
   `consensus_for: "annotator"` disambiguateable, which the job name cannot do.
3. **Log the schema name on `consensus` calls** too — the merged call inherits
   the arbitrated task's schema, so `consensus_for` plus a recorded schema is
   fully determined.

Together these turn §4.3's registry from a guess into a lookup, let the
dashboard skip parsing for the common path, and make the side-by-side (§4.5)
recoverable without matching prompt markdown.

**Not part of this change.** It adds fields to the trace format, so it is a
format change rather than a dashboard change, and per `AGENTS.md` it would need
its own migration and a `file-formats.md` update. Deliberately deferred rather
than smuggled in — the dashboard is fully correct without it, just more
defensive than it needs to be.

---

## 9. What actually shipped

Implemented as specified. Measured on `example_project/`: **117 calls**,
**116 paired**, **1 unpaired** (a request with no response — a killed call, kept
and flagged), **52 stage events**, **3.32 MB** page, **~127 ms** to collect +
render + write. Every shape present in the real data dispatches: 33 `verdict`,
32 `notes`, 30 `lines`, 11 `terms`, 10 `recap`, 1 `text`.

### Defects the build surfaced that the plan did not anticipate

1. **Call de-duplication doubled the entire ledger.** `order` was gated per
   dict (`call_id not in requests` / `not in responses`), so a `call_id` present
   on *both* sides was appended once when the request was read and again when
   its response arrived: 16 records for 8 calls, 233 for 117. Fix: one shared
   `seen` set gating only the ORDER. The first fix over-corrected — gating the
   *store* too, which dropped every response and left 117 calls all unpaired
   with no body. Correct form: store both sides always, de-duplicate the order.
   Caught by a per-chapter count, not by the suite.
2. **Consensus calls silently dropped out of the side-by-side.** A consensus call
   reports `job="consensus"` and `consensus_for="translator"`, so grouping on
   `job` put the candidates under one group and the consensus column under
   another. The matrix was missing its most interesting column. Fix: group on
   `consensus_for or job` (`key_task`), in both the Python de-duplication and the
   JS sibling lookup.
3. **Totals wore the wrong label.** The roll-up iterated `calls_detail`, which
   is deliberately empty for any tier-2 chapter (those rows live in `calls`), so
   it fell through to index totals while still reporting
   `"per-call rows (tier 2)"`. Fix: roll up over the same rows the ledger
   renders.
4. **"Hide all bodies" did nothing.** `host.firstChild.querySelector('.body')`
   matches at any depth, so it returned a *grandchild*, and
   `firstChild.removeChild()` threw `NotFoundError` — which aborted the whole
   expand/collapse loop on its first row. Found only by clicking the button in a
   real browser; no unit test was looking at it. Fix: remove the `.bodywrap`
   wrapper via its own `parentNode`.

A fifth bug (`class="tbl body"` colliding with the toggle's `.body` locator) was
caught in the same browser pass and is a near-miss for #4.

### Defects found after the first handoff

5. **The chapter-ledger outcome filter did nothing.** Reported by the
   maintainer as "the All/Translated/Unclosed buttons don't work". The rewrite
   had changed `querySelectorAll('.lrow')` to `querySelectorAll('tr.lrow')` —
   but a chapter row is a `<details class="lrow">`, so the selector matched
   **nothing**. The click handler still ran its first statement, so the chip
   flipped `aria-pressed` correctly: the control looked alive while filtering
   zero rows. This is the dangerous shape of JS bug — the evidence on screen
   contradicts the fault, so it reads as "the button is broken" rather than "the
   selector is wrong".

   Fixed by test **22** (`case_22_script_selectors_match_the_markup`), which
   parses every `querySelectorAll('…')` out of the shipped page and asserts the
   tag qualifier agrees with the tag the markup actually emits. It is a
   structural check rather than a behavioural one precisely because the
   behaviour under test was never wired up.

   Note the test immediately caught its own maintainer: the first version of the
   fix's comment contained the literal forbidden token `tr.lrow`, and case 22x
   failed on the comment. Same failure mode as the `</script>`-inside-a-comment
   hazard in §4.3 — **never write the pattern you are guarding against into a
   comment in the file the guard scans.**

6. **Every chapter claimed "Model calls — not recorded — no tier-1 run
   retained".** The maintainer suspected stale pre-migration logs; it was not
   that. All 11 buckets hold 8–11 real calls each. The cause was defect #1's
   sibling: when the calls moved to the unified `calls` list, `calls_detail`
   was set to `[]` for exactly those chapters (to stop the roll-up counting
   them twice), and the chapter row had no route back to them. The roll-up was
   correct; only the per-chapter panel went blind — and it blamed tier 1 for
   it, on a project whose tier 1 was present and used.

   Fixed by giving each chapter its own slice of `calls`, with the roll-up still
   iterating `calls` so nothing is counted twice. Pinned by `16f`–`16f4` and by
   case 23.

   The deeper lesson is #5's in a different costume: **a message that names a
   cause is a claim, and an empty data path is not evidence for it.** The
   panel was empty for two unrelated reasons (`log_llm` off, or retention had
   emptied every bucket) plus a third that was purely my refactor. It now
   states both real causes and names neither. The row tooltip's separate
   `"stage detail unavailable (no tier-1 run retained)"` is left alone: that
   one is provably true when it fires.

   Scope note: the migration worry is worth keeping as a habit. This project has
   no pre-v010 buckets — all 11 are `CHAPTER_NNNN`, every run_id falls in
   2026-10-07/08, and `index.jsonl` shows CHAPTER_0003 with 5 runs of which 3
   have bodies, which is `log_chapter_keep_runs: 3` at work, not an old format.

### Also worth recording

- **Response rows with no `call_id` were dropped entirely.** They cannot be
  *joined*, but they are still real calls with bodies and tokens. They now get a
  stable synthetic key (`~<event>:<ts>:<n>`) and are flagged unpaired.
- **Shape dispatch shipped as data, not code.** `_SHAPE_REGISTRY` is a tuple of
  `(frozenset(keys), shape)` pairs, most specific first; `merge` matches by
  equality because `MERGE_SCHEMA` forbids additional properties. Anything
  unmatched reaches the generic renderer.
- **The JS never re-guesses the shape.** Python decides and ships
  `record.shape`; the browser only renders it. A second copy of the dispatch
  rules would be a second thing to keep in sync.
- **Source arrays are stored once per fan-out group**, not once per call —
  measured 60 → 10 arrays on the sample, and the group key is `consensus_for or
  job`, so the consensus call shares its group's array.
- **The whole ledger row toggles**, not just the call-id button. Three guards
  earn their place, and each one is invisible in a screenshot, so all three are
  asserted in case 24: the nested button stops propagation (otherwise the click
  bubbles and fires both handlers, opening then instantly closing); an active
  text selection suppresses the toggle (dragging out a model name to copy must
  not collapse the row); and Enter/Space toggle from the keyboard. The `<tr>`
  gets `tabindex="0"` and a visible focus ring but deliberately **no** `role` —
  `role="button"` would destroy the table semantics, so the inner button stays
  the accessible control and reports `aria-expanded`.

### Still open (deferred deliberately)

- The `schema`/`template` logging in §8.
- `notes[].line` could link into the `lines` side-by-side; explicitly out of
  scope for the first pass.