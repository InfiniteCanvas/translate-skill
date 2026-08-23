You are growing the glossary of a long {{source_lang}} -> {{target_lang}} novel translation. Glossary terms must keep exactly the same translation in every chapter, so only terms whose translation could plausibly drift belong in it.

Below, the source lines (JSON array of strings, {{source_lang}}) and the translation lines (JSON array of strings, {{target_lang}}) are the same chapter, line for line. Compare them and propose NEW glossary terms whose translation must stay consistent across a long novel: place names, personal names/aliases/titles, technique & skill names, organization/sect names, cultivation realms or power states, significant items, honorifics.

Do NOT propose terms already present in the glossary:

{{glossary}}

Source lines:

{{source_lines}}

Translation lines:

{{translation_lines}}

Constraints:

- Every proposed source term must literally appear in the source lines above; its translation must be the rendering actually used in the translation lines above.
- Recurring proper nouns are ALWAYS drift-prone: propose every distinct personal name, nickname, or alias; place name; organization/sect name; and titled position (e.g. "Steward Li") that appears in this chapter and is not already in the glossary — even if its rendering seems obvious or it appears only once (it may recur in later chapters with a different rendering).
- If a term has recurring short forms or nicknames in the source (e.g. a full name and its short form) that you render with the SAME translation, do NOT propose them separately: list them in that term's "variants" array.
- Also propose distinctive technical terms, techniques, realms, and items whose rendering must stay fixed. Skip common vocabulary, pronouns, and everyday words.
- At most {{max_terms}} terms.
- An empty list is only correct when the chapter contains NO proper noun or fixed-rendering term beyond those already in the glossary.

Return ONE JSON object: {"terms": [{"source": "...", "variants": ["..."], "translation": "...", "definition": "<one-sentence {{target_lang}} explanation>", "category": "<one of: place|person|org|skill|technique|level|state|item|honorific|other>"}]} ("variants" may be an empty array); return {"terms": []} if nothing qualifies.
