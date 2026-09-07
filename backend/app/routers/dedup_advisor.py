"""
Dedup / Routing ADVISOR router (Step 3a).

The "principles chat + rules engine" front door:
  GET  /config            -> current RulesConfig
  POST /config            -> patch config (apply a confirmed principle)
  GET  /classify          -> dry-run classification counts over both indexes
  POST /chat              -> state a principle in natural language; Bedrock maps
                             it to a proposed config PATCH + reflects understanding
                             back. Nothing is applied until the user POSTs /config.

Advisory only. No file moves/deletes here — that stays in crossdedup/reconstruct.
All AI runs on AWS Bedrock (per hard rule).
"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services import dedup_rules as rules
from ..services import settings_store
from ..services.bedrock_client import get_bedrock_client

router = APIRouter()


@router.get("/config")
async def get_config():
    """Current rules configuration (principles turned into structured rules)."""
    return rules.get_config()


class ConfigPatch(BaseModel):
    prefer_folder: Optional[str] = None
    transfer_root: Optional[str] = None
    transferred_people: Optional[list[str]] = None
    china_path_signals: Optional[list[str]] = None
    china_use_cjk: Optional[bool] = None
    obsolete_years: Optional[float] = None
    low_value_exts: Optional[list[str]] = None
    principle_notes: Optional[list[str]] = None


@router.post("/config")
async def update_config(patch: ConfigPatch):
    """Apply a (confirmed) change to the rules. Returns the full updated config."""
    data = {k: v for k, v in patch.dict().items() if v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="no fields to update")
    if "prefer_folder" in data and data["prefer_folder"] not in ("organized", "snapshot"):
        raise HTTPException(status_code=400, detail="prefer_folder must be organized|snapshot")
    return rules.update_config(data)


@router.get("/classify")
async def classify(examples: int = 15):
    """DRY RUN — classify every file by dedup verdict, ROT bucket, and route.
    Returns counts + example transferred-redundant rows. No file changes."""
    try:
        return rules.classify(examples=examples)
    except Exception as e:  # pragma: no cover - surfaced to UI
        raise HTTPException(status_code=500, detail=f"classify failed: {e}")


# --------------------------------------------------------------------------- #
# Principles chat (Bedrock)
# --------------------------------------------------------------------------- #
class ChatTurn(BaseModel):
    role: str          # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatTurn] = []


_SYSTEM = """You are the Dedup/Routing Advisor for a file-cleanup tool. The user \
STATES PRINCIPLES about how to deduplicate files and where they should be routed. \
Your job: understand each principle, REFLECT it back in one or two plain sentences, \
and translate it into a config PATCH the deterministic rules engine can apply.

The universe is two folders: 'organized' (E:\\Google Drive Files\\Organized, the \
clean/keep tree) and 'snapshot' (E:\\Dropbox-Snapshot, a throwaway copy). Files are \
routed to one of: cpms (media -> AWS), sharepoint_us, sharepoint_china. Folder \
structure is PRESERVED; routing is a label, not a move. Terminated employees' files \
live under '<Person>'s files' folders and are low-priority keepers.

Config fields you may set in the patch (omit any you aren't changing):
- prefer_folder: "organized" | "snapshot"  (which copy wins when a dup spans both)
- transferred_people: string[]  (extra terminated-employee folder names)
- china_path_signals: string[]  (path substrings that mean SharePoint China)
- china_use_cjk: bool  (treat Chinese characters in a path as China)
- obsolete_years: number  (age past which an old, duplicated, low-value file is 'obsolete')
- low_value_exts: string[]  (extensions considered low-value when old+duplicated)
- principle_notes: string[]  (append a short human-readable summary of THIS principle)

Respond ONLY with valid JSON, no markdown, in this shape:
{
  "understanding": "<one or two sentences reflecting the principle back>",
  "patch": { ...only changed fields... },
  "needs_confirmation": true,
  "clarifying_question": "<a question if the principle is ambiguous, else empty>"
}
If the message is just conversation (not a principle), return an empty patch and put \
your reply in 'understanding'."""


@router.post("/chat")
async def chat(req: ChatRequest):
    """Map a natural-language principle to a proposed config patch + reflection.
    Does NOT apply anything — the UI shows 'understanding' and applies via /config."""
    model_id = settings_store.get_model("dedup_advisor")
    client = get_bedrock_client()

    messages = []
    for turn in req.history[-10:]:
        role = "assistant" if turn.role == "assistant" else "user"
        messages.append({"role": role, "content": [{"text": turn.content}]})
    messages.append({"role": "user", "content": [{"text": req.message}]})

    # include the current config so the model reasons against real state
    cfg_json = json.dumps(rules.get_config(), ensure_ascii=False)
    system = [{"text": _SYSTEM + f"\n\nCURRENT CONFIG:\n{cfg_json}"}]

    try:
        resp = client.converse(
            modelId=model_id,
            messages=messages,
            system=system,
            inferenceConfig={"maxTokens": 1024, "temperature": 0.1},
        )
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
        # Fall back: return the raw model text as the understanding, no patch.
        parsed = {"understanding": text, "patch": {}, "needs_confirmation": False,
                  "clarifying_question": ""}

    # Only keep patch keys the engine actually knows about.
    valid = set(ConfigPatch.__fields__.keys())
    patch = {k: v for k, v in (parsed.get("patch") or {}).items() if k in valid}
    return {
        "understanding": parsed.get("understanding", ""),
        "patch": patch,
        "needs_confirmation": bool(patch) and parsed.get("needs_confirmation", True),
        "clarifying_question": parsed.get("clarifying_question", ""),
        "model_id": model_id,
    }
