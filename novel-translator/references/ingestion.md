# Ingestion - novel-translator

The playbook for turning a web novel into a translation project: from a
novel URL to a scaffolded, ready-to-translate project. `CHAPTER_RE` and
`read_chapter` in `scripts/lib/project.py` are the source of truth for
behavior; this file is the source of truth for the procedure.

## When to use

The user gives you a URL instead of chapter files ("download this novel and
prepare it as a translation project"). The path is always the same:

1. Scrape the table of contents for the ordered chapter list.
2. Fetch each chapter and convert it to a `source/CHAPTER_NNNN.md` file.
3. Run `init`.
4. For long novels, work in batches: download the next batch, `sync`,
   `translate --next N`, repeat.

## The hard contract

Before `init` runs, the project directory must contain a `source/`
subdirectory holding at least one UTF-8 markdown file whose name matches:

```text
^CHAPTER_([0-9]{4})\.md$     (EXACT case; ASCII digits only)
```

`init` fails fast when `source/` is missing or holds zero matching files.

## File naming rules

- `CHAPTER_NNNN.md` is the **only** accepted spelling: exactly 4 zero-padded
  digits, no suffix. Padding is fixed, so the older 3-digit spelling
  (`Chapter_001.md`) is rejected — accepting both let one chapter be spelled
  two ways. Sorting is by parsed number, which with fixed padding is also
  plain lexicographic order. 9999 chapters is far beyond any novel.
- There is **no letter suffix**. An earlier version accepted `Chapter_0042a.md`
  for extras/bonus chapters; it was removed as unused, and such a file is now a
  near-miss (warned about below).
- Never rename or renumber after `init`. The manifest, translated copies,
  and note sidecars all key on file names; renaming a source file orphans
  its status, title, and translation.
- Near-miss names are not discovered — no manifest entry — but `init` and
  `sync` now **print a `[warn]` for each**, naming the file and the reason:

  ```text
  [warn] source/Chapter_001.md: ignored - 3 digits, not 4 (rename to CHAPTER_NNNN.md - 4 digits)
  [warn] source/Chapter_0042a.md: ignored - extras/bonus chapters (letter suffix) are no longer accepted - give this chapter its own number (rename to CHAPTER_NNNN.md - 4 digits)
  [warn] source/Chapter_0007.zh.md: ignored - not CHAPTER_NNNN.md (exactly 4 digits) (rename to CHAPTER_NNNN.md - 4 digits)
  [warn] 3 source file(s) look like chapters but were not added to the manifest
  ```

  The classes covered are short padding, a letter suffix (single or
  multi-letter), `Chapter_0007.zh.md`, `chapter 7.md`, `0007.md` (no
  `CHAPTER_` prefix), non-ASCII digits (full-width or Arabic-Indic), and a
  wrong extension. Files that are plainly not chapters (`README.md`,
  `notes.txt`, `.gitkeep`) are never reported — `source/` legitimately holds
  more than chapters. A warning never fails the command: `init`/`sync` still
  exit 0.

## Converting scraped pages to chapter files

Per chapter, turn the scraped page into clean, unwrapped UTF-8 text:

- Strip site chrome: navigation, ads, watermarks, boilerplate paragraphs
  ("click to continue", "next chapter" links), HTML tags, and HTML
  entities (`&amp;` -> `&`).
- Unwrap hard-wrapped source text: join wrapped lines so each paragraph is
  exactly one physical line.
- Blank lines only between paragraphs. The body is split on `\n` and
  translated as an indexed JSON array - one source line in, one translated
  line out - so a decorative blank line becomes a translated blank line.
  Trailing blank lines are stripped automatically.
- Convert the encoding to UTF-8; GBK/GB18030 is common on older Chinese
  sites.
- Keep the text verbatim - no cleanup of the prose itself.

Before (raw scrape, hard-wrapped):

```text
<h2>第二章 山边小村</h2>
<p>
  韩立沿着山路走了半日，
  终于在天黑前赶到了青牛镇。
</p>
<p>镇口的老槐树下，<br>坐着一名青衣少女。</p>
```

After (`source/CHAPTER_0001.md`):

```text
---
chapter_title: 第二章 山边小村
source_url: https://example.com/novel/chapter-1
---

韩立沿着山路走了半日，终于在天黑前赶到了青牛镇。
镇口的老槐树下，坐着一名青衣少女。
```

## Frontmatter during ingestion

Write per-chapter frontmatter when the site gives you the data:

```text
---
chapter_title: 第二章 山边小村
source_url: https://example.com/novel/chapter-1
---
```

- `init` and `sync` backfill missing frontmatter on bare chapters:
  `novel_title`/`author`/`source_url` from the novel-level values, and
  `chapter_title` from the first non-empty body line.
- `order` is always tool-managed - never write it.
- If the first body line repeats the chapter title AND frontmatter has
  `chapter_title`, the pipeline automatically drops the redundant body
  line at translate time - either placement works. Prefer frontmatter when
  the TOC gives you titles.

## Source repairs

`init` and `sync` repair the source before the manifest is rebuilt. Two
defects are repaired, and each repair is reported on the console.

- **A fused chapter heading.** A site that renders its heading into the
  chapter body - as the whole first paragraph, or as its prefix
  (`第135章 不甘楊浩宇…` under a heading of `第135章 不甘`) - leaves the
  heading inside the text the model translates. When the recorded
  `chapter_title` is a heading (it starts with `第N章` or `Chapter N`),
  it is taken off the front of the first paragraph.
- **A duplicated opening block.** A run of three or more consecutive
  paragraphs that repeats an earlier run of the same paragraphs - the
  shape a mirror's preview produces when it serves its excerpt and then
  the whole chapter. Only the later occurrence is removed, and the report
  names the paragraphs.

Three things the repair will not do:

- It never deletes a paragraph to strip a heading. The recorded title is
  only trusted when it looks like a heading, and only when it is
  strictly shorter than the line it prefixes: the skill's own frontmatter
  backfill derives a missing `chapter_title` from a chapter's whole first
  body line, so a line of prose can arrive wearing a title.
- It touches only the first paragraph. A speech that runs across
  paragraphs keeps its bare middle pieces - the site marks such a speech
  on its first and last pieces only, and that is the source's shape.
- It never rewrites a chapter that already has a translation. A source
  and its translation are paired line for line, so removing a line from
  the source would break the pair, and the pipeline compares a chapter
  against the source snapshot it took when the chapter started - it
  cannot see the drift. Such a chapter is reported instead
  (`[warn] CHAPTER_NNNN.md: ... - the chapter has a translation, so the
  source was left alone; re-translate it to pick the change up`).

Both repairs are idempotent: a second `sync` over a repaired source
finds nothing.

## What the book is missing

`init` and `sync` also write what the book is missing to
`logs/completeness.json`, and print it. Two independent signals:

- **The expected count.** How many chapters the source says the book
  has, against the chapters the project holds. The count comes from the
  source's own table of contents when the project carries one
  (`zh-toc.json`, the file the acquisition skill writes), and from the
  config key `expected_chapters` when it does not. The report keeps two
  kinds of missing apart, because they are different facts: the
  positions the source's table of contents does not list (the source has
  not published them) and the positions it lists but the project does
  not hold (the acquisition did not fetch them).
- **The length outlier.** A chapter whose source text is far shorter
  than the rest of the book, compared against the book's own median
  paragraph count (`completeness_outlier_ratio`, default 0.3). This is
  what a truncated acquisition looks like.

Neither signal blocks anything. A completeness finding is a fact about
the book, and what to do about it is the operator's decision.

## Workflow: long novels in batches

- Discover the TOC: the ordered chapter list. Paginated TOCs are common -
  collect every page and preserve the site's order.
- Fetch chapters with whatever web tooling the environment provides
  (firecrawl, WebFetch, curl). Be polite: rate-limit and respect the
  site; anti-bot behavior differs per site. No specific tool is required.

Then pick a path by novel length:

- **Small novel**: download everything into `source/`, then run `init`.
- **Long novel**: download a first batch, run `init`, then loop until the
  book is done - download the next batch, then:

```bash
uv run "$SCRIPT" sync --project .
uv run "$SCRIPT" translate --project . --next N
```

`sync` only works on an initialized project; before `init` there is
nothing to sync. A `sync` that changed anything commits
`sync: rescan source` to the project's git history (the repository is
created by `init`, or backfilled on existing projects by migration v003).

## Verifying before translating

- `init` prints `[init] manifest: N chapter(s)`; `init` and `sync` also
  print the completeness line - N of M chapters present, and which
  positions are missing - and write it to `logs/completeness.json`.
  `status` lists the chapters.
- Spot-check one chapter file: name matches the regex, text decodes
  cleanly (no mojibake), one paragraph per physical line.
- Run `status` and confirm there are no `[warn] N source file(s) not in
  manifest - run 'sync'` drift lines (it also warns on the reverse -
  manifest entries whose file is gone).

## Pitfalls checklist

- Wrong file names are silently dropped (see naming rules above).
- Hard-wrapped text inflates the line count: every wrapped physical line
  becomes its own translated line.
- Decorative blank lines become translated blank lines.
- Non-UTF-8 encodings (GBK/GB18030) produce mojibake - convert first.
- Non-ASCII digits never match (full-width and Arabic-Indic numerals are
  silently ignored; the chapter number must be ASCII `[0-9]`).
- Renaming files after init orphans their statuses, titles, and
  translations.
- Inserting files in the middle renumbers `order` of all later chapters.
  Translations in progress key by file name - prefer appending.
- Above 9999 chapters is unsupported.
