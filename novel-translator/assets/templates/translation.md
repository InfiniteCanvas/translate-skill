### Terminology

Reference the following translations:

{{glossary}}

### Task

Translate the user-facing text within the following JSON data into {{target_lang}}, taking the provided background information into consideration.

The chapter title to translate: "{{chapter_title}}"

{{chunk_info}}
{{background_section}}
### Strict Rules

1. Structure Preservation: You MUST preserve the original JSON structure exactly — an array of {"i", "t"} objects with EXACTLY {{line_count}} entries, in the same order. NEVER merge, split, drop, add, or reorder lines. A line whose input text "t" is empty must be returned with an empty "t".
2. Strict Non-Translation: NEVER alter the "i" values or the key names. Translate ONLY the "t" values.
3. Terminology: wherever a term from the reference translations above appears, render it exactly as given, adapted naturally for grammar (plurals, possessives). Never copy the reference markup itself into the translation.
4. Style: the translation style must strictly conform to [{{style}}].
5. Delimiters: retain the exact same number of separator lines (……), bracketed notes (【...】), and standalone punctuation — each on its own line, rendered equivalently. Strictly do not omit, escape, or translate these symbols, and pay close attention to their placement.
6. Inline markdown formatting (**bold**, *italic*, `code`) is preserved on the translated words.
7. Output ONLY the JSON object {"title": "<translated chapter title>", "lines": [{"i": <the SAME i as the input line>, "t": "<translation>"}]} — no text before or after it, no code fences, no explanations.

{{feedback_section}}
### Source Data

{{source_lines}}
