# Auto-build the epub after each finalized chapter (parallel subprocess)

After every chapter that finishes with status `translated`, fire a background `build-epub` subprocess so `export/` always holds a current, epubcheck-validated book while translation continues. Covers both `translate` and `retry` (both delegate to `pipeline.run_range`).

## 1. New: `scripts/lib/autobuild.py` — build scheduler

A small class managing **one build at a time** (epub writes `export/<slug>.epub` non-atomically to a fixed path — overlapping builds would corrupt it):

- `trigger(reason)` — mark a build pending (`reason` = chapter file, used in the log marker).
- `poll()` — non-blocking: reap a finished child (report `[epub-auto] build ok` / `[warn] epub-auto build failed - see logs/epub-build.log`), then spawn a new child if pending and idle.
- `finalize()` — end of the batch: wait for any running child, then if a build is still pending (or was skipped because one was running), run **one final synchronous build** so the last chapters are guaranteed in the final epub. Failures warn; translation exit codes are unchanged.
- `abort()` — KeyboardInterrupt path: kill a running child, don't start another, re-raise continues.

Spawn: `[sys.executable, "<skill>/scripts/translate.py", "build-epub", "--project", <dir>]` — full build *with* epubcheck (the docker call runs inside the child; it never blocks translation). Child stdout+stderr append to `logs/epub-build.log` with `=== build after Chapter_NNNN, <timestamp> ===` separators; the handle is closed on reap. Safe by ordering: `run_chapter` flips the manifest to `translated` before returning, so a spawned build always sees the finished chapter.

## 2. `scripts/lib/pipeline.py` — `run_range` integration

After `outcome == "translated"` (the single success point, ~line 997): `scheduler.trigger(file); scheduler.poll()`. The loop gets a `try/except KeyboardInterrupt: scheduler.abort(); raise` and a normal-path `scheduler.finalize()` before returning results. Scheduler is created only when `cfg["auto_build_epub"]` is true and the batch is non-empty.

## 3. `scripts/lib/project.py` — harden `atomic_write_text` for the parallel reader

On Windows, `os.replace` can raise `PermissionError` if the child has `chapters.json` open for reading at that instant — today that would mark a *successfully translated* chapter needs-review. Retry the replace ~5× (100ms apart) on `PermissionError`. Benefits every atomic write, costs 4 lines.

## 4. `scripts/lib/config.py` — `auto_build_epub: true` default

Comment: rebuild the epub in a parallel subprocess after each translated chapter; set false to build only via the `build-epub` command. Existing projects get it via config merging.

## 5. Docs

- **SKILL.md**: pipeline/operating notes — epub auto-rebuilds in the background per chapter (serialized), guaranteed final build, log at `logs/epub-build.log`, `auto_build_epub` off-switch.
- **README.md**: workflow note (the epub stays current during long batches; final build guaranteed at the end) + tuning line.
- **references/file-formats.md**: config key + the new log file.

## 6. Verification

1. Offline mock regression: init → `translate --chapters 1-3` — expect background builds (log separators in order, none overlapping), guaranteed final epub present, epubcheck passed, exit 0 unchanged.
2. `auto_build_epub: false` run — no spawns, no log.
3. Interrupt behavior: Ctrl-C mid-batch kills the child (spot-check by code review of the abort path; no live test needed).
4. Live sanity: `retry` one sotn chapter — build fires after it, parallel to nothing (last chapter → finalize path).
5. Sync installed copy, commit, push.