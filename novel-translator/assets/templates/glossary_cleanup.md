You are curating the translation glossary for a {{source_lang}} novel being translated into {{target_lang}}.

The balance gate flagged these glossary terms: the canonical English rendering never appears in the translation even though the source term appears in the source text.

[Flagged Terms]

{{term_list}}

[Sample Source Lines]

{{sample_lines}}

[Task]

For each flagged term, decide whether it belongs in a translation glossary:
- KEEP terms whose consistent rendering matters across chapters: names of people, places, sects, organizations, techniques, titles, artifacts, cultivation realms, or culturally loaded concepts.
- REMOVE mundane terms: everyday words, common nouns or verbs, generic objects, or transient phrases whose natural translation legitimately varies by context — these do not need enforced consistency.

Return ONE JSON object:
{"decisions": [{"source": "<term exactly as listed>", "keep": true or false, "reason": "<short English reason>"}]}

Output ONLY the JSON object — no code fences, no explanations.
