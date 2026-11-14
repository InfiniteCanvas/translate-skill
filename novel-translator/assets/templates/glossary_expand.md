You are growing the glossary of a long {{source_lang}} -> {{target_lang}} novel translation. Glossary terms must keep exactly the same translation in every chapter, so only terms whose translation could plausibly drift belong in it.

A term qualifies if it is one of TWO kinds.

**Kind 1 — named entities and named actions.** An expression the story uses for one specific thing: a personal name or alias, a place name, an organization or sect name, a named artifact, a named technique or skill, a named cultivation realm or power state, or a title bound to a name ("Empress Dowager Zhao", "Steward Li").

**Kind 2 — recurring story terms.** An expression this story keeps using for one specific recurring thing, even when its {{source_lang}} words are ordinary vocabulary and it is not formally a "name": a place the characters return to, an object or place that recurs, a mechanism of the plot, a role the story keeps casting, or a phrase the story has fixed an idiom for. Kind 2 is what the strict named-entity-only rule used to lose: the terms that carry a fixed rendering through a whole book are often ordinary words ("the side room", "the supermarket", "the original owner", "the little world"), and they drift exactly like names do — one chapter says "wing room", the next says "side room", and the reader stops trusting the prose.

Kind 2 has two extra tests, because this is where the noise comes from:

- **Could a translator have chosen differently?** A term with only one possible English rendering cannot drift and needs no pinning. "Wing room" and "side room" and "side chamber" are all defensible for 厢房, so 厢房 qualifies; "supermarket" is not a choice, so the word supermarket does not qualify on its own.
- **Will the story keep using it?** A one-off detail in this chapter is not a recurring term. The novel-wide occurrence count is checked by the pipeline, not by you, so propose a recurring term even if it appears only ONCE here — but only when this chapter shows it as something the story returns to (a room she sleeps in, a home she lives in, a school she attends, a system she uses, a thing the plot turns on).

And one test that keeps the category from becoming a category list:

- **Is it a TYPE or a TYPE-INSTANCE?** A type does not qualify, however often it recurs: a room type (the outer room, the small kitchen, the bedroom), a facility type (the cafeteria, the teaching building, the auditorium), a vehicle type (the black car), a role many people hold (the homeroom teacher, the instructor, the counselor), a general idea (the noise, public opinion, the college entrance exam). The question is not "does the story return to it" but "does the story mean ONE specific one": the story has a FIRST cafeteria and a SECOND cafeteria, so "the cafeteria" is a type and the pin would be wrong half the time; the dormitory she sleeps in is one specific one, so it qualifies. A type that the story never distinguishes among is still a type — if you cannot point to the ONE thing the story means, it does not qualify.

Below, the source lines (JSON array of strings, {{source_lang}}) and the translation lines (JSON array of strings, {{target_lang}}) are the same chapter, line for line. Compare them and propose NEW glossary terms of either kind: personal names and their aliases, place names, organization/sect names, named artifacts, technique & skill names, named cultivation realms or power states, and titles bound to a name ("Empress Dowager Zhao", "Steward Li"); plus the recurring story terms of Kind 2.

Do NOT propose terms already present in the glossary:

{{glossary}}

Source lines:

{{source_lines}}

Translation lines:

{{translation_lines}}

Constraints:

- Every proposed source term must literally appear in the source lines above; its translation must be the rendering actually used in the translation lines above.
- Apply the class test to every Kind 1 candidate: does the source expression name ONE specific referent in the story — this person, this place, this sect, this sword, this technique — or a CLASS of things? Class nouns such as 麦穗 ("wheat stalks"), 剑 ("sword"), or 雨 ("rain") are NEVER terms. When such a concept carries an in-story proper name — a technique called "Wheat-Gathering Palm", a sword named "Autumn Rain" — the NAME qualifies; the common noun it contains does not.
- Named entities are ALWAYS drift-prone: propose every distinct personal name, nickname, or alias; place name; organization/sect name; title bound to a name (e.g. "Steward Li", "Empress Dowager Zhao"); named artifact; named technique or skill; and named realm or power state that appears in this chapter and is not already in the glossary — even if its rendering seems obvious or it appears only once (it may recur in later chapters with a different rendering).
- A title qualifies ONLY when bound to a name: "Empress Dowager Zhao" qualifies, "Empress Dowager" alone does not. What one character calls another — kinship terms and other ways of address such as "great grandmother", "big brother", "my lord" — do NOT qualify on their own, because which address is used depends on the speaker and the scene. They DO qualify when the story fixes one form for one specific character and keeps using it — the housekeeper everyone calls 陈姨, rendered "Auntie Chen" every time — in which case propose the character's NAME, with that address as a variant.
- Kind 2 candidates must pass BOTH extra tests above (a translator could have chosen differently, and the story will keep using it) AND the type test (it names one specific thing, not a category of things). Ordinary vocabulary that merely recurs does NOT qualify: a common noun, a verb, an everyday object, a pronoun, a transient phrase, a generic noun ("rice", "supermarket" as a category, "fish pond", "the cafeteria") is still noise unless the story has fixed a specific referent for it in this book.
- If a term has recurring short forms or nicknames in the source (e.g. a full name and its short form) that you render with the SAME translation, do NOT propose them separately: list them in that term's "variants" array.
- At most {{max_terms}} terms.
- An empty list is only correct when the chapter contains NO named entity or named action beyond those already in the glossary AND no recurring story term that passes both extra tests.

Return ONE JSON object: {"terms": [{"source": "...", "variants": ["..."], "translation": "...", "definition": "<one-sentence {{target_lang}} explanation>", "category": "<one of: place|person|org|skill|technique|level|state|item|honorific|other>"}]} ("variants" may be an empty array); return {"terms": []} if nothing qualifies.
