You are reviewing a translated chapter of a {{source_lang}} novel for cultural adaptation.

[Background Information]

{{background_section}}

[Source Text]

{{source_lines}}

[Translation]

{{translation_lines}}

[Task]

Identify cultural references, idioms, wordplay, allusions, honorific nuances, and untranslatable expressions in the source that may not survive the translation above. For each one: explain it, and judge whether the reference would be lost on a {{target_lang}} reader without {{source_lang}} cultural background.

Attach notes only where that comprehension threshold is real — do not annotate anything the reader can infer from context or that is common knowledge. When you are unsure, include the entry and mark its threshold "low"; low-threshold entries are discarded automatically.

Return ONE JSON object: {"notes": [{"line": <0-based index into the Translation lines>, "term": "<the term in {{source_lang}} being annotated>", "note": "<1-2 sentence explanation in {{target_lang}}>", "threshold": "high" or "low"}]}; at most {{max_notes}} entries; return {"notes": []} if nothing qualifies.
