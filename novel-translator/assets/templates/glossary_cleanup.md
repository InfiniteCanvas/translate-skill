You are curating the translation glossary for a {{source_lang}} novel being translated into {{target_lang}}.

The balance check flagged these glossary terms as drift signals: the canonical English rendering never appears in the translation even though the source term appears in the source text.

[Flagged Terms]

{{term_list}}

[Sample Source Lines]

{{sample_lines}}

[Task]

For each flagged term, decide whether it belongs in a translation glossary. A term belongs if it is one of TWO kinds:

- **Kind 1 — a named entity or a named action**: an expression the story uses for one specific thing — personal names and their aliases, places, sects/organizations, techniques and skills, named artifacts, named cultivation realms or power states — plus titles bound to a name ("Empress Dowager Zhao", "Steward Li").
- **Kind 2 — a recurring story term**: an expression this story keeps using for one specific recurring thing, even when its words are ordinary vocabulary — a place the characters return to, an object or place that recurs, a mechanism of the plot, a role the story keeps casting, a phrase the story has fixed an idiom for (the room she sleeps in, the home she lives in, the system she uses, the body she took over, the world inside her space).

For Kind 2, apply two extra tests before keeping it:

- **Could a translator have chosen differently?** A term with only one possible English rendering cannot drift, so it never needed pinning — that is noise, retire it.
- **Is the drift a DRIFT or a variation?** If the translation renders the term another reasonable way, that is a variation to reconcile, not grounds for deletion: KEEP it, because the entry is what will hold the rendering steady from here. Retire it only when the source lines show it is generic vocabulary with no fixed referent in this story.

- KEEP Kind 1: named entities and named actions, and the Kind 2 recurring story terms that pass both extra tests.
- REMOVE everything else: common nouns or verbs, everyday words, generic or unnamed objects, pronouns, transient phrases (e.g. 麦穗 "wheat stalks"), and standalone titles, kinship terms, or other ways one character addresses another ("great grandmother", "Empress Dowager") — delete them even when they recur often, refer to one specific person, or have so far translated consistently.

Return ONE JSON object:
{"decisions": [{"source": "<term exactly as listed>", "keep": true or false, "reason": "<short English reason>"}]}

Output ONLY the JSON object — no code fences, no explanations.
