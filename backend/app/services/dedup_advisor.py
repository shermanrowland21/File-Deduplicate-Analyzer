"""
LLM dedup ADVISOR (Phase B) — classifies duplicate clusters by reading the
ACTUAL file content, with a strict anti-guessing contract.

Guiding principle (user directive): the model MUST base its judgment on real
file content, never on filenames/paths. Tokens don't matter; a correct call
does. So for each duplicate group we:
  1. read the actual content of ONE representative (all copies are byte-identical,
     so one read represents the whole group) via content_reader,
  2. give the model that real content PLUS the distinct paths (for context only),
  3. require it to CITE evidence from the content and to return
     'needs_human_review' / 'needs_more_content' rather than guess when the
     content is insufficient or unreadable (media/binary).

The advisor NEVER deletes. It returns a recommendation the human approves.
"""
import json
import time
import hashlib
from typing import Optional

from .bedrock_client import get_bedrock_client
from .content_reader import read_content
from .file_scanner import get_duplicates, human_readable_size

DEFAULT_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"

_advice_cache: dict = {}   # group_hash -> advice dict

SYSTEM_PROMPT = """You are a careful data-deduplication advisor. A user has \
byte-identical duplicate files and needs help deciding what is safe to remove.

ABSOLUTE RULES:
1. Base your judgment ONLY on the actual FILE CONTENT provided below. Do NOT \
infer meaning from filenames or folder paths — paths are given for CONTEXT ONLY \
(to see where copies live), never as evidence of what the file IS.
2. You MUST cite specific evidence from the content (quote a short snippet or \
name a concrete marker you saw) to justify your classification.
3. If the content was NOT provided (media/binary/unreadable) or is insufficient \
to judge safely, you MUST return classification "needs_human_review" (or \
"needs_more_content") — do NOT guess.
4. Never recommend deleting something that appears to be a functional part of a \
project/website/framework/library (templates, config, source, vendored deps), \
because other copies keep those projects working.

Classifications:
- "redundant_backup": the same content copied to multiple backup locations; \
  safe to keep one and remove the others.
- "project_internal": a framework/library/website/build file that each project \
  legitimately needs its own copy of; KEEP ALL.
- "versioned_content": distinct versions/revisions of an evolving document; \
  usually keep, human should choose.
- "needs_human_review" / "needs_more_content": cannot decide safely from content.

Recommended actions: "keep_one", "keep_all", "keep_source_of_truth", "human_review".

Respond ONLY with valid JSON:
{
  "classification": "...",
  "recommended_action": "...",
  "confidence": 0.0-1.0,
  "risk": "low" | "medium" | "high",
  "evidence": "the specific content snippet/marker you based this on",
  "rationale": "1-3 sentences explaining the decision, grounded in the content"
}"""


def _group_signature(group: dict) -> str:
    h = group.get("hash") or ""
    paths = "|".join(sorted(f["path"] for f in group.get("files", [])))
    return hashlib.md5((h + paths).encode("utf-8", "replace")).hexdigest()


def advise_group(group: dict, model_id: str = DEFAULT_MODEL,
                 use_cache: bool = True) -> dict:
    """
    Get an LLM recommendation for one duplicate group, based on REAL content.
    Cached by group signature.
    """
    sig = _group_signature(group)
    if use_cache and sig in _advice_cache:
        return _advice_cache[sig]

    files = group.get("files", [])
    paths = [f["path"] for f in files]
    rep = files[0]["path"] if files else None

    content_info = read_content(rep) if rep else {"read": False, "reason": "no file"}

    # Build the user message: REAL content + paths for context only.
    distinct_dirs = sorted({"/".join(p.replace("\\", "/").split("/")[:-1]) for p in paths})
    ctx = {
        "copies": len(files),
        "size_each": files[0]["size"] if files else 0,
        "size_human": human_readable_size(files[0]["size"]) if files else "0",
        "distinct_locations": distinct_dirs[:20],
        "extension": content_info.get("ext"),
    }

    if content_info.get("read"):
        body = (
            "CONTEXT (paths are for location awareness only, NOT evidence):\n"
            + json.dumps(ctx, ensure_ascii=False, indent=2)
            + "\n\nACTUAL FILE CONTENT (this is what you must judge on):\n"
            + "```\n" + (content_info.get("content") or "")[:120000] + "\n```"
            + ("\n\n[note: content was truncated head+tail]" if content_info.get("truncated") else "")
        )
    else:
        body = (
            "CONTEXT (paths are for location awareness only, NOT evidence):\n"
            + json.dumps(ctx, ensure_ascii=False, indent=2)
            + "\n\nFILE CONTENT: NOT AVAILABLE — "
            + str(content_info.get("reason", "unreadable"))
            + "\nYou were NOT given readable content. Per the rules you MUST NOT "
            "guess from the paths; return needs_human_review unless the metadata "
            "alone is genuinely sufficient (rarely)."
        )

    try:
        client = get_bedrock_client()
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": body}]}],
            system=[{"text": SYSTEM_PROMPT}],
            inferenceConfig={"maxTokens": 800, "temperature": 0.0},
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
        advice = json.loads(text.strip())
    except Exception as e:
        advice = {
            "classification": "needs_human_review",
            "recommended_action": "human_review",
            "confidence": 0.0,
            "risk": "high",
            "evidence": "",
            "rationale": f"advisor error: {e}",
        }

    # Enforce the contract: if the model claimed a delete-safe class but content
    # was unreadable OR it cited no evidence, downgrade to human_review.
    if not content_info.get("read") and advice.get("classification") not in (
        "needs_human_review", "needs_more_content", "project_internal"):
        advice["classification"] = "needs_human_review"
        advice["recommended_action"] = "human_review"
        advice["rationale"] = ("Downgraded: no readable content was available, so a "
                               "definitive classification cannot be trusted. " + advice.get("rationale", ""))
    if advice.get("classification") == "redundant_backup" and not (advice.get("evidence") or "").strip():
        advice["classification"] = "needs_human_review"
        advice["recommended_action"] = "human_review"
        advice["rationale"] = ("Downgraded: no content evidence was cited. " + advice.get("rationale", ""))

    result = {
        "group_hash": group.get("hash"),
        "signature": sig,
        "copies": len(files),
        "content_read": bool(content_info.get("read")),
        "content_kind": content_info.get("kind"),
        "sample_path": rep,
        "distinct_locations": distinct_dirs[:20],
        **advice,
        "advised_at": time.time(),
        "model": model_id,
    }
    _advice_cache[sig] = result
    return result


# ---------------------------------------------------------- batch advisory job

_advisor_jobs: dict = {}


def advise_scan(scan_id: str, model_id: str = DEFAULT_MODEL,
                max_groups: int = 200, min_size: int = 0,
                only_categories: Optional[list[str]] = None) -> str:
    """
    Start a background advisory job over a scan's duplicate groups. To keep it
    bounded + high-value, groups are prioritized by wasted space (biggest first)
    and capped at max_groups. Returns a job_id.
    """
    import threading
    job_id = f"advisor_{int(time.time())}"
    _advisor_jobs[job_id] = {
        "status": "running", "scan_id": scan_id, "model": model_id,
        "total": 0, "done": 0, "results": [], "current": "",
        "started_at": time.time(), "error": None,
    }
    threading.Thread(target=_advise_worker,
                     args=(job_id, scan_id, model_id, max_groups, min_size),
                     daemon=True).start()
    return job_id


def get_advisor_job(job_id: str):
    return _advisor_jobs.get(job_id)


def _advise_worker(job_id, scan_id, model_id, max_groups, min_size):
    job = _advisor_jobs[job_id]
    try:
        dup = get_duplicates(scan_id)
        if dup is None:
            job["status"] = "error"; job["error"] = "scan not found"; return
        groups = [g for g in dup.get("groups", [])
                  if (g.get("files") and g["files"][0]["size"] >= min_size)]
        groups.sort(key=lambda g: g.get("total_wasted_space", 0), reverse=True)
        groups = groups[:max_groups]
        job["total"] = len(groups)
        for g in groups:
            if job.get("cancelled"):
                job["status"] = "cancelled"; return
            job["current"] = (g.get("files") or [{}])[0].get("path", "")
            adv = advise_group(g, model_id)
            adv["wasted_space"] = g.get("total_wasted_space", 0)
            adv["wasted_human"] = human_readable_size(g.get("total_wasted_space", 0))
            job["results"].append(adv)
            job["done"] += 1
        job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


def cancel_advisor(job_id: str) -> bool:
    if job_id in _advisor_jobs:
        _advisor_jobs[job_id]["cancelled"] = True
        return True
    return False
