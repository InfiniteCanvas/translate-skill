You maintain the running story recap of a {{source_lang}} novel being translated into {{target_lang}}.

[Recap so far]

{{previous_recap}}

[This chapter — "{{chapter_title}}"]

{{chapter_text}}

[Task]

Condense the recap so far and this chapter into ONE running recap of at most 120 words, in {{target_lang}}, third person. Keep what later chapters may call back to:
- plot beats and their outcomes
- new characters, and how they relate to those already known
- changes in relationships, status, or allegiance
- revealed world facts, promises, and foreshadowing

Drop detail later chapters are unlikely to reference. No style commentary, no translator's notes, no quotations. Plain narrative prose.

Return ONE JSON object: {"recap": "<the updated running recap>"} and nothing else.
