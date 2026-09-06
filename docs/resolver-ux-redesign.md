# Resolver / Dedup UX redesign — modeled on proven tools

The current Resolver exposes the *engine* (source-of-truth field, cross-source vs
single-source categories, protected buckets). Users have to understand the
mechanism to use it — not intuitive. This redesign adopts the interaction models
of the two gold-standard dedup tools. (External content rephrased for licensing
compliance; links attribute sources.)

## What the leading tools do

### Gemini 2 (MacPaw) — "smart select + one button + restore"
Sources: [start scanning](https://macpaw.com/support/gemini/knowledgebase/start-scanning-for-duplicates),
[smart selection](https://macpaw.com/support/gemini/knowledgebase/gemini-2-smart-selection).
- After scan → a **summary screen**: a donut/among categories on the left (hover
  for counts), and on the right the **total auto-selected for removal** + a split
  by category.
- **Smart Selection auto-marks only 100%-safe duplicates**, and is
  **location-aware**: e.g. scanning Home marks dupes in Downloads/Desktop but
  leaves Documents alone — and **the original always stays safe**. (This is the
  intuitive form of "source of truth": you don't fill a field, the tool keeps the
  copy in the more-important location and marks the rest.)
- **One primary button** ("Smart Cleanup") acts on the safe set; **"Select More"**
  lets you go further manually.
- Removed files can be **restored** (reversibility is a first-class promise).

### dupeGuru — "reference + marked duplicates, keeper is locked"
Source: [results docs](https://dupeguru.voltaicideas.net/help/en/results.html).
- Results are **duplicate groups**: each has ONE **reference (keeper)** + indented
  **duplicate** rows with checkboxes.
- The reference's checkbox is **disabled** — you can *never* mark the keeper for
  deletion. Built-in safety.
- Reference is auto-chosen (biggest file, or any file inside a **reference
  folder**), and any file can be promoted with one click ("Make Selected into
  Reference").
- **Reference folder** = a folder whose files always become the keeper (never
  deleted). This is the clean version of our "source of truth."
- **Filter box** narrows results across the whole path (e.g. everything with
  "copy" in it). Review → mark → send to recycle bin.

## Redesign for our Resolver

Keep our stronger backend (structure-aware protection of templates/frameworks,
cross-source + within-source, quarantine-not-delete, AI advisor). Change the
PRESENTATION to match the proven models. Hide the engine; expose outcomes.

### 1. Summary-first (Gemini model)
On analyze, lead with a one-line outcome banner + a few numbers:
> **X duplicate groups · Y GB reclaimable · Z copies auto-selected (safe) · N protected**
Primary action button: **"Quarantine selected (reversible)."** Secondary: "Review
each group." No jargon ("cross_source_redundant") in the headline.

### 2. Per-group cards (dupeGuru model)
Each group is a card:
- **Keeper** row — green, a lock/★ icon, checkbox DISABLED (can't be removed).
- **Duplicate** rows — checkboxes; safe ones **pre-checked** by Smart Select.
- Each non-keeper has a **"Make keeper"** action (one click to change which copy
  survives).
- Show path, size, source, modified date per row. Highlight the differing bits.

### 3. "Keep folders" instead of "Source of truth" (rename + reframe)
Replace the bare "Source of truth" text field with **"Protected / preferred
folders"**: folders whose files are always the keeper and never removed. Same
backend field, human label + a Browse picker + the folder filter box. Structural
/ framework files (templates, node_modules, .git, .cshtml) are **auto-protected**
and shown in a clearly-labeled, collapsed "Protected — won't touch" section with
the reason.

### 4. Smart Select (Gemini model) — safe defaults, location-aware
Auto-select for removal only the copies that are clearly safe: exact-hash
duplicates where a keeper exists in a preferred folder (or the biggest/oldest),
and never a structural file. Everything ambiguous is left UNchecked for review.
A "Select more" affordance reveals the riskier candidates. Risk-weighted: rename
is cheap, quarantine/delete demands the higher bar (see content-naming-research.md
Learning 2).

### 5. Filter + reversibility
- A **filter box** over paths (find "copy", or a subfolder) — dupeGuru pattern.
- **Undo/Restore** button always visible after an action (we already quarantine +
  write a manifest; surface the restore).

### 6. Within-source vs cross-source — make it a plain toggle, not jargon
Two plain-language modes at the top:
- **"Clean inside one folder"** (within-source): find files copied to multiple
  places inside the same tree; keep one, quarantine the rest (structural still
  protected).
- **"Compare two sources"** (cross-source): find files that exist in BOTH (e.g.
  Dropbox and Google Drive); keep the preferred source's copy.
The backend already supports both; the UI just picks the mode in words.

## Anatomy (layout sketch)

```
┌─ Resolver ─────────────────────────────────────────────┐
│ Scan: [ Dropbox — 49,715 groups ▼ ]   Mode: (•)Clean inside ( )Compare two │
│ Keep / preferred folders: [ D:/Highland Park Dropbox/… ] [Browse] [filter…]│
│                                                                            │
│  ✅ 12,340 copies auto-selected · 136 GB reclaimable · 18,919 protected     │
│  [ Quarantine selected (reversible) ]   [ Select more ]   [ Undo last ]     │
├────────────────────────────────────────────────────────┤
│ Filter results: [ copy____________ ]                                       │
│                                                                            │
│  ▸ group (2 copies, 7.07 GB)                                               │
│     ★ KEEP  D:/…/GrowthCon 2022/2022-03-27 21-14-57.mkv   (locked)         │
│     ☑ remove D:/…/Backup/2022-03-27 21-14-57.mkv   [Make keeper]           │
│  ▸ group …                                                                 │
│                                                                            │
│  ▸ Protected — won't touch (18,919)  [why?]                                │
└────────────────────────────────────────────────────────┘
```

## Backend deltas needed
- `within_source` mode in resolver.analyze/execute (in progress) so "Clean inside
  one folder" works and still protects structural files.
- Per-group payload for the review cards: keeper + removable flags + reason, and a
  "smart-selected" boolean per removable copy.
- Reuse existing quarantine + manifest for Undo/Restore.

## Non-negotiables preserved
Quarantine-not-delete, structural protection, AI advisor for ambiguous groups,
confidence/evidence, risk-weighted thresholds — unchanged. Only the presentation
becomes outcome-first and card-based.
