You are curating the translation glossary for a {{source_lang}} novel being translated into {{target_lang}}.

The balance check flagged these glossary terms as drift signals: the canonical English rendering never appears in the translation even though the source term appears in the source text.

[Flagged Terms]

{{term_list}}

[Sample Source Lines]

{{sample_lines}}

[Task]

For each flagged term, decide whether it belongs in a translation glossary. A term belongs if and only if it is a named entity or a named action — an expression the story uses for one specific thing:
- KEEP named entities and named actions: personal names and their aliases, places, sects/organizations, techniques and skills, named artifacts, named cultivation realms or power states — plus titles bound to a name ("Empress Dowager Zhao", "Steward Li").
- REMOVE everything else: common nouns or verbs, everyday words, generic or unnamed objects, pronouns, transient phrases (e.g. 麦穗 "wheat stalks"), and standalone titles, kinship terms, or other ways one character addresses another ("great grandmother", "Empress Dowager") — delete them even when they recur often, refer to one specific person, or have so far translated consistently.

Return ONE JSON object:
{"decisions": [{"source": "<term exactly as listed>", "keep": true or false, "reason": "<short English reason>"}]}

Output ONLY the JSON object — no code fences, no explanations.
