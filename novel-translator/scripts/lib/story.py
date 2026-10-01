"""Rolling story-so-far recap: cross-chapter plot context for the pipeline.

The glossary pins terminology and the novel background pins the premise,
but nothing told the model translating chapter N what actually HAPPENED in
chapters 1..N-1. This module keeps story_state.json at the project root:
one entry per translated chapter, each holding a <= 120-word running recap
in the target language, regenerated from the previous recap plus the
just-assembled chapter by ONE recap-provider call over recap.md.

- ensure_recap resolves the recap to INJECT when a chapter starts
  translating: the predecessor's stored entry when present (no LLM call),
  else a one-call backfill of the predecessor (self-heal for chapters
  translated before the feature existed, or a crash that landed between
  ASSEMBLE and the record). The backfill's {{previous_recap}} is the
  nearest EARLIER existing entry found by walking the manifest order
  backwards -- never a recursive chain.
- record_recap writes the chapter's OWN entry after ASSEMBLE succeeds,
  unconditionally overwriting it (retranslation refreshes the recap).

Everything here is advisory context, never a gate: any failure prints one
[warn] line and degrades to "no recap" -- a chapter must never fail because
its context summary could not be built. IO follows the tn_history
conventions: BOM-tolerant read, warn-and-fresh on a malformed file, atomic
write with a trailing newline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lib import client, project

RECAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recap": {"type": "string"},
    },
    "required": ["recap"],
    "additionalProperties": False,
}


def _state_path(project_dir: Path) -> Path:
    return Path(project.paths(project_dir)["story_state"])


def load_state(project_dir: Path) -> dict:
    """Load story_state.json ({"chapters": {}} when missing; a malformed
    file is discarded -- recaps start fresh, never a crash -- with one
    [warn] line so the reset is never silent; tn.load_history's
    convention)."""
    path = _state_path(project_dir)
    if not path.is_file():
        return {"chapters": {}}
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        reason = type(exc).__name__
    else:
        if not isinstance(data, dict):
            reason = type(data).__name__
        elif not isinstance(data.get("chapters"), dict):
            reason = "invalid chapters"
        else:
            reason = None
    if reason is not None:
        print(f"[warn] story_state.json unreadable ({reason}) - recaps start fresh")
        return {"chapters": {}}
    return data


def save_state(project_dir: Path, state: dict) -> None:
    """Write story_state.json as pretty UTF-8 JSON with a trailing newline."""
    project.atomic_write_text(
        _state_path(project_dir),
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        newline="\n",
    )


def story_part(recap_text: str) -> str:
    """The [Background Information] frame's recap part: "" when there is no
    recap, else the labeled text injected into every prompt that fills
    {{background_section}} (translate, faithfulness review, note annotator)."""
    if not recap_text:
        return ""
    return "Story so far (auto-generated recap of the preceding chapters):\n" + recap_text


def predecessor(manifest: list[dict], file: str) -> str | None:
    """The manifest-order predecessor chapter file of `file`; None for the
    first chapter (or a file absent from the manifest)."""
    ordered = sorted(manifest, key=lambda e: int(e.get("order", 0)))
    prev: str | None = None
    for entry in ordered:
        name = entry.get("file")
        if name == file:
            return prev
        prev = name
    return None


def _entry_recap(state: dict, stem: str) -> str | None:
    """The stored recap for a chapter stem; None when absent or malformed
    (a broken entry counts as missing, so the backfill can repair it)."""
    entry = state.get("chapters", {}).get(stem)
    if (isinstance(entry, dict) and isinstance(entry.get("recap"), str)
            and entry["recap"].strip()):
        return entry["recap"]
    return None


def _nearest_earlier_recap(state: dict, manifest: list[dict], file: str) -> str:
    """The recap of the nearest existing entry STRICTLY EARLIER than `file`
    in manifest order; "" when none exists. Deliberately shallow: a backfill
    anchors on what is already stored, never recursing to fill the gap."""
    ordered = [e.get("file") for e in sorted(manifest, key=lambda e: int(e.get("order", 0)))]
    if file not in ordered:
        return ""
    for name in reversed(ordered[: ordered.index(file)]):
        recap = _entry_recap(state, Path(str(name)).stem)
        if recap is not None:
            return recap
    return ""


def _default_chat(project_dir: Path, cfg: dict) -> Callable[[str], str]:
    """The recap-provider call, with the pipeline's per-project LLM trace
    logging (tn_recheck.default_chat's pattern)."""
    from lib import pipeline  # local: pipeline imports this module at load time

    def chat(prompt: str) -> str:
        return pipeline._chat(project_dir, cfg, "recap", prompt,
                              json_schema=RECAP_SCHEMA)

    return chat


def _generate(project_dir: Path, cfg: dict, chapter_title: str, body: str,
              previous_recap: str,
              chat: Callable[[str], str] | None) -> str:
    """One recap-provider call over recap.md -> the recap string.

    Raises on any failure (callers treat recap generation as advisory)."""
    from lib import pipeline  # local: pipeline imports this module at load time

    tpl = pipeline._load_template(project.paths(project_dir)["templates"], "recap.md")
    prompt = pipeline.fill(
        tpl,
        {
            "source_lang": pipeline._lang_name(cfg.get("source_lang", "")),
            "target_lang": pipeline._lang_name(cfg.get("target_lang", "")),
            "previous_recap": previous_recap or "",
            "chapter_title": chapter_title or "",
            "chapter_text": body,
        },
        "recap.md",
    )
    do_chat = chat if chat is not None else _default_chat(project_dir, cfg)
    resp = do_chat(prompt)
    data = client.extract_json(resp)
    recap = data.get("recap") if isinstance(data, dict) else None
    if not isinstance(recap, str) or not recap.strip():
        raise ValueError("expected a non-empty 'recap' string")
    return recap


def _stamp(recap: str) -> dict:
    return {"recap": recap,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def ensure_recap(project_dir: Path, cfg: dict, manifest: list[dict], file: str,
                 tag: str, chat: Callable[[str], str] | None = None) -> str:
    """Resolve the recap to INJECT when translating chapter `file`.

    The predecessor's stored entry when present (no LLM call); else a
    one-call backfill of the predecessor, which also saves the state and
    prints `{tag} [init] recap (backfill <prev>)`. "" for the first
    chapter, and "" on any failure (one `{tag} [warn] recap backfill
    failed for <prev>: <exc>` line) -- advisory, never raises. `chat`
    overrides the LLM call (tests pass a stub).
    """
    prev: str | None = None
    try:
        prev = predecessor(manifest, file)
        if prev is None:
            return ""
        state = load_state(project_dir)
        existing = _entry_recap(state, Path(prev).stem)
        if existing is not None:
            return existing
        prev_path = project.paths(project_dir)["translated"] / prev
        if not prev_path.is_file():
            raise FileNotFoundError(f"translated chapter missing: {prev_path}")
        fm, body = project.read_chapter(prev_path)
        title = str(fm.get("title") or fm.get("chapter_title") or "")
        previous_recap = _nearest_earlier_recap(state, manifest, prev)
        recap = _generate(project_dir, cfg, title, body, previous_recap, chat)
        state.setdefault("chapters", {})[Path(prev).stem] = _stamp(recap)
        save_state(project_dir, state)
        print(f"{tag} [init] recap (backfill {prev})")
        return recap
    except Exception as exc:  # noqa: BLE001 - advisory: never block a chapter
        print(f"{tag} [warn] recap backfill failed for {prev or file}: {exc}")
        return ""


def record_recap(project_dir: Path, cfg: dict, file: str,
                 title: str, body: str, prev_recap_text: str, tag: str,
                 chat: Callable[[str], str] | None = None) -> None:
    """Generate/overwrite `file`'s OWN recap entry post-ASSEMBLE.

    {{previous_recap}} is prev_recap_text -- the recap ensure_recap returned
    at the chapter's start, passed in rather than re-resolved, so a backfill
    performed there is exactly what this chapter's record builds on. The
    entry is overwritten unconditionally (retranslation refreshes it), the
    state is saved, and `{tag} [init] recap` is printed. Any failure prints
    `{tag} [warn] recap generation failed for <file>: <exc>` and leaves the
    prior state intact -- never raises. `chat` overrides the LLM call
    (tests pass a stub).
    """
    try:
        recap = _generate(project_dir, cfg, title, body, prev_recap_text, chat)
        state = load_state(project_dir)
        state.setdefault("chapters", {})[Path(file).stem] = _stamp(recap)
        save_state(project_dir, state)
        print(f"{tag} [init] recap")
    except Exception as exc:  # noqa: BLE001 - advisory: never block a chapter
        print(f"{tag} [warn] recap generation failed for {file}: {exc}")
