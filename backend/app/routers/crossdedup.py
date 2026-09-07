"""
Cross-folder MD5 dedup router. Dedupe identical files across Organized +
Dropbox-Snapshot using the existing MD5 indexes. Preview (dry-run) -> purge
(reversible) -> undo.
"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import crossdedup as cd
from ..services import settings_store
from ..services.bedrock_client import get_bedrock_client

router = APIRouter()


@router.get("/preview")
async def preview(prefer_folder: str = "organized", cross_only: bool = False,
                  snapshot_only: bool = True):
    """DRY RUN — identical-MD5 duplicate groups. snapshot_only=true (default)
    removes ONLY Snapshot files that also exist in Organized; Organized is never
    touched. Shows GB reclaimable."""
    if prefer_folder not in ("organized", "snapshot", "none"):
        raise HTTPException(status_code=400, detail="prefer_folder must be organized|snapshot|none")
    return cd.preview(prefer_folder, cross_only=cross_only, snapshot_only=snapshot_only)


@router.get("/groups")
async def groups(prefer_folder: str = "organized", cross_only: bool = False,
                 snapshot_only: bool = True, offset: int = 0, limit: int = 100,
                 folder_filter: str = ""):
    """Paged review list: each group's keeper + the exact files queued for
    quarantine. snapshot_only=true (default) never queues an Organized file."""
    if prefer_folder not in ("organized", "snapshot", "none"):
        raise HTTPException(status_code=400, detail="prefer_folder must be organized|snapshot|none")
    return cd.groups_page(prefer_folder, cross_only=cross_only,
                          snapshot_only=snapshot_only,
                          offset=offset, limit=limit, folder_filter=folder_filter)


class PurgeRequest(BaseModel):
    prefer_folder: str = "organized"
    cross_only: bool = False
    snapshot_only: bool = True
    confirm: bool = False


@router.post("/purge")
async def purge(request: PurgeRequest):
    """Quarantine redundant duplicate copies. Reversible. snapshot_only=true
    (default) removes only Snapshot files with an Organized twin; Organized is
    never touched."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    try:
        job_id = cd.purge(request.prefer_folder, confirm=True,
                          cross_only=request.cross_only,
                          snapshot_only=request.snapshot_only)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"job_id": job_id, "status": "started"}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = cd.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/cancel/{job_id}")
async def cancel(job_id: str):
    """Stop a running purge cleanly (after the current file). Already-moved files
    stay in the manifest and are undoable."""
    if not cd.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"cancelling": True}


class UndoRequest(BaseModel):
    manifest_file: str
    confirm: bool = False


@router.post("/undo")
async def undo(request: UndoRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    res = cd.undo(request.manifest_file, confirm=True)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


# --------------------------------------------------------- keeper guidance

@router.get("/rule")
async def get_rule():
    """Current keeper rule (folder preference, prefer/avoid paths, tiebreakers)."""
    return cd.get_rule()


class RulePatch(BaseModel):
    prefer_folder: Optional[str] = None
    prefer_paths: Optional[list[str]] = None
    avoid_paths: Optional[list[str]] = None
    tiebreakers: Optional[list[str]] = None


@router.post("/rule")
async def set_rule(patch: RulePatch):
    """Apply a keeper rule (recomputes keepers; invalidates the group cache)."""
    return cd.set_rule({k: v for k, v in patch.dict().items() if v is not None})


_GUIDANCE_SYSTEM = """You translate a user's SPOKEN dedup guidance into a structured \
keeper rule for choosing which copy of a duplicate file to KEEP (the rest are \
quarantined). The two folders are 'organized' (the master) and 'snapshot' (a \
throwaway Dropbox copy). Files may sit in many subfolders.

You output a rule with these fields (include only what the user's guidance implies):
- prefer_folder: "organized" | "snapshot" | "none"
- prefer_paths: string[]  (ordered path substrings; EARLIER = higher priority. The
  copy whose path contains an earlier substring wins. e.g. ["Social Media Marketing",
  "Final Output"])
- avoid_paths: string[]  (path substrings that should LOSE when possible, e.g.
  "'s files" for terminated-employee transfer folders, "Stuff to sort", "Backup")
- tiebreakers: string[] ordered, from: cleanest_name, shortest_path, longest_path,
  longest_name, shortest_name, newest, oldest

Respond ONLY with valid JSON:
{
  "understanding": "<one or two plain sentences reflecting the guidance back>",
  "rule_patch": { ...only changed fields... },
  "clarifying_question": "<a question if ambiguous, else empty>"
}
If the message isn't guidance, return an empty rule_patch and put your reply in
'understanding'."""


class GuidanceChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@router.post("/guidance/chat")
async def guidance_chat(req: GuidanceChatRequest):
    """Turn spoken keeper guidance into a rule patch + reflect it back. Does NOT
    apply — the UI shows 'understanding' then POSTs /rule to apply."""
    model_id = settings_store.get_model("dedup_advisor")
    client = get_bedrock_client()

    messages = []
    for turn in req.history[-10:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": [{"text": turn.get("content", "")}]})
    messages.append({"role": "user", "content": [{"text": req.message}]})

    cur = json.dumps(cd.get_rule(), ensure_ascii=False)
    system = [{"text": _GUIDANCE_SYSTEM + f"\n\nCURRENT RULE:\n{cur}"}]

    try:
        resp = client.converse(
            modelId=model_id, messages=messages, system=system,
            inferenceConfig={"maxTokens": 1024, "temperature": 0.1})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bedrock error: {e}")

    text = ""
    for block in resp["output"]["message"]["content"]:
        if "text" in block:
            text += block["text"]
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"understanding": text, "rule_patch": {}, "clarifying_question": ""}

    valid = {"prefer_folder", "prefer_paths", "avoid_paths", "tiebreakers"}
    rule_patch = {k: v for k, v in (parsed.get("rule_patch") or {}).items() if k in valid}
    return {
        "understanding": parsed.get("understanding", ""),
        "rule_patch": rule_patch,
        "clarifying_question": parsed.get("clarifying_question", ""),
        "model_id": model_id,
    }


class ResolveAmbiguousRequest(BaseModel):
    guidance: str = ""
    prefer_folder: str = "organized"
    cross_only: bool = False
    snapshot_only: bool = True
    max_groups: int = 200


@router.post("/resolve-ambiguous")
async def resolve_ambiguous(req: ResolveAmbiguousRequest):
    """AI picks keepers for groups the rule couldn't decide (true ties), guided by
    the user's stated intent. Records overrides; recomputes on next load."""
    try:
        return cd.resolve_ambiguous(
            guidance=req.guidance, prefer_folder=req.prefer_folder,
            cross_only=req.cross_only, snapshot_only=req.snapshot_only,
            max_groups=req.max_groups)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"resolve failed: {e}")


@router.post("/clear-ai-overrides")
async def clear_ai_overrides():
    """Discard all AI keeper overrides (revert to the deterministic rule)."""
    cd.clear_ai_overrides()
    return {"cleared": True}
