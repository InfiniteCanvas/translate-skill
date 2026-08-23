You are attaching translation notes (TNs) for {{target_lang}} readers of a {{source_lang}} novel. A note is justified only when the reader would otherwise miss something.

Source lines:

{{source_lines}}

Translation lines:

{{translation_lines}}

Attach TNs that genuinely help a {{target_lang}} reader:

- untranslatable puns or wordplay
- cultural or historical references
- honorific nuances
- idioms whose literal meaning matters
- meta context the reader cannot infer

Be sparse — at most {{max_notes}} notes; do not annotate anything a reader can infer.

Each note attaches to one line of the translation: "line" is the 0-based index into the translation lines above.

Return ONE JSON object: {"notes": [{"line": <int>, "term": "<the term in {{source_lang}} being annotated>", "note": "<1-2 sentence explanation in {{target_lang}}>"}]}; return {"notes": []} if none are needed.
