You are reviewing a translated chapter of a {{source_lang}} novel for cultural adaptation.

{{background_section}}

[Source Text]

{{source_lines}}

[Translation]

{{translation_lines}}

[Glossary — deliberate renderings]

The following renderings are pinned project decisions, not translation errors. Wherever these terms appear, the translation is required to use exactly the rendering shown:

{{glossary}}

[Task]

Identify places where the translation alone cannot carry what the {{source_lang}} text gives its reader. Two kinds qualify:

1. Context lost in translation: wordplay, idioms, untranslatable expressions, honorific or register nuances, cultural references whose force the rendering cannot reproduce.
2. Context the source reader has implicitly: allusions whose origin (historical, mythological, literary), place or artifact background, or cultural practice the {{target_lang}} reader does not share, where a sentence of background measurably enriches the scene.

For each one: explain what is lost or what background is missing, and judge whether a {{target_lang}} reader without {{source_lang}} cultural background would genuinely miss it.

Attach notes only where that threshold is real — do not annotate anything the reader can infer from context or that is common knowledge. When you are unsure, include the entry and mark its threshold "low"; low-threshold entries are discarded automatically.

Standing exceptions — these ALWAYS meet the threshold at their first occurrence in the chapter:
- A measurement unit the translation keeps in transliteration (li, zhang, catty, shichen, and the like): note it with its rough metric or Imperial equivalent (category "unit").
- A glossary rendering kept in transliteration, or a culture-bound term whose pinned rendering drops nuance the source reader gets (jianghu, dantian, qi, cultivation realm names, and the like): note what the term literally means or carries.

How to write each note:
- A note must ADD information the reader cannot get from the translation: the literal meaning of the source term, the mechanism of the wordplay, the cultural or historical background. Never restate or paraphrase the translated line.
- For wordplay: give the source term, its literal meaning(s), how the wordplay works, and what the translation did instead.
- For honorifics: a shift in how one character addresses another (an honorific dropped, added, or replaced) can signal a relationship change even though each term alone is simple; note the shift and what it signals.

If more than {{max_notes}} entries qualify, order entries by severity of context loss — wordplay and idioms first, then cultural references and allusions, then honorific nuances — and stop at {{max_notes}} entries; never compress by thinning the explanations.

Return ONE JSON object: {"notes": [{"line": <0-based index into the Translation lines>, "term": "<the term in {{source_lang}} being annotated>", "note": "<1-2 sentence explanation in {{target_lang}}>", "category": "cultural" | "idiom" | "wordplay" | "honorific" | "unit" | "other", "threshold": "high" or "low"}]}; at most {{max_notes}} entries; return {"notes": []} if nothing qualifies.
