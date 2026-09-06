"""
Folder Re-Naming Advisor — recovers truncated / garbled folder names by looking
at the FILES INSIDE each folder, not by guessing from the (broken) folder name.

Strategy (cheap-first, evidence-based, never guess):
  Tier 1 — FILENAME evidence (free, no LLM): descriptive files inside often carry
    the real title, e.g. a folder "...Diagnosing Problems and Pr" contains
    "6th Webinar (Diagnosing Problems and Proper Ad.pptx". We mine those names.
  Tier 2 — CONTENT evidence (LLM reads REAL bytes via content_reader): when the
    filenames are generic ("Webinar Visual Materials.pptx"), the LLM reads the
    actual document/slide text and proposes the full name from real content.
  Tier 3 — LOW CONFIDENCE / needs_review: if neither yields solid evidence, we say
    so with low confidence rather than inventing a name.

Every proposal carries a CONFIDENCE score (0..1) and the EVIDENCE it came from, so
the user can auto-apply the high-confidence ones and only eyeball the rest — using
humans as the last resort, exactly as intended.

ALL LLM calls go through AWS Bedrock (Converse API). Nothing leaves the AWS
boundary. The model is configurable via env var; default is the cheap DeepSeek
Bedrock inference profile.
"""
import os
import re
import json
import time
import threading
from typing import Optional

from .bedrock_client import get_bedrock_client
from .content_reader import read_content

# Model is resolved at call time from the central Settings store (UI-settable),
# falling back to env var then a cheap Bedrock default. Never leaves AWS.
from . import settings_store

def _default_model() -> str:
    return settings_store.get_model("folder_namer")

_analyze_jobs: dict = {}
_proposal_cache: dict = {}   # folder_path -> proposal

# Files whose NAMES tend to carry the real title, best first.
_TITLE_EXTS_PRIORITY = [".pptx", ".docx", ".pdf", ".key", ".txt", ".md"]
# Generic filenames that carry no title signal (force Tier 2 content read).
_GENERIC_NAME_RX = re.compile(
    r"^(untitled|document\d*|presentation\d*|visual materials|webinar visual|"
    r"slides?|deck|final|copy|new|image\d*|img\d*|photo\d*|banner\d*|\d+)$",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You recover the correct, human-readable name of a FOLDER whose \
name was truncated or garbled during a data migration. You are given real EVIDENCE \
gathered from the files inside the folder (descriptive filenames and, when needed, \
actual extracted text/slide content).

ABSOLUTE RULES:
1. Base the proposed name ONLY on the evidence provided. Do NOT invent details that \
the evidence does not support.
2. You MUST quote the specific evidence you used.
3. If the evidence is weak or generic, return a LOW confidence and set \
"needs_review": true — do NOT fabricate a confident name.
4. Keep the proposed name a valid folder name: no characters \\ / : * ? " < > |, and \
preserve any leading date/number prefix that the current name already has if it \
looks intentional (e.g. "22.02.25 Live Webinar - ").

Respond ONLY with valid JSON:
{
  "proposed_name": "the corrected folder name",
  "confidence": 0.0-1.0,
  "evidence": "the exact filename or content snippet you based this on",
  "needs_review": true|false,
  "rationale": "1-2 sentences"
}"""

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize_folder_name(name: str) -> str:
    name = _INVALID_CHARS.sub("", name or "").strip().rstrip(". ")
    return name[:180]  # keep well under path limits


def _looks_truncated(name: str) -> bool:
    """Heuristics for a name that was likely cut off / mangled."""
    n = name.rstrip()
    if n.endswith("_") or n.endswith("-"):
        return True
    # ends mid-word abbreviation like "and Pr", "Silversmith" (no closing), "Coolan"
    last = n.split()[-1] if n.split() else ""
    # very long (near a path/name limit) is suspicious
    if len(name) >= 60:
        return True
    # ends without terminal punctuation and last token is short + capitalized-partial
    if last and 2 <= len(last) <= 6 and last[0].isalpha() and not n.endswith((".", ")", "]")):
        # could be a cut word; weak signal only
        return True
    return False


def _prefix_of(name: str) -> str:
    """Preserve an intentional leading date/number/label prefix, e.g.
    '22.02.25 Live Webinar - '. Returns the prefix (may be empty)."""
    m = re.match(r"^\s*([\d]{1,4}[.\-/][\d]{1,2}[.\-/][\d]{1,4}\s*[-–]?\s*"
                 r"(?:[A-Za-z ]{0,24}[-–]\s*)?)", name)
    return m.group(1) if m else ""


def _gather_evidence(folder: str, max_files: int = 400) -> dict:
    """Walk the folder, collect the most title-bearing filenames and pick a
    representative content-bearing file (for Tier 2). No LLM here."""
    title_files: list[str] = []       # (descriptive) candidate title filenames
    best_content_file: Optional[str] = None
    best_priority = len(_TITLE_EXTS_PRIORITY)
    n = 0
    for root, dirs, files in os.walk(folder):
        for fn in files:
            n += 1
            if n > max_files:
                break
            stem, ext = os.path.splitext(fn)
            ext = ext.lower()
            if ext in _TITLE_EXTS_PRIORITY:
                # candidate whose NAME may hold the title
                if not _GENERIC_NAME_RX.match(stem.strip()):
                    title_files.append(fn)
                # track best content-bearing file for Tier 2
                pr = _TITLE_EXTS_PRIORITY.index(ext)
                if pr < best_priority:
                    best_priority = pr
                    best_content_file = os.path.join(root, fn)
        if n > max_files:
            break
    # Rank title filenames: longer + more words = more descriptive
    title_files.sort(key=lambda f: (len(os.path.splitext(f)[0]), f), reverse=True)
    return {
        "title_filenames": title_files[:8],
        "content_file": best_content_file,
        "file_count_sampled": n,
    }


def propose_for_folder(folder: str, model_id: Optional[str] = None,
                       use_cache: bool = True, read_content_if_needed: bool = True) -> dict:
    """Produce a rename proposal for one folder, evidence-based + confidence."""
    model_id = model_id or _default_model()
    if use_cache and folder in _proposal_cache:
        return _proposal_cache[folder]

    current = os.path.basename(folder.rstrip("/\\"))
    prefix = _prefix_of(current)
    ev = _gather_evidence(folder)

    # ---- Tier 1: try to recover from a descriptive filename (free) ----
    tier = None
    proposal = None
    for fn in ev["title_filenames"]:
        stem = os.path.splitext(fn)[0]
        # e.g. "6th Webinar (Diagnosing Problems and Proper Ad" -> pull the phrase
        cleaned = re.sub(r"^\d+(st|nd|rd|th)?\s*(webinar)?\s*[\(\-:]?\s*", "",
                         stem, flags=re.IGNORECASE).strip(" ()-")
        if len(cleaned) >= 8 and not _GENERIC_NAME_RX.match(cleaned):
            candidate = _sanitize_folder_name((prefix + cleaned) if prefix and not cleaned.lower().startswith(prefix.lower()) else cleaned)
            # Confidence: only high if it clearly EXTENDS the truncated current name.
            trunk = re.sub(r"[_\-\s]+$", "", current).lower()
            conf = 0.6
            if trunk and trunk.replace(prefix.lower(), "").strip() and \
               candidate.lower().startswith(trunk[:max(8, len(trunk) - 4)]):
                conf = 0.9
            proposal = {
                "proposed_name": candidate,
                "confidence": conf,
                "evidence": f"filename inside folder: {fn}",
                "needs_review": conf < 0.8,
                "rationale": "Recovered from a descriptive file name inside the folder.",
                "tier": 1,
            }
            tier = 1
            break

    # ---- Tier 2: LLM reads REAL content when Tier 1 found nothing OR only a
    # weak/low-confidence filename guess. Reading the actual slide/doc content
    # (via the cheap Bedrock model) usually beats a truncated filename. ----
    tier1_weak = proposal is not None and (proposal.get("confidence", 0) < 0.8
                                           or proposal.get("needs_review"))
    if (proposal is None or tier1_weak) and read_content_if_needed and ev["content_file"]:
        content_info = read_content(ev["content_file"])
        if content_info.get("read"):
            tier1_proposal = proposal   # remember the filename guess as fallback
            body = (
                f"CURRENT (broken) folder name: {current!r}\n"
                f"Preserve this leading prefix if present: {prefix!r}\n\n"
                f"EVIDENCE — descriptive filenames inside:\n"
                + json.dumps(ev["title_filenames"], ensure_ascii=False, indent=2)
                + f"\n\nEVIDENCE — actual extracted content of "
                f"{os.path.basename(ev['content_file'])} "
                f"({content_info.get('kind')}):\n```\n"
                + (content_info.get("content") or "")[:20000]
                + "\n```"
            )
            llm_proposal = _ask_llm(body, model_id)
            llm_proposal["tier"] = 2
            # Keep whichever is stronger: the LLM content-based proposal or the
            # Tier-1 filename guess. Prefer the LLM when it produced a usable name
            # with >= the filename guess's confidence.
            if (llm_proposal.get("proposed_name") and
                    llm_proposal.get("confidence", 0) >= (tier1_proposal.get("confidence", 0) if tier1_proposal else 0)):
                proposal = llm_proposal
                tier = 2
            else:
                proposal = tier1_proposal   # fall back to the filename guess
                tier = 1
        else:
            # content unreadable -> fall back to Tier-1 guess if we had one
            proposal = tier1_proposal
            tier = 1 if tier1_proposal else None

    # ---- Tier 3: nothing solid — low confidence, needs review (no guessing) ----
    if proposal is None:
        # last resort: if there were ANY title filenames, offer weakly; else flag
        weak = ev["title_filenames"][0] if ev["title_filenames"] else None
        proposal = {
            "proposed_name": current,   # keep current; we won't invent
            "confidence": 0.15 if weak else 0.0,
            "evidence": (f"only weak filename: {weak}" if weak
                         else "no title-bearing files found inside"),
            "needs_review": True,
            "rationale": "Insufficient evidence to confidently rename; left as-is for review.",
            "tier": 3,
        }
        tier = 3

    result = {
        "folder": folder.replace("\\", "/"),
        "current_name": current,
        "prefix": prefix,
        "files_sampled": ev["file_count_sampled"],
        "content_file_used": (ev["content_file"] or "").replace("\\", "/") if tier == 2 else None,
        "model": model_id if tier == 2 else None,
        **proposal,
    }
    # Never propose an identical or empty rename as a "change".
    result["is_change"] = bool(result["proposed_name"]) and \
        result["proposed_name"].strip() != current.strip()
    _proposal_cache[folder] = result
    return result


def _ask_llm(body: str, model_id: str) -> dict:
    """Call Bedrock (Converse) and parse the JSON proposal. Stays on AWS."""
    try:
        client = get_bedrock_client()
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": body}]}],
            system=[{"text": SYSTEM_PROMPT}],
            inferenceConfig={"maxTokens": 500, "temperature": 0.0},
        )
        text = ""
        for block in resp["output"]["message"]["content"]:
            if "text" in block:
                text += block["text"]
        text = text.strip()
        for fence in ("```json", "```"):
            if text.startswith(fence):
                text = text[len(fence):]
        if text.endswith("```"):
            text = text[:-3]
        data = json.loads(text.strip())
        data["proposed_name"] = _sanitize_folder_name(data.get("proposed_name", ""))
        # enforce contract: no evidence -> force review + low confidence
        if not (data.get("evidence") or "").strip():
            data["needs_review"] = True
            data["confidence"] = min(float(data.get("confidence", 0)), 0.3)
        data.setdefault("needs_review", float(data.get("confidence", 0)) < 0.8)
        return data
    except Exception as e:
        return {
            "proposed_name": "",
            "confidence": 0.0,
            "evidence": "",
            "needs_review": True,
            "rationale": f"LLM error: {e}",
        }


# ------------------------------------------------------------- batch analyze job

def analyze_parent(parent: str, model_id: Optional[str] = None,
                   only_suspected: bool = True, max_folders: int = 500) -> str:
    """Start a background job that proposes names for the immediate subfolders of
    `parent`. Returns job_id. only_suspected=True limits to folders that look
    truncated/garbled (cheap: skips obviously-fine names)."""
    model_id = model_id or _default_model()
    job_id = f"foldernamer_{int(time.time())}"
    _analyze_jobs[job_id] = {
        "status": "running", "parent": parent, "model": model_id,
        "total": 0, "done": 0, "current": "", "results": [],
        "started_at": time.time(), "error": None, "cancelled": False,
    }
    threading.Thread(target=_analyze_worker,
                     args=(job_id, parent, model_id, only_suspected, max_folders),
                     daemon=True).start()
    return job_id


def get_analyze_job(job_id: str):
    return _analyze_jobs.get(job_id)


def cancel_analyze(job_id: str) -> bool:
    if job_id in _analyze_jobs:
        _analyze_jobs[job_id]["cancelled"] = True
        return True
    return False


def _analyze_worker(job_id, parent, model_id, only_suspected, max_folders):
    job = _analyze_jobs[job_id]
    try:
        if not os.path.isdir(parent):
            job["status"] = "error"; job["error"] = "parent folder not found"; return
        subs = [os.path.join(parent, d) for d in os.listdir(parent)
                if os.path.isdir(os.path.join(parent, d))]
        if only_suspected:
            subs = [s for s in subs if _looks_truncated(os.path.basename(s))]
        subs = subs[:max_folders]
        job["total"] = len(subs)
        for s in subs:
            if job.get("cancelled"):
                job["status"] = "cancelled"; return
            job["current"] = os.path.basename(s)
            try:
                job["results"].append(propose_for_folder(s, model_id))
            except Exception as e:
                job["results"].append({
                    "folder": s.replace("\\", "/"),
                    "current_name": os.path.basename(s),
                    "proposed_name": os.path.basename(s),
                    "confidence": 0.0, "needs_review": True, "is_change": False,
                    "evidence": "", "rationale": f"error: {e}", "tier": 3,
                })
            job["done"] += 1
        # rank: changes first, then lowest confidence first (review those)
        job["results"].sort(key=lambda r: (not r.get("is_change", False),
                                           r.get("confidence", 0)))
        job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


# ------------------------------------------------------------------- apply (safe)

APPLY_STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "folder_renames")


def apply_renames(items: list[dict], confirm: bool = False) -> dict:
    """Apply approved folder renames. items: [{folder, proposed_name}].
    Collision-safe (never overwrite an existing sibling) and REVERSIBLE — every
    rename is recorded to a manifest so it can be undone. Never deletes."""
    if not confirm:
        return {"applied": False, "reason": "confirm=false; no changes made"}
    os.makedirs(APPLY_STORE, exist_ok=True)
    manifest_path = os.path.join(APPLY_STORE, f"rename_{int(time.time())}.json")
    results = {"applied": True, "renamed": 0, "skipped": 0, "errors": [],
               "manifest": manifest_path, "moves": []}
    for it in items:
        src = it.get("folder")
        new_name = _sanitize_folder_name(it.get("proposed_name", ""))
        if not src or not new_name or not os.path.isdir(src):
            results["skipped"] += 1; continue
        parent = os.path.dirname(src.rstrip("/\\"))
        if new_name == os.path.basename(src.rstrip("/\\")):
            results["skipped"] += 1; continue
        dest = os.path.join(parent, new_name)
        # collision-safe: never overwrite an existing folder
        if os.path.exists(dest):
            i = 2
            while os.path.exists(os.path.join(parent, f"{new_name} ({i})")):
                i += 1
            dest = os.path.join(parent, f"{new_name} ({i})")
        try:
            os.rename(src, dest)
            move = {"from": src.replace("\\", "/"), "to": dest.replace("\\", "/")}
            results["moves"].append(move)
            results["renamed"] += 1
            # write manifest incrementally so a crash mid-batch is still reversible
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump({"created_at": time.time(), "moves": results["moves"]}, f, indent=2)
        except OSError as e:
            results["errors"].append({"folder": src, "error": str(e)})
    return results


def undo_renames(manifest_path: str, confirm: bool = False) -> dict:
    """Reverse a previous apply using its manifest (reversibility guarantee)."""
    if not confirm:
        return {"undone": False, "reason": "confirm=false"}
    if not os.path.exists(manifest_path):
        return {"undone": False, "reason": "manifest not found"}
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    res = {"undone": True, "restored": 0, "errors": []}
    for mv in reversed(data.get("moves", [])):
        cur, orig = mv["to"], mv["from"]
        try:
            if os.path.isdir(cur) and not os.path.exists(orig):
                os.rename(cur, orig)
                res["restored"] += 1
        except OSError as e:
            res["errors"].append({"folder": cur, "error": str(e)})
    return res
