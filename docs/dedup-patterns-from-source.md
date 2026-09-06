# Dedup UX patterns — learned from reading the source of 3 open-source tools

We cloned and read the actual source of the leading open-source dedup tools to
replace guesswork with proven patterns. This is the synthesis + the implementation
plan for our Resolver. We learn the *patterns/algorithms* and reimplement in our
own code (these are GPL/AGPL projects — no source copied).

Tools studied:
- **dupeGuru** — `github.com/arsenetar/dupeguru` (Python/Qt, GPLv3)
- **Czkawka / Krokiet** — `github.com/qarmin/czkawka` (Rust/Slint)
- **Nextcloud Duplicate Finder** — `github.com/eldertek/duplicatefinder` (PHP, AGPL)

---

## 1. dupeGuru — keeper = top of a sort; priority stack applied in bulk

Files read: `core/prioritize.py`, `core/engine.py` (`Group.prioritize`), `core/app.py`
(`reprioritize_groups`).

- Each duplicate group has a **reference (keeper)** = the file that sorts to the
  TOP under a `key_func`. `Group.prioritize(key_func)` = `sorted(ordered, key=...)`,
  top becomes ref.
- The user composes an ordered **priority list** of criteria (`prioritize.py`):
  - **Folder** — `sort_key` returns 0 if the file is `relative_to(chosen_folder)`,
    else 1 → files under the chosen folder become the keeper. **(This is exactly
    "keep files in Nermeen's path".)**
  - **Filename** — longest / shortest / ends-with-number / longest-path /
    shortest-path.
  - **Size** — highest / lowest. **Modification** — newest / oldest.
- `reprioritize_groups(sort_key)` applies ONE key across ALL groups at once → pick
  "prefer folder X" and X becomes the keeper everywhere; the rest are removable.
- Safety: **the reference's checkbox is disabled** — the keeper can never be
  marked for deletion.

**Take:** keeper selection is a bulk *rule*, not a per-group click.

## 2. Czkawka — bulk "select all except", + custom filters, + leave-one safety

Files read: `krokiet/src/connect_select/mod.rs`, `.../custom_select.rs` (both
heavily unit-tested).

- **One-click presets across ALL groups** (`SelectMode`):
  `SelectAllExceptNewest / ExceptOldest / ExceptBiggest / ExceptSmallest /
  ExceptLongestPath / ExceptShortestPath`, plus inverses and `InvertSelection`,
  `InvertSelectionInGroup`. `select_all_except_by_property` finds the extreme item
  to **spare** and selects all others. → "keep the best, mark the rest" in bulk;
  the user never touches individual rows.
- **Custom column selection** (`custom_select.rs`):
  - Filter by **FullPath / Path** with glob/regex (e.g. `*Nermeen*`) → selects
    every matching copy across all groups.
  - Numeric/date filters with operators `>= <= > < =` (size `>= 100`, date
    `< 2020-01-15`).
  - **`leave_one_in_group`** — CRITICAL safety: if a filter would select *every*
    copy in a group, it pops one back off so a whole group can never be selected
    for deletion. Disabled when reference folders are in use (a survivor is
    already guaranteed).
- Detects **hard links** and counts them once (avoids false duplicates).

**Take:** offer bulk presets + a path/attribute filter, always guarded by
"leave one in each group."

## 3. Nextcloud Duplicate Finder — origin folders + acknowledged state (persisted)

Files read: `lib/Service/OriginFolderService.php`, `FileDuplicateService.php`,
`lib/Db/FileDuplicate.php`.

- **`OriginFolder` + `isPathProtected(path)`** — the user registers "origin
  folders"; any path under one is **protected (never deleted)**. This is our
  "Keep / preferred folders" as a *persisted, first-class* concept, not a text box.
- **`acknowledged` flag per duplicate group** — mark a group reviewed and it drops
  out of the active list. Solves "hundreds of groups is overwhelming": triage,
  acknowledge handled ones, they stop cluttering the view.
- Runs server-side in a web UI (relevant to the CPMS future).

**Take:** persist preferred folders + let users acknowledge/dismiss groups.

---

## The synthesized design for our Resolver

Replace per-card "Make keeper" with a **rule-driven, bulk** model that combines
the three tools' best ideas. Our differentiators stay: structure-aware protection
(website/framework files), quarantine-not-delete + manifest/undo, AI advisor for
ambiguous cases, hash-based grouping.

### A. Keeper rule (dupeGuru) — the primary control
An ordered rule stack that picks the keeper in EVERY group at once:
1. **Prefer files in folder:** `[pick a path]`  ← solves the Nermeen case
2. Tiebreakers (optional, ordered): Newest | Oldest | Longest name | Shortest
   path | Biggest | Smallest.
Applied server-side: for each group sort by the rule → top is keeper → all
non-keeper, non-structural copies are removal candidates.

### B. Bulk selection presets + filter (Czkawka)
- One-click: "Select all except keeper", "…except newest", "…except in preferred
  folder".
- A **path filter** (glob) to bulk-select copies matching a pattern.
- **Always leave one per group** guardrail on any bulk select (never allow a whole
  group to be fully selected).

### C. Persisted preferred folders + acknowledge (Nextcloud)
- "Keep / preferred folders" persisted per user (reused across sessions), with
  `is_path_protected` semantics.
- **Acknowledge** a group (or a whole filter view) to remove it from the working
  list without deleting — for triaging big result sets.

### D. Hygiene
- Filter out junk (`desktop.ini`, `.DS_Store`, `Thumbs.db`) entirely.
- Detect hard links / already-identical-path cases and don't double-count.
- Structural/framework files remain protected and shown separately.

### E. Layout (outcome-first)
```
Scan [▼]   Keeper rule: prefer folder [ …/Nermeen … ] + tiebreak [Newest ▼]
Bulk: [Select all except keeper] [Except in preferred folder] [Filter path: ____]
      (always leaves one per group)
──────────────────────────────────────────────
✔ 12,340 copies selected · 136 GB reclaimable · 18,919 protected · leaves 1/group
[ Quarantine selected (reversible) ]   [ Undo last ]   [ Acknowledge reviewed ]
──────────────────────────────────────────────
group cards (spot-check / override) …      Protected — won't touch (collapsed)
```

## Backend deltas
- Keeper selection: replace ad-hoc "biggest under SoT" with a **rule stack**
  (folder-preference + tiebreakers) in `_plan_removals_for_group`.
- **leave-one-per-group** invariant enforced in the planner (never return a group
  with zero keepers).
- Junk-file + hard-link filters in the scan/review builders.
- Persist preferred folders + per-group `acknowledged` (small JSON/SQLite by
  scan id).

## Non-negotiables preserved
Quarantine-not-delete + manifest/undo, structural protection, hash-based grouping,
AI advisor, confidence/evidence, risk-weighted thresholds.

## For CPMS (carry-over)
The Nextcloud model (server-side, origin folders, acknowledge, per-user persisted
state, sharing awareness via `ShareService`) is the closest analog to the CPMS
build — see `Content-Creation-Platrform/docs/INGEST-INTELLIGENCE-HANDOFF.md`.
Sharing-awareness (don't remove a copy someone else depends on) belongs there,
backed by real permission metadata the local tool can't see.
