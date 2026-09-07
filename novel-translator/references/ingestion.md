# Ingestion - novel-translator

The playbook for turning a web novel into a translation project: from a
novel URL to a scaffolded, ready-to-translate project. `CHAPTER_RE` and
`read_chapter` in `scripts/lib/project.py` are the source of truth for
behavior; this file is the source of truth for the procedure.

## When to use

The user gives you a URL instead of chapter files ("download this novel and
prepare it as a translation project"). The path is always the same:

1. Scrape the table of contents for the ordered chapter list.
2. Fetch each chapter and convert it to a `source/Chapter_NNNN.md` file.
3. Run `init`.
4. For long novels, work in batches: download the next batch, `sync`,
   `translate --next N`, repeat.

## The hard contract

Before `init` runs, the project directory must contain a `source/`
subdirectory holding at least one UTF-8 markdown file whose name matches:

```text
^Chapter_(\d{1,4})([a-z]?)\.md$     (case-insensitive)
```

`init` fails fast when `source/` is missing or holds zero matching files.

## File naming rules

- 3-digit (`Chapter_001.md`) and 4-digit (`Chapter_0001.md`) conventions
  both work; sorting is by parsed number, not by string. Prefer 4-digit
  padding for scraped novels - many exceed 999 chapters.
- The letter suffix marks extras/bonus chapters: `Chapter_0042a.md` sorts
  between `Chapter_0042.md` and `Chapter_0043.md`.
- Numbers above 9999 are unsupported.
- Never rename or renumber after `init`. The manifest, translated copies,
  and note sidecars all key on file names; renaming a source file orphans
  its status, title, and translation.
- Near-miss names are SILENTLY ignored by discovery - no error, no manifest
  entry: `Chapter_0007.zh.md`, `chapter 7.md`, `0007.md` (no `Chapter_`
  prefix), or a number typed with full-width digits. Catch them by
  comparing the manifest count against the TOC (see Verifying below).

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

After (`source/Chapter_0001.md`):

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
nothing to sync.

## Verifying before translating

- `init` prints `manifest: N chapter(s)` - compare N against the TOC's
  chapter count. `status` lists the chapters.
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
- Full-width digits never match.
- Renaming files after init orphans their statuses, titles, and
  translations.
- Inserting files in the middle renumbers `order` of all later chapters.
  Translations in progress key by file name - prefer appending.
- Above 9999 chapters is unsupported.
