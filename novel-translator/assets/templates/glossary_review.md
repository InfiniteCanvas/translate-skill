You are reviewing the translation glossary for a {{source_lang}} novel being translated into {{target_lang}}.

The glossary keeps names and set phrases consistent across chapters, so a wrong entry poisons every later chapter that uses it. Review the entries below before further translation relies on them.

[Glossary Review]

{{entries}}

[Task]

Judge each glossary entry:
- `translation` faithfully and idiomatically renders `source` in {{target_lang}} — a wrong, misleading, or awkwardly literal rendering is kind `mistranslation`.
- The translation is in the target language, not left in {{source_lang}} script or wording — kind `wrong_language`.
- `definition` is accurate English and actually describes the term — kind `definition`.
- `category` fits the term — kind `category`.
- Each string in `variants` is a {{source_lang}}-script spelling of the source (e.g. traditional vs. simplified) — anything else parked there is kind `variant`.
- Two glossary entries are the same term or one duplicates another — kind `duplicate`.
- Different entries rendered by the same translation in a way that obscures distinct concepts — kind `collision`.

Also report cross-entry conflicts among the entries shown here — the same term split across entries, or distinct concepts collapsed into one shared rendering.

[Severity]

- `warn` — should be fixed before further translation.
- `info` — minor or optional improvement.

Report ONLY genuine problems — entries you consider acceptable must NOT be listed. When unsure, do not report.

`suggestion` is the corrected translation, definition, or category string when you are confident of the fix; otherwise an empty string.
`action` is one concise instruction telling a fixing agent exactly what to change in the glossary (which entry, which field, what value — and for duplicates/collisions, which entries to merge or how to separate them); an empty string when the suggestion alone says it all.

Return ONE JSON object:
{"findings": [{"source": "<exactly as listed>", "kind": "...", "severity": "warn" or "info", "reason": "<short English reason>", "suggestion": "<fix or empty string>", "action": "<instruction or empty string>"}]}

Output ONLY the JSON object — no code fences, no explanations.
