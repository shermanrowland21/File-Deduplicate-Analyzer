# Project Handoff — File Dedup / Migration Cleanup

Last updated: 2026-09-08. **Read this first when starting a new session.**
Repo: `shermanrowland21/File-Deduplicate-Analyzer`, branch `master`. All work below
is committed + pushed (HEAD `9f9d365`).

---

## The big goal (unchanged)

Clean up a mangled Google Takeout export and route it to its destinations before
Dropbox is shut down. Two folders on the **E: drive** are the ENTIRE universe:
- `E:\Google Drive Files\Organized`  — the mangled Takeout export; the **keep/master** tree
- `E:\Dropbox-Snapshot`               — a robocopy snapshot of live Dropbox; the **throwaway** staging copy

End state:
- **Media** (video/images/design) → **CPMS** on AWS (keep folder structure; needs a migration path up to AWS).
- **Business docs** (Word/PDF/Excel) → **SharePoint** — USA and China (some folders belong to the China team; engineering/CAD is China-bound).
- Everything deduplicated + correctly organized first.

**Hard rules:**
- **NEVER hard-delete.** Everything is quarantine + reversible manifest + Undo.
- **All AI runs on AWS Bedrock.** Never external.
- `D:\Highland Park Dropbox` is LIVE Dropbox — DO NOT touch. Work only on the two E: folders.
- Communication style: college-sophomore level, concise. See `.kiro/steering/communication-style.md`.

---

## CURRENT STATE (as of this handoff)

### Foundation — DONE
- **MD5 index of BOTH folders is complete** (full-file MD5, every file, min_size=0):
  - Organized: **678,929 files, 100% hashed, 7.44 TB** (grew ~26K from the delta sync).
  - Snapshot: **664,620 files hashed** + 54 new CAD files hashed after a manual delta update.
  - Index DBs: `~/.file_dedup_analyzer/md5_index/E_Google Drive Files_Organized.db` and `E_Dropbox-Snapshot.db`. Table `files(path PK, md5, size, mtime)`, indexed on md5.
- **Drive blueprint** (GAM crawl of all 30 shared drives): `~/.file_dedup_analyzer/blueprint/blueprint.db`, 443K Drive files.

### Delta sync — DONE (this session)
- Ran the **overnight delta supervisor** (`backend/overnight_delta.py`): downloaded ALL adds/mods since the Takeout baseline `2026-07-13T19:50:41Z`, for BOTH shared drives AND all 56 users' My Drives, into Organized. Native Google Docs converted to Office (docx/xlsx/pptx). Everything MD5-hashed **incrementally** (batches of 300) as it arrived.
- Progress file: `~/.file_dedup_analyzer/delta/overnight_progress.json` — shows `"stage": "done"`.
- Deletions were NOT applied (downloads-only). If you want to quarantine Drive-deleted files locally later, that's a separate `apply_delta(..., do_deletions=True)` pass — but review first (mostly folders/sync-stubs; ~135 real superseded docs on sherman@).

### Dedup numbers (last dry-run, will shift slightly post-delta)
- Cross-folder snapshot-only dedup: **~283K Snapshot files / ~791 GB** reclaimable (keep Organized, remove Snapshot twins). Organized never touched.
- `-pinned` artifacts in Organized: ~15.4K redundant / 67 unique (folder-aware keeper now).
- System junk (`._` sidecars, .DS_Store, etc.): **~48K files** — filtered out of dedup + purgeable.

---

## WHAT'S BUILT (all committed)

### UI (React, served by backend on port 4600)
Sidebar is now cleaned up — **legacy tabs retired** (Scan Directory, Duplicates, Resolver, Extract Archives, Folder Reconciler removed). Remaining tabs, each with a plain-language **PanelIntro** banner (what it does / safety / when to use):
- **Cross-Folder Dedup** (START HERE) — snapshot-only dedup, junk-purge card at top, voice keeper-guidance chat, cancel + persistent Undo.
- **Pinned Cleanup** — quarantine redundant `-pinned`, rename unique; folder-aware keeper; guidance chat.
- **Dedup Advisor** — voice/chat principles → routing rules (CPMS / SharePoint-US / China), dry-run classification.
- **Folder Renamer** — reads file content to fix mangled folder names (DeepSeek on Bedrock).
- **Find & Purge**, **Media Intelligence**, **Visual Search**, **File Analysis**, **Smart Rename** (now has separator/case pickers — defaults to clean spaces, no underscores), **Settings**.

### Backend services (backend/app/services)
- **md5_index.py** — full-MD5 indexer + `index_paths(root, paths, min_size=0)` targeted hasher (hash a known list, no re-walk; resumable, crash-safe).
- **crossdedup.py** — snapshot-only cross-folder dedup. `is_junk()` filter + `junk_purge` job. Keeper rule engine (`get_rule/set_rule`: prefer_folder, prefer_paths, avoid_paths, tiebreakers) + AI ambiguity resolution (`resolve_ambiguous`). Cancel + undo. Paged `groups_page`.
- **drive_delta.py** — `detect()` (read-only), `apply_delta()` (downloads adds/mods, converts Google→Office, quarantines deletions), incremental hashing, 429 backoff + pacing. `reexport_native()` for flattened files.
- **reconstruct.py** — Drive-blueprint match ledger, filename fix, `-pinned` cleanup (`pinned_preview/pinned_quarantine[bg job]/pinned_rename/pinned_undo`, folder-aware keeper).
- **folder_reconciler.py** — per-parent reconcile + **NEW whole-tree reconciler** (see below).
- **dedup_rules.py** / **dedup_advisor** — the principles→rules engine + Bedrock chat.
- **settings_store.py** — per-function Bedrock model. **Models fixed this session** to active IDs: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-haiku-4-5-20251001-v1:0`, `deepseek.v3.2`. (Old `claude-3-5-*-20241022` and `us.deepseek.v3-v1:0` were dead — all replaced.)

### Detached runner scripts (backend/)
- `overnight_delta.py` — drives→users delta, unattended. DONE running.
- `delta_apply_runner.py` — single-scope delta apply with progress file.
- `delta_detect_runner.py` — read-only delta detect.
- `reconcile_tree_runner.py` — whole-tree folder reconcile (preview|apply).
- `md5_supervisor.py` / `launch_detached_md5.ps1` — original crash-proof MD5 hashers.

---

## ⚠️ THE OPEN ITEM — whole-tree folder reconciler has a BUG to fix

Built this session (`folder_reconciler.reconcile_tree_preview / reconcile_tree / undo_tree`
and `reconcile_tree_runner.py`). It walks all of Organized **deepest-first**, groups
split-twin folders by an anchor (date + 14-char title prefix), merges them into one
canonical folder, and quarantines empties. Local grouping only (no GAM — Drive
validation across 50K folders = too many GAM calls). Reversible via a master manifest
(`undo_tree`).

**PREVIEW RESULT (read-only, nothing applied):**
- 23,223 parent folders scanned
- 263 merge groups, 77 shells, **157,104 files would move**, 6,608 empty folders to quarantine

**THE BUG — do NOT run apply until fixed:** the twin-detection has **false positives**
where the distinguishing token is at the END of the name. Example it wrongly grouped:
```
MERGE into: Unishipper Invoices to pay 2024
  members: ...2022 | ...2023 | ...2024 | ...2025   ← FOUR DIFFERENT YEARS
```
The anchor only looks at the first 14 chars of the title, so folders differing only by
a trailing **year** (2022 vs 2023) or number ("Part 2") get merged when they shouldn't.
Merging those would collapse distinct folders (reversible, but wrong). The 157K
files-moved is inflated by these bad groups.

**THE FIX (next step):** tighten twin-detection so two candidate members are NOT merged
if they differ only by a distinct trailing **year** or **number**. Then re-run preview
(should be a smaller, safe set), review examples again, then apply. The GENUINE twins are
truncation artifacts like `Sales Commission 2025_` + `Sales Commission 2025` (trailing
`_`), which ARE correct to merge.

Where to fix: the `_anchor()` grouping in `folder_reconciler.py` (line ~123) and/or add a
guard in `reconcile_tree`/`_classify` that rejects a merge group whose members carry
distinct trailing year/number tokens.

---

## THE PLAN / ORDER (agreed)

1. **Delta sync** — ✅ DONE.
2. **Whole-tree folder reconciliation FIRST** (before dedup) — fix mangled/split folder
   structure so paths are clean before dedup decisions. ⚠️ Fix the twin bug above, then run.
3. **Then dedup:**
   - Purge **system junk** (the `._`/DS_Store card on Cross-Folder Dedup).
   - **Pinned Cleanup** (clear `-pinned` artifacts).
   - **Cross-Folder Dedup** snapshot-only (remove Snapshot twins, keep Organized) — ~791 GB.
   - Use the **keeper-guidance voice chat** to set folder preferences before purging (the Run
     button is gated until an applied rule exists, so you can't run with wrong keepers).
4. **Dedup Advisor** — state principles by voice (terminated-employee files, media→CPMS,
   China folders→China SharePoint) → routing classification.
5. **Migration** (future): media→CPMS/AWS, business→SharePoint US/China. CPMS/S3 push is
   NOT built yet (S3Storage is a placeholder).

---

## KEY PRINCIPLES the user stated (for the Advisor / keeper rules)
- Media → CPMS/AWS, keep descriptive folder structure; need migration path up to AWS.
- Business → SharePoint US + China; some folders are China team's; engineering/CAD is China-bound.
- **Terminated-employee files**: transferred into `E:\Dropbox-Snapshot\Sherman Rowland\<Person>'s files\` (curly apostrophe U+2019). If a dup exists in an active person's folder, keep active; terminated copy is redundant. Nested (Nermeen's folder contains Joana's, Maricar's). ~2,299 files / 425 GB.
- Keeper preference: when identical files sit in different folders, prefer the more specific/meaningful folder over generic dumps; the `-pinned` copy is often in the BETTER folder (so keeper is folder-aware, not name-based).
- Discoverability of the final organized set matters a lot.

---

## OPERATIONAL NOTES / GOTCHAS
- **Start the backend** (only affects the UI, not detached jobs):
  `$env:ORGANIZED_ROOT="E:\Google Drive Files\Organized"; $env:SNAPSHOT_ROOT="E:\Dropbox-Snapshot"; $env:GAM_ADMIN_USER="sherman@hplapidary.com"; $env:GAM_PATH="C:\GAM7\gam.exe"; & ".\venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 4600 --log-level warning`  (run in `backend/`)
- **control_pwsh_process reuses a DEAD terminal** on first try (isReused:true) and won't actually start. Verify port 4600 with a health check; if not listening, stop + confirm port free + start again.
- **Background jobs die on REBOOT** (not on Kiro restart). After a reboot, restart backend + any runner. Machine won't sleep — long jobs run overnight fine.
- **PowerShell mangles multi-line commands / heredoc quotes** — use a scratch `.py`/`.ps1` file for anything complex, then delete it. Keep shell commands single-line.
- **URL:** http://localhost:4600 — hard-refresh Ctrl+Shift+R for new bundles.
- **GAM** at `C:\GAM7\gam.exe`, admin `sherman@hplapidary.com`.
- **Frontend build:** `cd frontend; npm run build` (backend serves `frontend/dist` from disk — no backend restart needed for frontend-only changes, just refresh).
- **Snapshot gets manual delta updates** — when it does, re-hash the new files. There's no UI button for this yet (used a scratch script: walk Snapshot, diff vs index by size+mtime, `mi.index_paths` the new ones). Worth building a "refresh Snapshot index" button.

## Progress-file locations (read these to check job state)
- Delta: `~/.file_dedup_analyzer/delta/overnight_progress.json`, `apply_runner_progress.json`
- Tree reconcile: `~/.file_dedup_analyzer/folder_reconcile/tree_runner_progress.json`
- Manifests (for undo): `~/.file_dedup_analyzer/{crossdedup,reconstruct,folder_reconcile}/…`

## IMMEDIATE NEXT ACTIONS for the new session
1. Start the backend (command above); confirm http://localhost:4600 health.
2. **Fix the whole-tree reconciler twin bug** (trailing year/number false positives) in `folder_reconciler.py`.
3. Re-run `python reconcile_tree_runner.py preview`; review example_merges in the progress file; confirm clean.
4. Run `python reconcile_tree_runner.py apply` (detached) — reversible via `undo_tree(master_manifest)`.
5. Then move to dedup: junk purge → pinned cleanup → cross-folder snapshot-only dedup (with keeper guidance).
