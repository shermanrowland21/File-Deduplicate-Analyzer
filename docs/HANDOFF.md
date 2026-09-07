# Project Handoff — File Dedup / Migration Cleanup

Last updated: 2026-09-07. Read this first when starting a new session.

---

## The big goal

Clean up a mangled data set and route it to its destinations before Dropbox is shut down.

Two folders on the E: drive are the ENTIRE universe:
- `E:\Google Drive Files\Organized`  (the mangled Google Takeout export — the "keep/clean" tree)
- `E:\Dropbox-Snapshot`              (a robocopy snapshot of the live Dropbox; the throwaway staging copy)

End state:
- **Media files (video, images, design)** → go to **CPMS** (the Content Production Management System on AWS). NOT stored here long-term. Keep them in their descriptive folder structure; we need a migration PATH to push them up to AWS.
- **Business docs (Word/PDF/Excel/etc.)** → go to **SharePoint** — both **USA and China** (certain folders belong to the China team).
- Everything deduplicated and correctly organized first.

Hard rules:
- **NEVER hard-delete.** Everything is quarantine + reversible manifest.
- **All AI must run on AWS Bedrock.** Never external. (Settings system routes per-function models to Bedrock.)
- `D:\Highland Park Dropbox` is the LIVE Dropbox — DO NOT touch/dedupe it. Its scans were purged earlier. Work only on the two E: folders.

Communication style: college-sophomore level, concise. See `.kiro/steering/communication-style.md`.

---

## Where things stand RIGHT NOW

### MD5 hashing (the foundation for everything)
Full-file MD5 of ALL files (not just >50MB) in both folders. Needed because Google Drive only exposes MD5, so MD5 is the only hash that lets us match local files to Drive AND dedupe exactly.

- Two standalone crash-proof supervisors run detached (survive Kiro crashes):
  - `backend/md5_supervisor.py "E:\Google Drive Files\Organized"`
  - `backend/md5_supervisor.py "E:\Dropbox-Snapshot"`
  - Launch both via `backend/launch_detached_md5.ps1` (fixes space-in-path).
- Index DBs: `~/.file_dedup_analyzer/md5_index/E_Google Drive Files_Organized.db` and `E_Dropbox-Snapshot.db`. Table `files(path PK, md5, size, mtime)`, indexed on md5. Resumable (skip path+size+mtime match).
- Progress files: `~/.file_dedup_analyzer/md5_index/supervisor_*.json`; logs `backend/detached_*.log`.
- STATUS as of last check: **Snapshot ~done** (664K files/4.07TB, final pass finding 0 new). **Organized NOT done** (631K files/6.17TB, still hashing videos on pass 3). WAITING for Organized to finish before the big folder cleanup.
- Check status: read the supervisor_*.json, or query the DBs for COUNT(*)/SUM(size).

### What's been CLEANED already (all reversible)
- **`(N)` duplicate purge** (files like `video (1).mp4` that have a byte-identical clean twin): ran twice. Removed 108 (early) + 245 Organized + 385 Snapshot ≈ 738 files / ~54 GB. Quarantine dirs `_ParenDupeQuarantine_*` + manifests in `~/.file_dedup_analyzer/reconstruct/purge_manifests/`.
- **Empty folder cleanup**: 32 + 8 empty junk folders quarantined.
- **Flint Knapping folder merge** (test): merged split webinar twins; later found the manifest-on-timeout bug (now fixed with incremental manifest flush).

### What's BUILT and ready (backend, all in `backend/app`)
Routers registered in `main.py`. Backend runs on port 4600, serves the React UI too.

- **md5_index.py** — full-MD5 indexer (hardened: never crashes on bad/locked/long-path files).
- **drive_blueprint.py** + `blueprint_runner.py` — GAM crawl of ALL SHARED DRIVES ONLY (no My Drives) → `~/.file_dedup_analyzer/blueprint/blueprint.db` (`files(file_id,md5,name,drive_id,drive_name,rel_path,size,is_folder)`, indexed on md5). DONE: 443,183 Drive files indexed across 30 shared drives.
- **reconstruct.py** (`/api/reconstruct`) — the Drive-driven repair:
  - fix-ledger (`~/.file_dedup_analyzer/reconstruct/ledger.db`): matches local md5 → Drive md5 → correct name/path. Status matched/no_match. Organized match ran: ~162K matched / 55K no_match (partial, pre-full-hash).
  - Phase 1 filename fix (`/rename/preview|apply|undo`): rename files in place to Drive-authoritative name, extension-safe, reversible.
  - `(N)` dupe purge (`/parendupes/find|purge|undo`).
  - MD5-driven folder analysis (`/folders/analyze`): classifies folders real/redundant/empty/unknown by CONTENT not name (so truncated real folders aren't mistaken for junk).
  - merge-into-twin (`/folders/merge-plan|merge|merge-undo`): consolidate split webinar folders into the canonical Drive-named folder, skip byte-identical dupes, incremental reversible manifest.
  - `/folders/validate` — compare a local folder's md5 set to Drive blueprint → matched/missing/extra/verdict.
- **crossdedup.py** (`/api/crossdedup`) — CROSS-FOLDER dedup across the two E: folders using the two MD5 indexes:
  - `/preview?prefer_folder=organized` — DRY RUN. Last result: **339,599 dup groups, 658,334 redundant files, 2,388.9 GB reclaimable** (cross-folder 225,486 groups/795.7GB; within-folder 114,113). 
  - `/purge {prefer_folder, confirm}` → background job; `/status/{job}`; `/undo {manifest_file}`.
  - Keeper rule: prefer_folder (organized|snapshot) then tiebreak shortest-path/cleanest-name(no (N)/-at-)/longest-name. Leave-one invariant. Only quarantines when a keeper survives (so a snapshot-only file is safe).
- **catalog.py** (`/api/catalog`) — unified filesystem catalog for instant name search (built earlier, portable to CPMS).
- **name_purge.py** (`/api/purge`) — Find & Purge by keyword (for the Brandon Dawson–type client purges). Reversible.
- Also earlier work: scan_store, resolver (redesigned card UI), folder_reconciler, folder_namer/file_namer, settings (per-function Bedrock model), light/dark theme.

### Git
All committed + pushed to GitHub (repo: shermanrowland21/File-Deduplicate-Analyzer, branch master). Last commit c1bf003. **crossdedup.py + its router are NOT committed yet** — commit them.

---

## THE AGREED PLAN (do in this order)

### STEP 1 — Fix Organized folder structure/names from Google Drive (FIRST)
Once Organized hashing finishes:
1. Run full Organized folder cleanup: `/folders/merge` (merge split twins into canonical Drive names), rename truncated folders, quarantine empties.
2. Validate against Drive (`/folders/validate`) — confirm folders match what Google Drive has.
This gives a clean, correctly-structured base BEFORE dedup decisions.
Gotcha: folder moves make the hashers re-walk — that's why we wait for hashing to finish.

### STEP 2 — Cross-folder dedup, keeper = Organized
Run `/api/crossdedup/purge {prefer_folder:"organized", confirm:true}`. ~2.3 TB reclaimable. Reversible.
Nuance confirmed: if a file exists ONLY in Snapshot, keep it (tool already does this).

### STEP 3 — AI Dedupe/Routing Assistant (BUILD NEXT — build order below)
Frames the problem as industry-standard "ROT" cleanup (Redundant/Obsolete/Trivial). Research confirmed the approach (RecordPoint, Securiti, MERAI arxiv paper): metadata-driven classification at scale, rules first, AI for the ambiguous middle, defensible (reversible) disposal, chunked AI + hash-index.

Build in this order:
- **(a) Principles chat + RULES engine FIRST** (deterministic, free, ~80% of decisions):
  - A chat where the user STATES PRINCIPLES (by voice — see voice note below), the assistant REFLECTS BACK its understanding, user confirms, then it runs.
  - Rules classify every file by: file TYPE (media→CPMS vs business→SharePoint), AGE, PATH/owner, DUP status → into R/O/T buckets + routing (CPMS media / SharePoint-US / SharePoint-China).
  - Terminated-employee rule (user confirmed direction): if a dup also exists in an ACTIVE person's folder, keep the active copy; the terminated-person copy is redundant. Age + type factor in (old + graphic + duplicated → likely drop).
- **(b) AI content-analysis layer SECOND** (Bedrock, chunked):
  - For the ambiguous middle — read actual Word/PDF/doc content to label things ("legal agreement," "commission calculation," etc.), especially poorly-named files.
  - Works on CHUNKS, leverages the MD5 DB, can be re-queried/re-chunked. Never processes all 658K at once.
  - Model: **Claude Sonnet on AWS Bedrock** (strong multi-step reasoning + doc reading). Stays on Bedrock per hard rule.
  - Advisory ONLY: AI proposes; deterministic reversible tools execute on user approval. AI never moves/deletes directly.
  - Note: a lot of media content-analysis will happen in CPMS instead; here we focus on business docs/PDFs that need clearer definition.

### Voice interface (user request)
User HATES typing — wants a slick voice→text interface for the principles chat. User will provide code from their other tool. **PROMPT THE USER for that code when starting the chat UI.** Don't build voice from scratch first; use their proven component.

---

## Key principles the user stated (capture in the chat, refine with them)
- Media files → CPMS/AWS; keep them in their descriptive folder structure; need migration path up to AWS, then out of this tree.
- Business content → SharePoint US + China; certain folders belong to the China team.
- Don't hard-delete — hide/quarantine; it's a big set that's hard to eyeball in a UI, so AI helps decide.
- Terminated-employee files: transferred to owner (sherman) when people were let go; many duplicate other people's files. Keep if unique/important; if old + duplicated + low-value (e.g. 4-yr-old graphics), likely drop. Provenance (who/where it came from) matters.
- Unnamed files (generic iPhone names, etc.): some analysis happens in CPMS; but some Word/PDFs here need content analysis to define what they are (legal agreements, commission calcs, groups of related data).
- Discoverability of the final organized set matters a lot.

---

## Operational notes / gotchas
- Backend keeps dying between sessions — it's fine, just restart it (only affects the UI, not the detached hashing/blueprint jobs). Start it with env vars:
  `$env:ORGANIZED_ROOT="E:\Google Drive Files\Organized"; $env:SNAPSHOT_ROOT="E:\Dropbox-Snapshot"; $env:GAM_ADMIN_USER="sherman@hplapidary.com"; $env:GAM_PATH="C:\GAM7\gam.exe"; & ".\venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 4600 --log-level warning`
- control_pwsh_process often reuses a DEAD terminal on first try (isReused:true) and won't actually start. Then stop it, verify port 4600 free, start again for a real fresh process.
- PowerShell console sometimes swallows multi-line command output — use a scratch .py/.ps1 file and run it, or keep commands single-line. Delete scratch files after.
- Machine won't sleep (confirmed) — long jobs run overnight fine.
- URL: http://localhost:4600 (hard-refresh Ctrl+Shift+R for new bundles).
- GAM at C:\GAM7\gam.exe, admin sherman@hplapidary.com. Marketing Dropbox shared drive id 0ABPjSvMd3ZyaUk9PVA.
- Frontend build: `cd frontend; npm run build`.

## Immediate next actions for the new session
1. Commit crossdedup.py + router to git.
2. Check if Organized hashing finished (supervisor json / DB counts).
3. If done → STEP 1 (folder cleanup + validate), then STEP 2 (cross-dedup keep=organized).
4. Build STEP 3(a): principles chat + rules engine. Prompt user for their voice-input code.
