You are reviewing the translator's notes attached to a {{source_lang}} novel being translated into {{target_lang}}.

A note earns its place only by adding context: information the {{target_lang}} reader cannot get from the translation itself, or an explanation of context the {{source_lang}} reader has implicitly and the translation cannot carry. Review the existing notes below and flag the ones that fail that bar.

[Notes Review]

{{entries}}

[Task]

Judge each note against its translated line, its source line, and the surrounding context:
- `restates` — the note adds nothing beyond what the translated line already says; a reader who skipped it would lose nothing.
- `overexplains` — the note explains common knowledge or something inferable from the surrounding context, failing the comprehension threshold: the reader did not need it spelled out.
- `wrong` — the note misexplains the source term: factually or culturally off, the wrong literal meaning, or a background/conversion claim that does not hold.
- `misanchored` — the note rides the wrong line: the `term` does not appear in `translated_line` (nor its source in `source_line`), or the explanation clearly belongs to a different line than the one it is attached to.

Report ONLY genuine problems — a note you consider acceptable must NOT be listed; return an empty findings array when every note is fine. When unsure, do not report. Do not invent notes that are absent from the list — only judge the entries given, and reference each by its exact `idx`.

[Severity]

- `warn` — the note misleads the reader or wastes a footnote; fix or delete it.
- `info` — minor; still worth improving.

`suggestion` is the concrete replacement note text when you are confident of the fix, or the concrete fix itself (e.g. the line the note should ride on); an empty string otherwise.

Return ONE JSON object:
{"findings": [{"idx": <the entry's idx>, "kind": "restates"|"overexplains"|"wrong"|"misanchored", "severity": "warn"|"info", "reason": "<one sentence>", "suggestion": "<concrete replacement note text or fix, or empty string>"}]}

Output ONLY the JSON object — no code fences, no explanations.
