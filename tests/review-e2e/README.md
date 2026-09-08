# review-e2e: manual mock-server kit for `review glossary` / `review fix`

A manual end-to-end kit for the advisory glossary review flow against the
offline mock server — nothing here is wired into the automated suite. The
two files are a complete minimal project: `config.json` points all five
providers at `http://127.0.0.1:8901/v1` (the mock server's default port),
and `glossary.json` holds the three terms from `tests/test_glossary_review.py`
case 10 (天雷宗 -> "river town", 裴家村, 灵石).

## Procedure

1. Start the mock server (it prints its URL and serves forever):

   ```
   python tests/mock_server.py          # default port 8901; or: python tests/mock_server.py 8901
   ```

2. Scaffold a throwaway project — an empty directory with both kit files
   copied to its root (a `logs/` directory appears there on the first run):

   ```
   mkdir review-e2e-run && cp tests/review-e2e/config.json tests/review-e2e/glossary.json review-e2e-run/
   ```

3. Run the review commands against it:

   ```
   python novel-translator/scripts/translate.py --project review-e2e-run review glossary
   python novel-translator/scripts/translate.py --project review-e2e-run review glossary --fix
   python novel-translator/scripts/translate.py --project review-e2e-run review fix --dry-run
   python novel-translator/scripts/translate.py --project review-e2e-run review fix
   ```

## What to expect (verified against this kit)

- `review glossary` exits 1 with one warn (the mock's review tier flags the
  first glossary entry, 天雷宗, as a mistranslation suggesting "Mock Fix")
  plus one heuristic info (裴家村's English-string variant), and writes
  `review-report.md` into the project directory: YAML frontmatter with the
  run's counts (`entries_reviewed: 3`, `batch_errors: 0`, `outcome` 1 warn
  / 1 info, `machine_applicable: 2`, `manual_review: 0`,
  `manual_review_indices: []`), then a `## Machine-applicable` section
  holding both findings, including the `- Command: glossary replace ...`
  bullet for the mistranslation.
- `review glossary --fix` applies the suggestion in-process
  (天雷宗 -> "Mock Fix" in glossary.json) and exits 0.
- `review fix --dry-run` lists both machine-applicable commands and exits 0.
- `review fix` (real run): the `glossary set --remove-variant` command
  applies, but the `glossary replace` command fails with
  `chapters.json not found - run 'init' first` — replace rewrites chapters
  and needs a manifest, which this two-file kit deliberately omits. To
  exercise that leg, add a `chapters.json` (and `translated/` chapters) to
  the scaffolded project; the rest works with the two files alone.
