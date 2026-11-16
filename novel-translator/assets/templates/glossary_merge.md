Two versions of a glossary entry for the same term are given. Existing entry (already used in published chapters):

{{existing_json}}

Newly proposed entry:

{{proposed_json}}

Merge them into ONE canonical entry:

- Keep the existing translation unless the proposed translation is clearly a correction of an error.
- Merge the definitions into the single most informative one-sentence definition.
- Merge the story contexts into one sentence that says how THIS story uses the term, who uses it, and what it is not to be confused with. Keep the existing context unless the proposed one corrects it.
- Keep the most specific valid category.

Return ONE JSON object: {"source": "...", "translation": "...", "definition": "...", "context": "...", "category": "..."}. The "context" may be an empty string when neither version carries one.
