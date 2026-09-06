# Content-based AI naming — research learnings & how we apply them

Findings from the commercial landscape (renamer.ai, NameQuick) and academic
literature, distilled into concrete design rules for this tool and for the CPMS
platform build. All source content below is **rephrased/summarized for licensing
compliance**; links are provided for attribution.

## The market validates the concept

AI content-based renaming is a real, active 2026 category. Tools like
[renamer.ai](https://renamer.ai/ai-file-renamer) and
[NameQuick](https://www.namequick.app/blog/ai-file-renaming) do the same core loop
we're building: open the file, OCR documents / run vision on images, propose a
descriptive name, and rename **only after the user approves a preview**. Their
cited example — an invoice becoming `Tidewater_BOL-4471_2026-03-18` — is exactly
"AI-read content poured into a naming convention," confirming this is *both* AI
*and* a naming format, not either/or.

Notably, their recommended extraction stack (pdfplumber/PyPDF2, python-docx,
openpyxl) is the **same stack we already use** — we are at parity on extraction.

Our differentiation is NOT out-polishing them locally; it's (a) running entirely
inside the customer's **AWS/Bedrock boundary** (their files never leave the
tenant), and (b) feeding a **durable learning knowledge base** on the CPMS
platform. See `Content-Creation-Platrform/docs/INGEST-INTELLIGENCE-HANDOFF.md`.

## Learning 1 — reading strategy is decided by file type; images/scans are the trap

Source: [renamer.ai — rename by content, every file type](https://renamer.ai/how-to-rename-files-based-on-content).

- text / Office (.docx/.xlsx/.pptx) / born-digital PDF → extractable text layer.
- images / scanned PDF / photo-of-document → **pixels only**; need OCR or vision.
- The #1 silent failure: a script hits an image, extracts an empty string, and
  either skips it or writes a blank name **while reporting success** — you don't
  notice until you go looking.

**How we apply it:** `content_reader` routes by type; `doc_images` extracts &
OCRs embedded images in image-heavy docs; unreadable files return an EXPLICIT
`read=false` / `needs_review` marker — never a silent blank. Keep this loud.

## Learning 2 — confidence-aware abstention (the big one)

Sources: I-CALM ([arxiv 2604.03904](https://arxiv.org/html/2604.03904)),
Pareto self-supervision calibration ([arxiv 2306.16564](https://arxiv.org/html/2306.16564v2)),
decision-theoretic confidence ([arxiv 2604.03216](https://arxiv.org/html/2604.03216v1)).

Key findings (rephrased):
- LLMs frequently produce **confident but incorrect** answers because typical
  scoring rewards answering over honestly expressing uncertainty.
- The desirable behavior under uncertainty is to **abstain**, not to emit a
  plausible-but-wrong answer.
- Calibrated confidence is essential to detect errors and enable
  **human-in-the-loop verification**.
- Confidence should **guide decisions under the risk of the action** — a
  high-stakes action warrants a higher confidence bar than a low-stakes one.

**How we apply it:**
- Anti-guessing contract already: cite evidence or return `needs_review`; downgrade
  delete-safe claims lacking evidence (dedup advisor).
- **Refinement — risk-weighted auto-approve thresholds.** Different actions carry
  different risk, so their default confidence bar should differ:
  - Rename a file (reversible): low risk → lower threshold (e.g. 0.8) OK.
  - Move/quarantine across sources: medium risk → higher bar.
  - Anything that removes/deletes: high risk → highest bar + always human-confirmed.
  - Never auto-apply below the action's bar; those go to review, sorted worst-first.

## Learning 3 — content-derived (abstractive) identifiers beat heuristics

Sources: LMIndexer ([arxiv 2310.07815](https://arxiv.org/html/2310.07815v1)),
ACID summarization IDs ([arxiv 2311.08593](https://arxiv.org/html/2311.08593v2)).

Abstractive, content-based identifiers (a short summary-derived name) outperform
naive path/first-tokens approaches — validating that an AI-summarized descriptive
name is genuinely better than filename/path inference. Reinforces: name from
content, not from the old name.

## Learning 4 — layered validation, LLM as the semantic stage

Source: BIM/CDE file-name validation ([MDPI 2075-5309/16/14/2886](https://www.mdpi.com/2075-5309/16/14/2886)).

Best practice is a **three-stage pipeline**: syntactic (regex) → referential
(against project lookups/known values) → **LLM semantic** (compare the proposed
name's fields against the actual document content). The LLM is the semantic check
in a layered system, not a one-shot oracle.

**How we apply it:**
- Syntactic: our filename sanitizer + ill-named detector.
- Referential: **validate proposed names/labels against a known taxonomy** —
  vendors, material types ("laguna agate"), campaigns. Locally this is light;
  on CPMS this is the **learning knowledge base** (confirmed exemplars), which is
  exactly the referential layer the paper describes.
- Semantic: the LLM proposal grounded in real content + cited evidence.

## Learning 5 — cheap-first routing, escalate only for hard cases

Source: fast file-name classifier ([arxiv 2410.01166](https://www.arxiv.org/pdf/2410.01166))
— a lightweight classifier handled 90%+ of documents at ~99% accuracy, ~442×
faster than heavy models.

**How we apply it:** free filename-tier check first, then the cheap Bedrock text
model, escalating to a stronger/vision model only when needed. The per-function
model Settings we added let each stage use the cheapest adequate model.

## Concrete changes to make (local tool)

1. **Risk-weighted default thresholds** per action type (rename < move < delete),
   surfaced in the review UI. (Learning 2)
2. Keep the **explicit-unreadable** behavior loud in the UI (a distinct
   "couldn't read — needs review" bucket, not mixed into low-confidence). (L1)
3. **Referential validation hook** — optional list of known terms
   (vendors/materials/campaigns) the namer can prefer/snap to; seeds the CPMS
   knowledge base later. (L4)
4. Keep tiered cheap-first routing; expose model choice per stage (done via
   Settings). (L5)

## Concrete changes (CPMS platform — the durable version)

- The **learning knowledge base** IS the referential layer (L4) + abstractive IDs
  (L3): confirmed exemplars (Titan multimodal embeddings on Aurora pgvector) let
  new assets be classified by nearest confirmed neighbor, with calibrated
  confidence driving human-in-the-loop review (L2). Detailed in
  `INGEST-INTELLIGENCE-HANDOFF.md`.
