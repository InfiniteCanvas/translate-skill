You are auditing ONE pinned glossary rendering against the chapter whose translation was just rejected for faithfulness, to decide whether the ENTRY is the fault and what to do about it.

A project builds a glossary as it translates. Each entry pins ONE rendering for a source term, and every chapter is translated against that pinning. The pinning normally is correct. It fails in one specific way: the entry freezes a sense the source does not always carry -- a homograph, an idiom that contains the term, a common word the entry froze as a name -- and then every translation that OBEYS the pin is unfaithful. No retranslation can fix that, because the constraint travels with the entry: the fix is to the glossary, not to the translation.

You are given the term (its pinned rendering and definition) and the CONTEXTS where it occurs in this chapter.

<term>

{{term}}

</term>

<contexts>

{{contexts}}

</contexts>

Each context is a span of lines around one or more places the term occurs in this chapter. The source lines are numbered, and the translation lines carry THE SAME numbers, because the translation is line-for-line with its source: line N in one is line N in the other. Use them together. This batch holds {{context_count}} context(s) covering {{hit_count}} occurrence(s) of that term.

The question, for this chapter:

> Does the pinned rendering fail to cover the sense the SOURCE uses the term in here -- so that forcing the pin would produce a mistranslation?

Judge it as follows.

1. THE SENSE TEST IS SOURCE AGAINST PIN. Read the source lines: what does the term mean at each occurrence in this chapter? A term is over-bound here when the source's use does not mean what the pinned rendering means -- a different referent, a homograph, an idiom containing the term, a title versus a common noun, a technical sense versus an ordinary one. It is safe when every occurrence in this chapter carries the pinned sense.
2. THE TRANSLATION IS EVIDENCE, NOT THE VERDICT. A translation that avoided the pin and rendered the sense naturally shows the pin does not fit. A translation that obeyed the pin and reads wrong confirms it. But the translation is one translator's response and may itself be wrong; the entry is judged against the source.
3. THE COUNTS ARE A QUESTION-GENERATOR, NOT A CHARGE. Each context reached you because the translation rendered the pinned form fewer times than a coverage floor. That happens constantly for innocent reasons: English does not repeat a noun where the source language repeats it, and a term was legitimately paraphrased or rendered as a pronoun. Low usage alone is NEVER evidence that the entry is at fault.
4. DO NOT FLAG STYLE. Register, word choice, and how natural the target text reads are out of scope. Only a wrong meaning counts.
5. WHEN IN DOUBT, ANSWER FALSE. A true verdict with a retire action deletes a glossary entry that every later chapter would otherwise be translated against; a wrong retirement is expensive to notice and undo.

When "mistranslates" is true, you must ALSO return the remedy as "action":

- "retire" -- the ENTRY is what is wrong: the pinned sense does not cover any occurrence in this chapter, so no translation under this entry can be faithful here. Deleting the entry lets the term translate naturally again; where the term is legitimately a name of something specific, a longer compound that names that thing usually already has its own entry.
- "keep" -- the fault is narrower than the entry: the pinning is defensible for the term but this chapter needs an alternative rendering, a definition tweak, or the source itself is special. That is a human decision, not an automatic deletion. When in doubt between the two, "keep".

Return ONLY this JSON object, one verdict per chapter you were given a context for:

{
  "verdicts": [
    {"source": "<the term, exactly as given above>", "chapter": "<the chapter file name, exactly as given in its context>", "mistranslates": true or false, "action": "retire" or "keep", "reason": "<one or two sentences: what the source does with this term here, and why the pin does or does not cover it>"}
  ]
}
