"""Re-evaluate translator's notes on already-translated chapters.

The `tn` subcommand's engine. Per chapter (manifest order): run the
annotator fresh over the source/translation pair (same tn_generate.md
template and guided schema as the pipeline's TN_GENERATE) and dedup through
tn.process (same gap rule and history threading as TN_DEDUP, with history
saved after each successfully processed chapter), then write the result to
the notes/<stem>.json sidecar. Legacy chapters whose notes are still baked
into the markdown ("[^N]" markers + a "## Translator's Notes" section) are
detected up front, but the clean-markdown rewrite lands only after the
annotator succeeded -- a failed run leaves a legacy chapter byte-unchanged
(baked notes are the only copy on disk until the sidecar exists).
Annotator failures keep the existing notes/sidecar/history and never abort
the run -- the caller reports the failed files. Eligibility problems
(not translated, missing files) skip per chapter instead of raising.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from lib import client, pipeline, project, replace, tn
from lib.pipeline import fill


def _note_signatures(notes: list[dict]) -> list[tuple]:
    """Comparable form of a note set: (line, term, note) per entry, so
    'changed' means the sidecar content really differs (a reworded note or
    a moved line counts), not just a rewritten updated_at."""
    return [
        (note.get("line"), str(note.get("term", "")).strip(), str(note.get("note", "")).strip())
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
    are totals across chapters.
    """
    paths = project.paths(project_dir)
    prefix = "[dry-run] " if dry_run else ""

    def default_chat(prompt: str) -> str:
        return pipeline._chat(
            project_dir, cfg, "annotator", prompt, json_schema=pipeline.NOTES_SCHEMA
        )

    do_chat = chat or default_chat

    # Run-level setup: the template, config knobs, and novel background are
    # the same for every chapter, so load them once instead of per chapter.
    tpl = pipeline._load_template(paths["templates"], "tn_generate.md")
    max_notes = str(int(pipeline._cfg_value(cfg, "max_notes_per_chapter")))
    gap = int(pipeline._cfg_value(cfg, "tn_gap_chapters"))
    keep_low = bool(pipeline._cfg_value(cfg, "tn_keep_low_confidence"))

    novel_info: dict = {}
    if paths["novel_info"].is_file():
        try:
            loaded = json.loads(paths["novel_info"].read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                novel_info = loaded
        except (ValueError, OSError):
            novel_info = {}
    style_profile = novel_info.get("style_profile")
    style_profile = style_profile if isinstance(style_profile, dict) else {}
    novel_background = str(
        novel_info.get("background") or style_profile.get("background") or ""
    ).strip()
    background_section = (
        "[Background Information]\n" + novel_background + "\n" if novel_background else ""
    )

    # Threaded across chapters in order and saved after each successfully
    # processed one, exactly like the pipeline's TN_DEDUP per chapter.
    history = tn.load_history(project_dir)

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
        source_lines = src_body.split("\n")
        # Same leading-title drop as the pipeline: a body line repeating the
        # frontmatter chapter_title was consumed by the title field.
        first = source_lines[0].strip().strip("\u3000 ") if source_lines else ""
        if first and first == str(fm_s.get("chapter_title", "")).strip().strip("\u3000 "):
            source_lines = source_lines[1:]

        try:
            prompt = fill(
                tpl,
                {
                    "source_lang": pipeline._lang_name(cfg.get("source_lang", "")),
                    "target_lang": pipeline._lang_name(cfg.get("target_lang", "")),
                    "source_lines": json.dumps(source_lines, ensure_ascii=False),
                    "translation_lines": json.dumps(body_lines, ensure_ascii=False),
                    "background_section": background_section,
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
        kept, history, warnings = tn.process(
            raw_notes, len(body_lines), chapter_order, history, gap, keep_low,
        )
        for warning in warnings:
            print(f"[tn] {file}: [warn] {warning}")
        if not dry_run:
            if did_migrate:
                # Surgical rewrite (replace.py's frontmatter rule): keep the
                # YAML block byte-verbatim, replace only the body.
                raw = translated_path.read_text(encoding="utf-8")
                head, _tail = replace._split_frontmatter(raw)
                out = (head + "\n\n" if head else "") + clean_body.rstrip("\n") + "\n"
                project.atomic_write_text(translated_path, out, newline="\n")
            tn.save_history(project_dir, history)
            kept = tn.save_notes(project_dir, file, body_lines, kept)
        if did_migrate:
            migrated += 1
            print(f"{prefix}[tn] {file}: migrated baked-in notes to sidecar ({len(baked)})")

        # ---- report ----
        before_terms = [str(note.get("term", "")).strip() for note in baseline]
        after_terms = [str(note.get("term", "")).strip() for note in kept]
        bits = []
        added = [term for term in after_terms if term not in before_terms]
        dropped = [term for term in before_terms if term not in after_terms]
        if added:
            bits.append(f"+{', '.join(added)}")
        if dropped:
            bits.append(f"-{', '.join(dropped)}")
        diff = f" ({'; '.join(bits)})" if bits else ""
        if did_migrate or _note_signatures(baseline) != _note_signatures(kept):
            changed += 1
        notes_before += len(baseline)
        notes_after += len(kept)
        print(f"{prefix}[tn] {file}: {len(baseline)} before -> {len(kept)} after{diff}")

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
