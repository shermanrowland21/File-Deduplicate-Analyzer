# SharePoint (and multi-source) dedup — design roadmap

Status: **design note only, not yet implemented.** Captured while the local
dedup + resolver + folder-renamer pipeline is being finished. Purpose: record the
source-abstraction decision before we add more local-only code that would be
harder to refactor later.

## Goal

Extend the app beyond the local filesystem so it can dedupe and manage files that
live in **SharePoint** document libraries (and, by the same abstraction, Google
Drive and OneDrive). SharePoint in particular becomes a sprawl of near-duplicate
documents ("Copy of Copy of Final_v3") across libraries with no native
cross-library dedup — exactly the problem this tool solves locally.

## Core insight / the shift

The current app scans a **local filesystem**: it walks directories (`os.walk`) and
hashes bytes on disk. SharePoint is a **remote, API-backed store** — we don't have
the bytes locally. The strategy adapts, but the architecture already has the right
bones.

## What carries over unchanged

- **Hash-based dedup logic** — duplicates are still "same content = same hash."
- **The Resolver** — source-of-truth selection, quarantine-not-delete, protected
  structural buckets, reversible manifests. On SharePoint, "quarantine" = move to a
  designated library/folder or apply a retention label, never hard-delete.
- **AI content-advisor + folder-namer** — directly reusable; SharePoint files are
  exactly the docx/pptx/pdf that `content_reader` already extracts text from.
- **Crash-durable checkpointing + persisted results** — arguably MORE important for
  remote sources, since API enumeration is slower and rate-limited.
- **"Never leave the boundary" rule** — honored. Microsoft Graph is Microsoft's own
  first-party API for the customer's own tenant (same category as GAM for Google).
  No third-party endpoints.

## What must change for SharePoint

1. **No local walk — use Microsoft Graph API.**
   Enumerate document libraries via Graph:
   `/sites/{site-id}/drives/{drive-id}/items` (SharePoint/OneDrive share the same
   drive backend). This replaces `os.walk`. Enumeration must be **paged and
   backoff-aware** because SharePoint APIs throttle.

2. **Hash WITHOUT downloading — the big win.**
   Graph returns a `file.hashes` facet per item:
   - `quickXorHash` — SharePoint/OneDrive's native hash, **always present**.
   - `sha1Hash` / `sha256Hash` — sometimes present, not guaranteed.
   So we can detect duplicates from **metadata alone, zero bytes downloaded**. This
   is the SharePoint equivalent of the local smart-sample hashing and makes
   cross-library dedup cheap and fast.

3. **Content reading is download-on-demand.**
   Only download the **one representative per hash group** for the AI advisor /
   folder-namer to read (same pattern already used locally). Never bulk-download.

4. **Auth via Azure AD app registration.**
   Graph client-credentials (app-only) or delegated flow, scoped to the tenant.
   Read token/secret from env vars; never commit. Mirrors the GAM approach.

## Proposed architecture: a source abstraction

Introduce a common **FileSource** interface so nothing downstream is hardwired to
local disk. A shared "file record" — `{id/path, size, hash, hash_kind, source,
source_path, mime, modified}` — is emitted by every source, so the scanner,
resolver, advisor, and folder-namer all work unchanged regardless of origin.

Sources:
- `LocalFileSource` — current behavior: walk + smart-hash bytes on disk.
- `SharePointSource` — Graph: page items, read `file.hashes`, download-on-demand
  for content.
- `GoogleDriveSource` — formalize the existing GAM plumbing behind the same
  interface (Drive exposes an MD5 checksum, already used in `compute_md5`).

Payoff: **cross-source dedup for free** — e.g. "this file exists in SharePoint AND
your local Organized folder AND Google Drive." The Resolver's source-of-truth model
extends naturally to "keep the SharePoint copy, quarantine the local duplicates" or
vice versa.

## Honest caveats / open problems

- **Graph throttling** — big libraries need paged enumeration with retry/backoff.
  The existing checkpoint model handles interruptions well, which helps.
- **Hash normalization for cross-store matching** — `quickXorHash` is always there
  but local files are hashed with SHA-256 / smart-sample. Cross-store dedup
  (SharePoint quickXorHash vs local SHA) needs either:
  (a) also compute quickXorHash locally for candidate matches, or
  (b) a size-gated download-and-rehash fallback for high-value suspected matches.
  Within a single SharePoint tenant, quickXorHash alone is sufficient.
- **Versioning & permissions** — SharePoint items carry version history and
  per-item permissions. "Quarantine not delete" should preserve version history
  (move + retain), and enumeration/actions must respect who-can-see-what.
- **Scale** — enterprise libraries can be millions of items; the persisted-scan +
  resume-without-rescan work already in progress is a prerequisite, not a nicety.

## Recommended first step (small, validating)

Do NOT build the full abstraction first. Start with a **read-only SharePoint
enumerator**:
1. Azure AD app registration (read-only Sites.Read.All or narrower).
2. Page one document library via Graph, pull each item's `file.hashes.quickXorHash`.
3. Feed those into the EXISTING dedup grouping (group by hash).
4. Show cross-library duplicates with **zero downloads.**

That single spike proves the model — cross-library dedup from metadata — before
investing in the full `FileSource` refactor. Only after it's validated do we
generalize `LocalFileSource` / `SharePointSource` / `GoogleDriveSource` behind the
common interface.

## Prerequisites already in place / in progress

- Persisted scan results + `resume-without-rescan` (load prior results, no walk).
- Crash-durable checkpointing (atomic writes every N files).
- Incremental hash cache (path+size+mtime keyed).
- AI advisor + folder-namer with anti-guessing contract, on AWS Bedrock only.
- Resolver with quarantine-not-delete + reversible manifests.

## Non-goals (for now)

- Real-time sync / continuous monitoring of SharePoint (this is a batch analyze +
  resolve tool, not a sync engine).
- Modifying SharePoint content in place beyond move-to-quarantine / labeling.
- Any processing outside the customer's own AWS + Microsoft tenant boundary.
