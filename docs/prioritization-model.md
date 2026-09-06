# Keeper prioritization — adopting dupeGuru's proven model

Studied the open-source **dupeGuru** ([github.com/arsenetar/dupeguru](https://github.com/arsenetar/dupeguru),
GPLv3, Python+Qt) to fix the rough "Make keeper per group" UX. We learn the
*algorithm/pattern* and write our own implementation — we do NOT copy GPL source.

## What dupeGuru does (the key insight)

Choosing which copy survives is done by **prioritization rules applied in bulk**,
not by clicking each group:

- Each duplicate group has a **reference (keeper)** = the file that sorts to the
  TOP of the group under a `key_func`. (`core/engine.py` `Group.prioritize`:
  `sorted(ordered, key=key_func)`, top becomes ref.)
- The user builds an ordered **priority list** of criteria (`core/prioritize.py`):
  - **Folder** — files under a chosen folder sort to top (its `sort_key` returns
    0 if the file is `relative_to(chosen_folder)`, else 1). ← this is exactly
    "keep files in Nermeen's path."
  - **Filename** — longest / shortest / ends-with-number / longest-path /
    shortest-path.
  - **Size** — highest / lowest.
  - **Modification time** — newest / oldest.
- `reprioritize_groups(sort_key)` applies ONE key across ALL groups at once — so
  picking "prefer folder X" instantly makes X the keeper in every group and marks
  the rest for removal. No per-card clicking.

(Behavior rephrased from dupeGuru docs/source for GPL compliance; we reimplement.)

## How we adopt it (our Resolver)

Replace per-card "Make keeper" with a small **"Keeper rules"** builder:

1. **Prefer files in folder:** [pick a path]  ← primary; solves the Nermeen case
2. Tiebreakers (optional, ordered): Newest | Oldest | Longest name | Shortest
   path | Biggest | Smallest.

Applying the rules:
- For each group, sort copies by the rule stack; the top is the keeper.
- Every non-keeper, non-structural copy becomes a removal candidate (bulk).
- Structural/framework files still protected; junk (`desktop.ini`, `.DS_Store`,
  `Thumbs.db`) filtered out entirely.
- Show the bulk outcome: "Keeping copies in <folder>; N other copies (Y GB) will
  be quarantined." One button. Cards remain available to spot-check/override.

## Backend

- Add a `keeper_rules` concept to `_plan_removals_for_group`: a ranked key that
  picks the keeper (folder-preference first, then tiebreakers), replacing the
  current "biggest under source_of_truth else biggest."
- `source_of_truth` / "preferred folders" already maps to the folder rule — keep
  it, just make the UI drive it clearly and add tiebreakers.
- Junk filter in the review/plan builders.

## Non-negotiables preserved
Quarantine-not-delete + manifest/undo, structural protection, hash-based
grouping. Only the keeper-selection UX changes to the bulk prioritization model.
```
```
