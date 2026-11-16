You are writing the STORY CONTEXT of the glossary terms of a {{source_lang}} -> {{target_lang}} novel translation. Every term already has a fixed rendering; its context is the one sentence that tells a later chapter how THIS story uses it, so that the rendering does not drift.

Below are the terms that appear in the chapter below and have no context yet, each with the lines it appears in.

{{terms}}

Source lines ({{source_lang}}):

{{source_lines}}

Translation lines ({{target_lang}}):

{{translation_lines}}

For each term, write ONE sentence in {{target_lang}} that says three things: how this story uses the term, who uses it, and what else in the story it must not be confused with. Write it from what the lines show - the reader of the translation needs this sentence and nothing else. If the lines do not show enough to say all three, leave that term out.

Return ONE JSON object: {"contexts": [{"source": "...", "context": "..."}]}. Return {"contexts": []} if nothing here shows enough.
