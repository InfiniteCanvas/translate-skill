"""Re-evaluate translator's notes on already-translated chapters.

The `tn` subcommand's engine. Per chapter (manifest order): run the
annotator fresh over the source/translation pair (same tn_generate.md
template, guided schema, and [Background Information] frame -- novel
background plus the predecessor chapter's rolling story recap -- as the
pipeline's TN_GENERATE) and dedup through
tn.process (same gap rule, history threading, and max_notes cap as
TN_DEDUP, with history saved after each successfully processed chapter),
then write the result to the notes/<stem>.json sidecar and the discarded
candidates to notes/<stem>.dropped.json. Legacy chapters whose notes are
still baked
into the markdown ("[^N]" markers + a "## Translator's Notes" section) are
detected up front, but the clean-markdown rewrite lands only after the
annotator succeeded -- a failed run leaves a legacy chapter byte-unchanged
(baked notes are the only copy on disk until the sidecar exists).
Annotator failures keep the existing notes/sidecar/history and never abort
the run -- the caller reports the failed files. A "successful" evaluation
that keeps ZERO notes over a chapter with a non-empty baseline counts as a
failure too: save_notes unlinks an empty sidecar (and the migration rewrite
would strip the only baked copy first), so the existing notes must win.
Eligibility problems (not translated, missing files) skip per chapter
instead of raising.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from lib import client, glossary, pipeline, project, replace, story, tn, vcs
from lib.pipeline import fill


def _recap_part(recap_state: dict, manifest: list[dict], file: str) -> str:
    """The [Background Information] frame's recap part for re-checking
    `file`: the PREDECESSOR chapter's stored recap rendered through
    story.story_part -- exactly what the pipeline's TN_GENERATE injects for
    this chapter -- or "" (first chapter, no state yet, no stored entry).
    Read-only: no LLM call, no backfill, no state writes; any surprise
    (malformed manifest, broken entry) degrades silently to "" so one bad
    chapter never aborts the run."""
    try:
        prev = story.predecessor(manifest, file)
        if prev is None:
            return ""
        entry = recap_state.get("chapters", {}).get(Path(prev).stem)
        if (isinstance(entry, dict) and isinstance(entry.get("recap"), str)
                and entry["recap"].strip()):
            return story.story_part(entry["recap"])
        return ""
    except Exception:  # noqa: BLE001 - advisory: silent per-chapter degrade
        return ""


def make_chat(project_dir: Path, cfg: dict,
              chapter: str) -> Callable[[str], str]:
    """A chapter-bound annotator call.

    Built per chapter, not once: cmd_tn never enters run_chapter, so a single
    closure could not know which chapter it was annotating, and every
    chapter's prompt/response would land in the project bucket with no chapter
    tag to attribute it to."""
    def chat(prompt: str) -> str:
        return pipeline._chat(
            project_dir, cfg, "annotator", prompt,
            json_schema=pipeline.NOTES_SCHEMA, chapter=chapter,
        )
    return chat


def _note_signatures(notes: list[dict]) -> list[tuple]:
    """Comparable form of a note set: (line, term, note, category) per
    entry, so 'changed' means the sidecar content really differs (a
    reworded note, a moved line, or a pure category change counts), not
    just a rewritten updated_at."""
    return [
        (
            note.get("line"),
            str(note.get("term", "")).strip(),
            str(note.get("note", "")).strip(),
            str(note.get("category", "")).strip(),
        )
        for note in notes
    ]


def recheck_chapters(
    project_dir: Path,
    manifest: list[dict],
    files: list[str],
    cfg: dict,
    dry_run: bool = False,
    chat: Callable[[str], str] | None = None,
) -> dict:
    """Re-run the annotator over the given translated chapters.

    chat overrides the LLM call (tests pass a stub); the default keeps the
    pipeline's per-project llm trace logging. dry_run performs the full
    evaluation (LLM calls included) but writes nothing -- not the legacy
    migration rewrite, not the sidecar, not tn_history.json. Returns
    {"scanned", "changed", "migrated", "notes_before", "notes_after",
    "failed": [files], "skipped": [files], "dry_run": bool}; note counts
    are totals across chapters (failed chapters count in neither, exactly
    like the annotator-exception path).
    """
    paths = project.paths(project_dir)
    prefix = "[dry-run] " if dry_run else ""

    def default_chat_for(chapter: str) -> Callable[[str], str]:
        return make_chat(project_dir, cfg, chapter)

    # Run-level setup: the template, config knobs, and novel background are
    # the same for every chapter, so load them once instead of per chapter.
    # The glossary too: re-annotation never grows it, so one load serves the
    # whole run (the per-chapter contextual slice is computed per chapter).
    tpl = pipeline._load_template(paths["templates"], "tn_generate.md")
    max_notes = int(pipeline._cfg_value(cfg, "max_notes_per_chapter"))
    gap = int(pipeline._cfg_value(cfg, "tn_gap_chapters"))
    keep_low = bool(pipeline._cfg_value(cfg, "tn_keep_low_confidence"))
    glossary_cap = int(pipeline._cfg_value(cfg, "contextual_glossary_cap"))
    g = glossary.load(project_dir)

    novel_info = project.load_novel_info(project_dir)
    style_profile = novel_info.get("style_profile")
    style_profile = style_profile if isinstance(style_profile, dict) else {}
    novel_background = str(
        novel_info.get("background") or style_profile.get("background") or ""
    ).strip()

    # Recap parity with the pipeline's TN_GENERATE: the re-check's
    # annotator must judge the same [Background Information] frame (novel
    # background + the predecessor's rolling recap), or the two annotators
    # evaluate different contexts. READ-ONLY: the state is loaded once per
    # run, never written, and never backfilled (no LLM call); story.
    # load_state already warns on a malformed file, and anything else
    # unexpected degrades to a recap-less run.
    try:
        recap_state = story.load_state(project_dir)
    except Exception as exc:  # noqa: BLE001 - advisory: run without recaps
        print(f"[warn] story_state.json unloadable ({exc}) - runs without recap")
        recap_state = {}

    # Threaded across chapters in order and saved after each successfully
    # processed one, exactly like the pipeline's TN_DEDUP per chapter.
    history = tn.load_history(project_dir)
    # tn.process bumps times/last_order for every kept note even when the
    # note text is unchanged, so a history-only drift must count as a
    # change or the run would dirty the worktree without committing.
    history_before = json.dumps(history, sort_keys=True, ensure_ascii=False)

    scanned = 0
    changed = 0
    migrated = 0
    notes_before = 0
    notes_after = 0
    failed: list[str] = []
    skipped: list[str] = []

    # Eligibility: skipped chapters warn and continue, never abort the run.
    eligible: list[tuple[dict, str]] = []
    for file in files:
        entry = project.find_entry(manifest, file)
        if entry is None:
            print(f"[warn] {file}: no manifest entry")
            skipped.append(file)
            continue
        status = entry.get("status")
        if status != "translated":
            print(f"[warn] {file}: not translated (status: {status}) - skipped")
            skipped.append(file)
            continue
        if not (paths["translated"] / file).is_file():
            print(f"[warn] translated/{file} listed as translated but missing")
            skipped.append(file)
            continue
        if not (paths["source"] / file).is_file():
            print(f"[warn] source chapter missing: {file}")
            skipped.append(file)
            continue
        eligible.append((entry, file))
    eligible.sort(key=lambda pair: int(pair[0].get("order", 0)))

    for entry, file in eligible:
        translated_path = paths["translated"] / file
        source_path = paths["source"] / file
        chapter_order = int(entry.get("order", 0))
        scanned += 1

        # ---- load chapters + legacy detection ----
        try:
            _fm, body = project.read_chapter(translated_path)
            fm_s, src_body = project.read_chapter(source_path)
        except (ValueError, OSError) as exc:
            print(f"[tn] {file}: [warn] cannot read chapter - skipped: {exc}")
            failed.append(file)
            continue
        clean_body, baked = tn.strip_marked_notes(body)
        body_lines = clean_body.split("\n")
        did_migrate = bool(baked) or clean_body != body
        baseline = baked if did_migrate else tn.load_notes(project_dir, file)

        # ---- fresh annotator pass (the re-evaluation) ----
        # Same leading-title drop as the pipeline: a body line repeating the
        # frontmatter chapter_title was consumed by the title field.
        source_lines, _dropped = project.drop_leading_chapter_title(
            src_body.split("\n"), fm_s
        )

        # Same glossary frame the pipeline's build_ctx gives tn_generate.md:
        # the chapter's contextual slice rendered as Hy-MT2 pairs, so the
        # annotator can tell pinned renderings from translation errors.
        glossary_str = glossary.render_contextual(
            glossary.contextual(g, "\n".join(source_lines), glossary_cap)
        )

        # Same [Background Information] frame the pipeline's
        # background_section() builds: novel background, then the
        # predecessor's rolling recap ("" when there is none).
        recap_part = _recap_part(recap_state, manifest, file)
        chapter_background = pipeline.background_section(novel_background, recap_part)

        try:
            do_chat = chat if chat is not None else default_chat_for(file)
            prompt = fill(
                tpl,
                {
                    "source_lang": pipeline._lang_name(cfg.get("source_lang", "")),
                    "target_lang": pipeline._lang_name(cfg.get("target_lang", "")),
                    "source_lines": json.dumps(source_lines, ensure_ascii=False),
                    "translation_lines": json.dumps(body_lines, ensure_ascii=False),
                    "background_section": chapter_background,
                    "glossary": glossary_str,
                    "max_notes": max_notes,
                },
                "tn_generate.md",
            )
            resp = do_chat(prompt)
            data = client.extract_json(resp)
            raw_notes = data.get("notes") if isinstance(data, dict) else None
            if not isinstance(raw_notes, list):
                raise ValueError("expected a 'notes' array")
        except Exception as exc:  # noqa: BLE001 - keep existing notes, keep going
            print(f"[tn] {file}: [warn] note generation failed - keeping existing notes: {exc}")
            failed.append(file)
            continue

        # ---- dedup + write ----
        # The legacy migration rewrite lands only here, after the annotator
        # succeeded: a failed run must leave a legacy chapter byte-unchanged
        # (its baked notes are the only copy on disk until the sidecar write).
        history_in = history
        kept, history, warnings, dropped = tn.process(
            raw_notes, len(body_lines), chapter_order, history, gap, keep_low,
            max_notes=max_notes,
        )
        for warning in warnings:
            print(f"[tn] {file}: [warn] {warning}")
        # A "successful" evaluation that keeps ZERO notes over a chapter
        # with a non-empty baseline must not destroy it -- before ANY
        # mutation: save_notes unlinks an empty sidecar, and in the
        # migration branch the rewrite below would strip the only baked
        # copy first. Treat it exactly like a failed evaluation: keep
        # sidecar and markdown, keep the threaded history untouched (no
        # tn_history write for this chapter), report the file in `failed`
        # (cmd_tn exits 1). Zero baseline + zero kept stays a benign no-op.
        if not kept and baseline:
            history = history_in
            print(
                f"{prefix}[tn] {file}: annotator returned 0 notes for a "
                f"chapter with {len(baseline)} note(s) - keeping existing sidecar"
            )
            failed.append(file)
            continue
        if not dry_run:
            if did_migrate:
                # Surgical rewrite (replace.py's frontmatter rule): keep the
                # YAML block byte-verbatim, replace only the body. BOM-
                # tolerant: a leading BOM would hide the frontmatter from
                # _split_frontmatter and the rewrite would drop it.
                raw = translated_path.read_text(encoding="utf-8-sig")
                head, _tail = replace._split_frontmatter(raw)
                out = (head + "\n\n" if head else "") + clean_body.rstrip("\n") + "\n"
                project.atomic_write_text(translated_path, out, newline="\n")
            tn.save_history(project_dir, history)
            kept = tn.save_notes(project_dir, file, body_lines, kept)
            tn.save_dropped(project_dir, file, dropped)
        if did_migrate:
            migrated += 1
            print(f"{prefix}[tn] {file}: migrated baked-in notes to sidecar ({len(baked)})")

        # ---- report ----
        before_terms = [str(note.get("term", "")).strip() for note in baseline]
        after_terms = [str(note.get("term", "")).strip() for note in kept]
        bits = []
        added = [term for term in after_terms if term not in before_terms]
        removed_terms = [term for term in before_terms if term not in after_terms]
        if added:
            bits.append(f"+{', '.join(added)}")
        if removed_terms:
            bits.append(f"-{', '.join(removed_terms)}")
        diff = f" ({'; '.join(bits)})" if bits else ""
        if did_migrate or _note_signatures(baseline) != _note_signatures(kept):
            changed += 1
        notes_before += len(baseline)
        notes_after += len(kept)
        print(f"{prefix}[tn] {file}: {len(baseline)} before -> {len(kept)} after{diff}")

    if not dry_run and (
        changed
        or history_before != json.dumps(history, sort_keys=True, ensure_ascii=False)
    ):
        vcs.commit(project_dir, "tn: re-check notes")
    return {
        "scanned": scanned,
        "changed": changed,
        "migrated": migrated,
        "notes_before": notes_before,
        "notes_after": notes_after,
        "failed": failed,
        "skipped": skipped,
        "dry_run": dry_run,
    }
