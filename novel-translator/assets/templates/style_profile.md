You are analyzing a {{source_lang}} novel to prepare for its translation into {{target_lang}}.

[Source Sample]

{{sample_text}}

[Task]

Based only on the sample above, produce a translation style profile for this novel. Judge the genre, narrative voice, tone, register, dialogue style, and any stylistic conventions worth preserving (for example the author's asides or notes to readers).

Return ONE JSON object:
{"style_summary": "<one sentence in English describing the prose style, tone, and register a translator should match>", "background": "<2-4 sentences in English describing the novel's genre, narrative voice, setting, and stylistic conventions, written as guidance for a translator>"}

Output ONLY the JSON object — no code fences, no explanations.
