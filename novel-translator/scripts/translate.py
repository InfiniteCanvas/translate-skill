#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pyyaml>=6.0", "ebooklib>=0.18", "pillow>=10.0"]
# ///
"""novel-translator: staged, resumable CJK novel translation CLI.

Subcommands: init, ping, seed, profile, styles, status, translate, retry, mark, review, build-epub.
Exit codes: 0 ok, 1 chapter needs-review / epubcheck failed, 2 usage or setup error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows console safety: force UTF-8 with replacement characters.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

# Make scripts/lib importable no matter where the CLI is invoked from.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib import client, config, cover, epub, glossary, logger, pipeline, project, review, tn  # noqa: E402
from lib import profile as profile_mod  # noqa: E402
from lib import styles as styles_mod  # noqa: E402

SKILL_ROOT = SCRIPT_DIR.parent
ASSETS_DIR = SKILL_ROOT / "assets"
CATALOGUES_DIR = ASSETS_DIR / "catalogues"
TEMPLATES_SRC_DIR = ASSETS_DIR / "templates"

DEFAULT_API_BASE = "http://100.85.218.125:8888/v1"
MARK_STATUSES = ("translated", "pending", "needs-review")


class CliError(Exception):
    """Usage or setup error -> exit code 2."""


def _fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)


def _load_config(project_dir: Path) -> dict:
    try:
        return config.load_config(project_dir)
    except FileNotFoundError as exc:
        raise CliError(f"{project_dir / 'config.json'} not found - run 'init' first") from exc
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read config.json: {exc}") from exc


def _load_manifest(project_dir: Path) -> list[dict]:
    try:
        return project.load_manifest(project_dir)
    except FileNotFoundError as exc:
        raise CliError(f"manifest not found in {project_dir} - run 'init' first") from exc
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read manifest: {exc}") from exc


def _probe_glossary(project_dir: Path) -> None:
    """Fail fast (CliError, exit 2) when glossary.json is unreadable/corrupt,
    instead of letting every chapter in a batch crash mid-run."""
    try:
        glossary.load(project_dir)
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        raise CliError(f"cannot read {project_dir / 'glossary.json'}: {exc}") from exc


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, project_dir: Path) -> int:
    print(f"[init] initializing project: {project_dir}")
    paths = project.paths(project_dir)

    if paths["config"].exists() and not args.force:
        raise CliError(f"{paths['config']} already exists (use --force to reinitialize)")

    src_dir = paths["source"]
    if not src_dir.is_dir():
        raise CliError(f"source directory not found: {src_dir}")
    # Use the real discovery rule (CHAPTER_RE) so files like
    # Chapter_0007.zh.md are not counted here but silently dropped from the
    # manifest later.
    chapters = project.discover(project_dir)
    if not chapters:
        raise CliError(
            f"no source chapters found in {src_dir}: files must be named "
            "'Chapter_NNN[a].md' (1-4 digit zero-padded number, optional "
            "single-letter suffix), e.g. Chapter_001.md or Chapter_0002a.md"
        )
    print(f"[init] found {len(chapters)} source chapter(s)")

    # Resolve the style guide before anything is created so a bad name fails
    # fast instead of leaving a half-initialized project.
    style_name: str | None = None
    style_body: str | None = None
    if args.style != "auto":
        style_path = Path(args.style)
        if style_path.is_file():
            style_name = style_path.stem
            try:
                text = style_path.read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:  # ValueError covers UnicodeDecodeError
                raise CliError(f"cannot read style file {style_path}: {exc}") from exc
            _description, parsed_body = styles_mod.parse_style_file(text)
            if not parsed_body.strip():
                raise CliError(f"style file {style_path} has an empty body")
            style_body = parsed_body
        else:
            style_name = args.style
            try:
                style_body = styles_mod.load_style(project_dir, args.style)
            except styles_mod.StyleError as exc:
                raise CliError(str(exc)) from exc

    if not TEMPLATES_SRC_DIR.is_dir():
        raise CliError(f"skill templates not found: {TEMPLATES_SRC_DIR}")

    for key in ("draft", "translated", "export", "covers", "templates"):
        paths[key].mkdir(parents=True, exist_ok=True)
    print("[init] created directories: draft translated export covers templates")

    if style_name is not None:
        (project_dir / "style.md").write_text(
            (style_body or "").rstrip("\n") + "\n", encoding="utf-8", newline="\n"
        )
        print(f"[init] style: {style_name} -> style.md")
        names = [name for name, _desc in styles_mod.list_styles(project_dir)]
        print(f"[init] available styles: {', '.join(names)} (or --style auto)")
    else:
        # --style auto: remove any style.md left by a previous preset init -
        # the pipeline treats style.md as tier 1, so a stale copy would
        # silently override the regenerated profile.
        (project_dir / "style.md").unlink(missing_ok=True)

    providers = {
        job: {"base_url": args.api_base, "model": None, "temperature": temperature,
              "max_tokens": 16384, "thinking": False}
        for job, temperature in (
            ("translator", 0.7),
            ("glossary", 0.2),
            ("reviewer", 0.0),
            ("annotator", 0.2),
            ("profile", 0.3),
        )
    }
    # Hy-MT2 model card: translation sampling is temperature 0.7, top_p 1.0.
    providers["translator"]["top_p"] = 1.0
    cfg: dict[str, Any] = {
        "source_lang": args.source_lang,
        "target_lang": args.target_lang,
        "providers": providers,
    }
    cfg.update(config.DEFAULTS)
    config.save_config(project_dir, cfg)
    print(f"[init] wrote {paths['config'].name}")

    novel_info: dict[str, Any] = {
        "title": args.title,
        "title_translated": None,
        "author": args.author,
        "source_url": args.source_url,
        "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
        "source_lang": args.source_lang,
        "target_lang": args.target_lang,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cover": "covers/cover.jpg",
        "style": style_name if style_name is not None else "auto",
    }
    if args.background:
        novel_info["background"] = args.background
    paths["novel_info"].write_text(
        json.dumps(novel_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[init] wrote {paths['novel_info'].name}")

    glossary.save(project_dir, glossary.empty())
    paths["tn_history"].write_text("{}\n", encoding="utf-8")
    print(f"[init] initialized {paths['glossary'].name} and {paths['tn_history'].name}")

    copied = 0
    for tpl in sorted(TEMPLATES_SRC_DIR.glob("*.md")):
        shutil.copy2(tpl, paths["templates"] / tpl.name)
        copied += 1
    if copied == 0:
        raise CliError(f"no *.md templates found in {TEMPLATES_SRC_DIR}")
    print(f"[init] copied {copied} template(s) into templates/")

    # Backfill missing frontmatter on bare source chapters (novel-level
    # fields from the CLI args; chapter_title from the first body line) so
    # the manifest and translated copies carry proper metadata. Runs before
    # sync_manifest so it can pick up the titles.
    backfilled = 0
    for chapter in chapters:
        fm, body = project.read_chapter(chapter.path)
        changed = False
        for key, value in (
            ("novel_title", args.title),
            ("author", args.author),
            ("source_url", args.source_url),
        ):
            if key not in fm and value:
                fm[key] = value
                changed = True
        if "chapter_title" not in fm:
            first_line = next(
                (ln.strip(" \u3000#") for ln in body.split("\n") if ln.strip()), ""
            )
            if first_line:
                fm["chapter_title"] = first_line
                changed = True
        if changed:
            project.write_chapter(chapter.path, fm, body)
            backfilled += 1
    if backfilled:
        print(f"[init] backfilled frontmatter on {backfilled} bare chapter(s)")

    manifest = project.sync_manifest(project_dir)
    print(f"[init] manifest: {len(manifest)} chapter(s)")

    total_added = 0
    total_skipped = 0
    catalogues_used = 0
    if CATALOGUES_DIR.is_dir():
        for cat_path in sorted(CATALOGUES_DIR.glob("*.json")):
            try:
                catalogue = glossary.load_catalogue(cat_path)
            except Exception as exc:  # noqa: BLE001 - a bad catalogue must not abort init
                print(f"[warn] catalogue {cat_path.name}: {exc}")
                continue
            if catalogue.get("language") != args.source_lang:
                continue
            added, skipped = glossary.seed(project_dir, catalogue, int(cfg["seed_min_count"]))
            total_added += added
            total_skipped += skipped
            catalogues_used += 1
            print(f"[init] {cat_path.name}: seeded {added} terms ({skipped} skipped as duplicates)")
    else:
        print(f"[warn] catalogues directory not found: {CATALOGUES_DIR}")
    print(
        f"[init] glossary seeded {total_added} terms from {catalogues_used} catalogue(s) "
        f"({total_skipped} skipped as duplicates)"
    )

    # Style profile (legacy '--style auto' path): sample the opening chapters
    # and have the model describe the narrative voice; stored as
    # novel_info.json:style_profile and used by later prompts. Preset styles
    # read project style.md instead and skip this LLM call. Failures only
    # warn -- init must survive a missing profile.
    if args.style == "auto" and not args.skip_profile:
        try:
            prof = profile_mod.generate_profile(
                project_dir, cfg,
                int(cfg.get("style_sample_chapters", 4)),
                int(cfg.get("style_sample_chars", 12000)),
            )
            novel_info["style_profile"] = prof
            # Rewrite novel_info.json (same pretty format as written earlier in init).
            paths["novel_info"].write_text(
                json.dumps(novel_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"[init] style profile: {prof.get('style_summary', '')[:100]}")
        except Exception as exc:  # noqa: BLE001 - profile problems must not abort init
            print(f"[warn] style profile generation failed: {exc}")

    try:
        cover_path = cover.ensure_cover(project_dir, novel_info, args.cover_url)
        print(f"[ok] cover ready: {cover_path}")
    except Exception as exc:  # noqa: BLE001 - cover problems must not abort init
        print(f"[warn] cover setup failed: {exc}")

    print("[ok] project initialized")
    print(f"     title:    {args.title}")
    print(f"     author:   {args.author}")
    print(f"     chapters: {len(manifest)} ({args.source_lang} -> {args.target_lang})")
    if novel_info.get("style") and novel_info["style"] != "auto":
        print(f"     style:    {novel_info['style']}")
    prof = novel_info.get("style_profile")
    if isinstance(prof, dict):
        print(f"     style:    auto profile ({str(prof.get('style_summary', ''))[:60]})")
    print("next step: translate --next N   (e.g. 'translate --next 3')")
    return 0


def _ping_err(exc: Exception) -> str:
    """Compact per-job error line for ping output."""
    return f"{type(exc).__name__}: {str(exc)[:120]}"


def cmd_ping(args: argparse.Namespace, project_dir: Path) -> int:
    cfg = _load_config(project_dir)
    failed = False
    for job in config.PROVIDER_JOBS:
        pcfg = config.provider(cfg, job)
        base_url = str(pcfg.get("base_url", ""))
        try:
            model = client.resolve_model(base_url, headers=client.auth_headers(pcfg))
            extra = f" (config model: {pcfg['model']})" if pcfg.get("model") else ""
            print(f"[ok] {job:<10} {base_url} -> {model}{extra}")
        except Exception as exc:  # noqa: BLE001 - endpoint errors are reported per job
            # Hosted providers may not serve /models (or auth-gate it); a
            # minimal chat completion still proves routing + auth work.
            if pcfg.get("model"):
                try:
                    client.probe(pcfg)
                    print(
                        f"[ok] {job:<10} {base_url} -> {pcfg['model']} "
                        f"(chat ok; /models failed: {_ping_err(exc)})"
                    )
                    continue
                except Exception as exc2:  # noqa: BLE001 - report both failures
                    failed = True
                    print(
                        f"[FAIL] {job:<10} {base_url} -> "
                        f"/models: {_ping_err(exc)}; chat: {_ping_err(exc2)}"
                    )
                    continue
            failed = True
            print(f"[FAIL] {job:<10} {base_url} -> {_ping_err(exc)}")
    if failed:
        _fail("ping: one or more providers unreachable")
        return 2
    print("[ok] all providers reachable")
    return 0


def cmd_seed(args: argparse.Namespace, project_dir: Path) -> int:
    cfg = _load_config(project_dir)
    if args.min_count is not None:
        min_count = int(args.min_count)
    else:
        min_count = int(cfg.get("seed_min_count", config.DEFAULTS["seed_min_count"]))

    explicit = bool(args.catalogue)
    if explicit:
        cat_paths: list[Path] = []
        for raw in args.catalogue:
            path = Path(raw)
            if not path.is_file():
                raise CliError(f"catalogue not found: {path}")
            cat_paths.append(path)
    else:
        if not CATALOGUES_DIR.is_dir():
            raise CliError(f"catalogues directory not found: {CATALOGUES_DIR}")
        cat_paths = sorted(CATALOGUES_DIR.glob("*.json"))

    lang = cfg.get("source_lang")
    total_added = 0
    total_skipped = 0
    used = 0
    for cat_path in cat_paths:
        try:
            catalogue = glossary.load_catalogue(cat_path)
        except Exception as exc:
            if explicit:
                raise CliError(f"cannot load catalogue {cat_path}: {exc}") from exc
            print(f"[warn] catalogue {cat_path.name}: {exc}")
            continue
        if not explicit and catalogue.get("language") != lang:
            continue
        added, skipped = glossary.seed(project_dir, catalogue, min_count)
        total_added += added
        total_skipped += skipped
        used += 1
        print(f"[ok] {cat_path.name}: seeded {added} terms ({skipped} skipped as duplicates)")

    if used == 0:
        print(f"[warn] no catalogues matched (language={lang})")
        return 0
    print(f"[ok] seeded {total_added} terms ({total_skipped} skipped as duplicates) from {used} catalogue(s)")
    return 0


def cmd_profile(args: argparse.Namespace, project_dir: Path) -> int:
    cfg = _load_config(project_dir)
    paths = project.paths(project_dir)
    if not paths["novel_info"].is_file():
        raise CliError(f"{paths['novel_info']} not found - run 'init' first")
    novel_info = json.loads(paths["novel_info"].read_text(encoding="utf-8"))

    sample_chapters = (
        int(args.chapters) if args.chapters is not None
        else int(cfg.get("style_sample_chapters", 4))
    )
    sample_chars = (
        int(args.chars) if args.chars is not None
        else int(cfg.get("style_sample_chars", 12000))
    )

    try:
        prof = profile_mod.generate_profile(project_dir, cfg, sample_chapters, sample_chars)
    except Exception as exc:  # noqa: BLE001 - any generation failure is exit 1
        _fail(f"style profile generation failed: {type(exc).__name__}: {exc}")
        return 1

    novel_info["style_profile"] = prof
    paths["novel_info"].write_text(
        json.dumps(novel_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("[ok] style profile written to novel_info.json")
    if (project_dir / "style.md").is_file():
        print("[warn] style.md exists and takes precedence over the profile - delete it to activate the profile")
    print(f"style_summary: {prof.get('style_summary', '')}")
    print(f"background: {prof.get('background', '')}")
    return 0


def cmd_styles(args: argparse.Namespace, project_dir: Path) -> int:
    rows = styles_mod.list_styles(project_dir)
    if not rows:
        print("(no style presets found)")
        return 0
    width = max(len(name) for name, _ in rows)
    for name, desc in rows:
        print(f"{name:<{width}}  {desc}")
    print("\nuse: init --style NAME   (or --style auto for a model-generated profile)")
    return 0


def cmd_status(args: argparse.Namespace, project_dir: Path) -> int:
    manifest = _load_manifest(project_dir)
    paths = project.paths(project_dir)
    entries = sorted(manifest, key=lambda e: int(e.get("order", 0)))

    rows: list[tuple[str, str, str, str, str]] = []
    for entry in entries:
        state = pipeline.load_state(paths["draft"], entry["file"])
        attempts = "-" if state is None else str(state.get("attempt", 0))
        rows.append(
            (
                str(entry.get("order", "")),
                entry["file"],
                str(entry.get("status", "?")),
                attempts,
                str(entry.get("title") or ""),
            )
        )

    ow = max([len(r[0]) for r in rows] + [len("ORDER")])
    fw = max([len(r[1]) for r in rows] + [len("FILE")])
    sw = max([len(r[2]) for r in rows] + [len("STATUS")])
    aw = max([len(r[3]) for r in rows] + [len("ATT")])
    print(f"{'ORDER':>{ow}}  {'FILE':<{fw}}  {'STATUS':<{sw}}  {'ATT':>{aw}}  TITLE")
    for r in rows:
        print(f"{r[0]:>{ow}}  {r[1]:<{fw}}  {r[2]:<{sw}}  {r[3]:>{aw}}  {r[4]}")

    print()
    for status in project.STATUSES:
        count = sum(1 for e in manifest if e.get("status") == status)
        print(f"{status:>13}: {count}")
    g = glossary.load(project_dir)
    print(f"{'glossary':>13}: {len(g.get('terms', []))} terms")
    history = tn.load_history(project_dir)
    print(f"{'tn_history':>13}: {len(history)} terms")

    if paths["novel_info"].is_file():
        try:
            novel_info = json.loads(paths["novel_info"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            novel_info = {}
        # Mirror the pipeline's style resolution: style.md (tier 1, only when
        # it has a non-empty body) -> style_profile.style_summary -> default.
        style_body = ""
        if (project_dir / "style.md").is_file():
            try:
                _desc, style_body = styles_mod.parse_style_file(
                    (project_dir / "style.md").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                style_body = ""
        if style_body.strip():
            name = novel_info["style"] if isinstance(novel_info.get("style"), str) else "custom"
            print(f"{'style':>13}: {name} (style.md)")
        elif isinstance(novel_info.get("style_profile"), dict) and str(
            novel_info["style_profile"].get("style_summary") or ""
        ).strip():
            print(f"{'style':>13}: auto profile")
        else:
            print(f"{'style':>13}: default fallback")

    if args.why:
        for entry in entries:
            if entry.get("status") != "needs-review":
                continue
            file = entry["file"]
            state = pipeline.load_state(paths["draft"], file)
            if state is None:
                print(f"\n{file} (attempt -):\n    (no state file)")
                continue
            print(f"\n{file} (attempt {state.get('attempt', 0)}):")
            feedback = state.get("feedback")
            recent = [
                fb for fb in (feedback if isinstance(feedback, list) else [])
                if isinstance(fb, str)
            ][-3:]
            if recent:
                for fb in recent:
                    print(f"    {fb[:200]}")
            else:
                print("    (no feedback)")
    return 0


def cmd_translate(args: argparse.Namespace, project_dir: Path) -> int:
    cfg = _load_config(project_dir)
    manifest = _load_manifest(project_dir)
    _probe_glossary(project_dir)
    force = bool(args.force)

    if args.next is not None:
        if args.next < 1:
            raise CliError("--next must be a positive integer")
        ordered = sorted(manifest, key=lambda e: int(e.get("order", 0)))
        # needs-review chapters are deliberately excluded: they wait for a
        # human/agent decision, then `retry` or `mark`.
        if force:
            pool = [e["file"] for e in ordered]
        else:
            pool = [e["file"] for e in ordered if e.get("status") in ("pending", "in-progress")]
        files = pool[: args.next]
        if not files:
            print("[ok] nothing to translate (no pending chapters; needs-review chapters require 'retry')")
            return 0
    else:
        files = pipeline.parse_range(args.chapters, manifest)

    print(f"[init] translating {len(files)} chapter(s)" + (" (force)" if force else ""))
    results = pipeline.run_range(project_dir, files, cfg, force=force)

    print()
    print(
        f"[ok] translated: {len(results['translated'])}  "
        f"needs-review: {len(results['needs-review'])}  "
        f"skipped: {len(results['skipped'])}"
    )
    if results["needs-review"]:
        _fail("chapters need review: " + ", ".join(results["needs-review"]))
        return 1
    return 0


def cmd_retry(args: argparse.Namespace, project_dir: Path) -> int:
    cfg = _load_config(project_dir)
    manifest = _load_manifest(project_dir)
    _probe_glossary(project_dir)
    if args.failed:
        files = [
            e["file"]
            for e in sorted(manifest, key=lambda e: int(e.get("order", 0)))
            if e.get("status") == "needs-review"
        ]
        if not files:
            print("[ok] no needs-review chapters to retry")
            return 0
    else:
        files = pipeline.parse_range(args.chapters, manifest)
    paths = project.paths(project_dir)

    for file in files:
        stem = Path(file).stem
        for artifact in (f"{stem}.state.json", f"{stem}.md", f"{stem}.lines.json"):
            (paths["draft"] / artifact).unlink(missing_ok=True)
        (paths["translated"] / file).unlink(missing_ok=True)
        project.set_status(manifest, file, "pending")
        print(f"[init] {file}: cleared artifacts, status pending")
    project.save_manifest(project_dir, manifest)

    results = pipeline.run_range(project_dir, files, cfg, force=False)

    print()
    print(
        f"[ok] translated: {len(results['translated'])}  "
        f"needs-review: {len(results['needs-review'])}  "
        f"skipped: {len(results['skipped'])}"
    )
    if results["needs-review"]:
        _fail("chapters need review: " + ", ".join(results["needs-review"]))
        return 1
    return 0


def cmd_mark(args: argparse.Namespace, project_dir: Path) -> int:
    manifest = _load_manifest(project_dir)
    files = pipeline.parse_range(args.chapters, manifest)
    paths = project.paths(project_dir)

    if args.status == "translated":
        missing = [f for f in files if not (paths["translated"] / f).is_file()]
        if missing:
            raise CliError(
                "cannot mark translated, missing output file(s): "
                + ", ".join(f"translated/{f}" for f in missing)
            )

    for file in files:
        project.set_status(manifest, file, args.status)
        print(f"[ok] {file} -> {args.status}")
    project.save_manifest(project_dir, manifest)
    return 0


def cmd_review(args: argparse.Namespace, project_dir: Path) -> int:
    if args.batch_size < 1:
        raise CliError("--batch-size must be a positive integer")
    cfg = _load_config(project_dir)
    _probe_glossary(project_dir)
    g = glossary.load(project_dir)
    if not g.get("terms"):
        print("[ok] glossary is empty - nothing to review")
        return 0

    # Header and per-batch progress print BEFORE/DURING the model calls --
    # a large glossary means minutes of silent LLM batches otherwise.
    terms = g.get("terms", [])
    n_batches = -(-len(terms) // args.batch_size)
    print(
        f"[glossary] review: {len(terms)} entries"
        + f" ({n_batches} model batch(es) of up to {args.batch_size})"
    )
    result = review.review_glossary(project_dir, cfg, args.batch_size)
    batches = result["batches"]
    translations = {
        str(e.get("source", "")): str(e.get("translation", "")) for e in g.get("terms", [])
    }
    findings = result["findings"]
    for f in findings:
        line = (
            f"[glossary] {f['severity']} '{f['source']}' -> "
            f"'{translations.get(f['source'], '?')}': {f['kind']} - {f['reason']}"
        )
        if f.get("suggestion"):
            line += f" (suggest: {f['suggestion']})"
        print(line)

    applied: list[dict] = []
    skipped: list[dict] = []
    if args.fix:
        fixes = review.apply_fixes(project_dir, findings)
        applied, skipped = fixes["applied"], fixes["skipped"]
        for a in applied:
            print(f"[glossary] fixed '{a['source']}': {a['field']} '{a['old']}' -> '{a['new']}'")
        for s in skipped:
            print(f"[glossary] warn fix skipped for '{s['source']}' ({s['field']}): {s['reason']}")

    # A fix resolves every finding on the same entry FIELD, not just the
    # finding kind it came from (e.g. a mistranslation fix also resolves the
    # wrong_language warn it superseded).
    resolved = {(a["source"], a["field"]) for a in applied}

    def outstanding(f: dict) -> bool:
        field = review.field_for_kind(f["kind"])
        return field is None or (f["source"], field) not in resolved

    warns = sum(1 for f in findings if f["severity"] == "warn" and outstanding(f))
    infos = sum(1 for f in findings if f["severity"] == "info" and outstanding(f))
    logger.log_event(project_dir, {
        "event": "glossary_review",
        "entries": result["entries"],
        "batches": batches,
        "batch_errors": result["batch_errors"],
        "findings": findings,
        "applied": applied,
        "skipped": skipped,
    })
    print(f"[glossary] review: {result['entries']} entries, {warns} warn / {infos} info findings")
    report_path = review.write_report(
        project_dir,
        findings=findings, terms=terms, applied=applied, skipped=skipped,
        ran_fix=bool(args.fix), batches=batches,
        batch_errors=result["batch_errors"], cfg=cfg,
    )
    print(f"[glossary] report: {report_path}")
    if warns:
        _fail(f"glossary review: {warns} finding(s) need attention")
        return 1
    print("[ok] glossary review complete")
    return 0


def cmd_build_epub(args: argparse.Namespace, project_dir: Path) -> int:
    cfg = _load_config(project_dir)
    paths = project.paths(project_dir)
    if not paths["novel_info"].is_file():
        raise CliError(f"{paths['novel_info']} not found - run 'init' first")
    novel_info = json.loads(paths["novel_info"].read_text(encoding="utf-8"))

    try:
        epub_path, ok, output = epub.build(project_dir, novel_info, cfg, bool(args.skip_check))
    except Exception as exc:  # noqa: BLE001 - report builder crashes as setup errors
        _fail(f"epub build failed: {type(exc).__name__}: {exc}")
        return 2

    print(f"[ok] epub: {epub_path}")
    if output and str(output).strip():
        print(str(output).rstrip())
    if ok is None:
        print("[warn] epubcheck skipped")
        return 0
    if ok:
        print("[ok] epubcheck passed")
        return 0
    _fail("epubcheck failed")
    return 1


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translate.py",
        description="Staged, resumable novel translation pipeline (CJK -> target language).",
    )
    # Accept --project both before and after the subcommand (separate dests so
    # the subparser default can't clobber a value given before it).
    parser.add_argument("--project", dest="project_global", default=None,
                        help="project directory (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", dest="project", default=None,
                        help="project directory (default: current directory)")

    p = sub.add_parser("init", parents=[common], help="initialize a project from source/ chapters")
    p.add_argument("--title", required=True, help="novel title (original language)")
    p.add_argument("--author", required=True, help="author name")
    p.add_argument("--source-url", default="", help="URL of the source novel")
    p.add_argument("--source-lang", default="zh", help="source language code (default: zh)")
    p.add_argument("--target-lang", default="en", help="target language code (default: en)")
    p.add_argument("--tags", default="", help="comma-separated tag list")
    p.add_argument("--api-base", default=DEFAULT_API_BASE, help="OpenAI-compatible API base URL")
    p.add_argument("--cover-url", default=None, help="URL to download the cover image from")
    p.add_argument("--style", default="classic", metavar="NAME|PATH|auto",
                   help="style guide: a preset name, a path to a .md file, or 'auto' for a model-generated profile (default: classic)")
    p.add_argument("--background", default=None,
                   help="2-4 sentences of novel background for the translator's context frame")
    p.add_argument("--skip-profile", action="store_true",
                   help="deprecated no-op for preset styles; still skips the LLM call for --style auto")
    p.add_argument("--force", action="store_true", help="reinitialize even if config.json exists")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ping", parents=[common], help="check provider endpoints and resolved models")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("seed", parents=[common], help="seed the glossary from asset catalogues")
    p.add_argument("--min-count", type=int, default=None, help="minimum term count in source text")
    p.add_argument("--catalogue", action="append", default=None, metavar="PATH",
                   help="explicit catalogue path (repeatable; bypasses language filter)")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("profile", parents=[common],
                       help="regenerate the style profile for an initialized project")
    p.add_argument("--chapters", type=int, default=None, metavar="N",
                   help="chapters to sample (default: config style_sample_chapters)")
    p.add_argument("--chars", type=int, default=None, metavar="N",
                   help="approx. source characters to include (default: config style_sample_chars)")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("styles", parents=[common], help="list available translation style presets")
    p.set_defaults(func=cmd_styles)

    p = sub.add_parser("status", parents=[common], help="show per-chapter pipeline status")
    p.add_argument("--why", action="store_true",
                   help="show recent feedback for every needs-review chapter")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("translate", parents=[common], help="run the translation pipeline on chapters")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--chapters", metavar="SPEC",
                   help="chapter spec, e.g. 1,3-5,Chapter_0007.zh.md")
    g.add_argument("--next", type=int, metavar="N", help="next N untranslated chapters")
    p.add_argument("--force", action="store_true", help="retranslate even if already translated")
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("retry", parents=[common], help="wipe chapter artifacts and translate from scratch")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--chapters", metavar="SPEC",
                   help="chapter spec, e.g. 1,3-5,Chapter_0007.zh.md")
    g.add_argument("--failed", action="store_true",
                   help="retry every needs-review chapter (max attempts reached)")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("mark", parents=[common], help="force a chapter status")
    p.add_argument("--chapters", metavar="SPEC", required=True, help="chapter spec")
    p.add_argument("--status", required=True, choices=list(MARK_STATUSES))
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("review", parents=[common],
                       help="advisory quality review (glossary: source-translation alignment audit)")
    p.add_argument("subject", choices=["glossary"], help="what to review")
    p.add_argument("--fix", action="store_true",
                   help="apply guarded model-suggested fixes (translation/definition/category)")
    p.add_argument("--batch-size", type=int, default=review.DEFAULT_BATCH_SIZE, metavar="N",
                   help="glossary entries per model review call (default 40)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("build-epub", parents=[common], help="assemble translated chapters into an EPUB")
    p.add_argument("--skip-check", action="store_true", help="skip the epubcheck validation")
    p.set_defaults(func=cmd_build_epub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    project_dir = Path(getattr(args, "project", None) or args.project_global or ".").resolve()
    try:
        return int(args.func(args, project_dir))
    except CliError as exc:
        _fail(str(exc))
        return 2
    except pipeline.PipelineError as exc:
        _fail(str(exc))
        return 2
    except KeyboardInterrupt:
        _fail("interrupted (chapter state is saved; re-run to resume)")
        return 130
    except OSError as exc:
        _fail(f"{type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
