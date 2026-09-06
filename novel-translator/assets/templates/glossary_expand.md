You are growing the glossary of a long {{source_lang}} -> {{target_lang}} novel translation. Glossary terms must keep exactly the same translation in every chapter, so only terms whose translation could plausibly drift belong in it.

A term qualifies if and only if it is a NAMED ENTITY or a NAMED ACTION: an expression the story uses for one specific thing. Common nouns name a class of things, translate by context, and never qualify — no matter how often they recur or how central the scene is.

Below, the source lines (JSON array of strings, {{source_lang}}) and the translation lines (JSON array of strings, {{target_lang}}) are the same chapter, line for line. Compare them and propose NEW glossary terms that are named entities or named actions: personal names and their aliases, titled positions, place names, organization/sect names, named artifacts, technique & skill names, named cultivation realms or power states, and honorifics (fixed forms of address).

Do NOT propose terms already present in the glossary:

{{glossary}}

Source lines:

{{source_lines}}

Translation lines:

{{translation_lines}}

Constraints:

- Every proposed source term must literally appear in the source lines above; its translation must be the rendering actually used in the translation lines above.
- Apply one test to every candidate: does the source expression name ONE specific referent in the story — this person, this place, this sect, this sword, this technique — or a CLASS of things? Class nouns such as 麦穗 ("wheat stalks"), 剑 ("sword"), or 雨 ("rain") are NEVER terms. When such a concept carries an in-story proper name — a technique called "Wheat-Gathering Palm", a sword named "Autumn Rain" — the NAME qualifies; the common noun it contains does not.
- Named entities are ALWAYS drift-prone: propose every distinct personal name, nickname, or alias; place name; organization/sect name; titled position (e.g. "Steward Li"); named artifact; named technique or skill; and named realm or power state that appears in this chapter and is not already in the glossary — even if its rendering seems obvious or it appears only once (it may recur in later chapters with a different rendering).
- Honorifics and other fixed forms of address qualify without being proper names — their rendering must stay uniform across chapters.
- Do NOT propose common nouns or verbs, everyday vocabulary, pronouns, unnamed objects (flora, fauna, food, weather, clothing, unnamed weapons), or transient phrases. Frequency is not qualification.
- If a term has recurring short forms or nicknames in the source (e.g. a full name and its short form) that you render with the SAME translation, do NOT propose them separately: list them in that term's "variants" array.
- At most {{max_terms}} terms.
- An empty list is only correct when the chapter contains NO named entity, named action, or honorific beyond those already in the glossary.

Return ONE JSON object: {"terms": [{"source": "...", "variants": ["..."], "translation": "...", "definition": "<one-sentence {{target_lang}} explanation>", "category": "<one of: place|person|org|skill|technique|level|state|item|honorific|other>"}]} ("variants" may be an empty array); return {"terms": []} if nothing qualifies.
