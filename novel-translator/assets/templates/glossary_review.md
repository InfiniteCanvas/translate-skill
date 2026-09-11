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
- The term is not a named entity or a named action — the glossary keeps only expressions that name one specific thing: a person, place, sect/organization, named artifact, named technique or skill, named cultivation realm or power state, or a title bound to a name ("Empress Dowager Zhao", "Steward Li"). A common noun naming a class of things (e.g. 麦穗 "wheat stalks"), or a standalone title, kinship term, or other form of address without a name ("great grandmother") — wording that shifts with the speaker and the scene — does not belong in a glossary at all — kind `mundane`.
- Do not flag entries with `origin: "seeded"` as `mundane`: seeded entries come from hand-curated catalogues of deliberate domain vocabulary.
- Do not flag entries with `category: "unit"` as `mundane` either: unit entries are guide-only reference vocabulary — injected into the translation prompt to pin a transliteration (li, catty) and ignored by the drift checker — not named entities. Their `translation` and `definition` (the conversion) are still judged like any other entry's.

Also report cross-entry conflicts among the entries shown here — the same term split across entries, or distinct concepts collapsed into one shared rendering.

[Severity]

- `warn` — should be fixed before further translation.
- `info` — minor or optional improvement.

Report ONLY genuine problems — entries you consider acceptable must NOT be listed. When unsure, do not report.

`suggestion` is the corrected translation, definition, or category string when you are confident of the fix; otherwise an empty string.
`action` is one concise instruction telling a fixing agent exactly what to change in the glossary (which entry, which field, what value — and for duplicates/collisions, which entries to merge or how to separate them); an empty string when the suggestion alone says it all. `mundane` findings leave both `suggestion` and `action` empty — the fix (retiring the term) is fully determined by the source.

Return ONE JSON object:
{"findings": [{"source": "<exactly as listed>", "kind": "...", "severity": "warn" or "info", "reason": "<short English reason>", "suggestion": "<fix or empty string>", "action": "<instruction or empty string>"}]}

Output ONLY the JSON object — no code fences, no explanations.
