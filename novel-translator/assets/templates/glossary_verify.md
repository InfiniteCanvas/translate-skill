You are checking whether a project's pinned glossary rendering MISTRANSLATES part of a {{source_lang}} novel translated into {{target_lang}}.

A project builds a glossary as it translates. Each entry pins one rendering for a term, and every later chapter is translated against that pinning. The pinning is a project decision and normally correct. It fails in one specific way: a term that means one thing where the entry was seeded means something ELSE somewhere else in the same book, and forcing the pinned rendering there produces a translation that is wrong.

Glossary drift is exactly "words meaning different things in different contexts". You are therefore given ONE term and several CONTEXTS it appears in, possibly spread across different chapters, and you judge them AGAINST EACH OTHER. A sense that is ordinary in one context and absent from the pinned rendering is only visible by comparison.

This batch holds {{context_count}} context(s) of that term.

<term>

{{term}}

</term>

<contexts>

{{contexts}}

</contexts>

Each context is a window of lines around one place the term occurs. The source lines are numbered, and the translation lines carry THE SAME numbers, because the translation is line-for-line with its source: line N in one is line N in the other. Use them together. A window can show the same term meaning different things within a single chapter.

That is the only thing you are deciding. Answer, for each CONTEXT's chapter given below:

> In that chapter, does forcing the term to be rendered as its pinned value produce a mistranslation?

Judge it as follows.

1. READ THE SOURCE AND TRANSLATION LINES. They are the evidence. A term is safe in a chapter when every place that chapter uses it means what the pinned rendering means. It is a mistranslation when the chapter uses the term in a sense the pinned rendering does not cover -- a different referent, a homonym, a title versus a common noun, a technical sense versus an ordinary one.
2. COMPARE THE CONTEXTS WITH EACH OTHER. This is where the real evidence is. One chapter rendering the term faithfully while another uses it for a different referent is the finding this job exists to catch, and it cannot be seen from a single chapter alone.
3. THE COUNTS ARE A QUESTION-GENERATOR, NOT A CHARGE. Each context reached you because the translation rendered the pinned form fewer times than a coverage floor. That happens constantly for innocent reasons: English does not repeat a noun where Chinese repeats it, and a term was legitimately paraphrased or rendered as a pronoun. Low usage alone is NEVER evidence of drift. Say so plainly rather than inventing a sense to justify the flag.
4. AN UNUSED TERM IS NOT A DRIFTED TERM. If the translation simply chose a different but correct rendering, the answer is false -- the glossary may still be the wrong pin for this context, but nothing about that chapter is mistranslated today. You are auditing the chapter's correctness under the pinning, not auditing the glossary's usefulness.
5. DO NOT FLAG STYLE. Register, word choice, and how natural the target text reads are out of scope. Only a wrong meaning counts.
6. WHEN IN DOUBT, ANSWER FALSE. A false confirmation marks the chapter for human review and schedules a re-translation. An ordinary correct translation being sent back costs real work.

Return ONLY this JSON object, one verdict per chapter you were given a context for:

{
  "verdicts": [
    {"source": "<the term, exactly as given above>", "chapter": "<the chapter file name, exactly as given in its context>", "mistranslates": true or false, "reason": "<one or two sentences: what that chapter does with this term, and why that does or does not collide with the pinned rendering>"}
  ]
}