"""
Bulk FILE re-naming advisor.

Point it at a directory of ill-named files (IMG_1234.jpg, DSC0001.jpg,
Untitled.docx, screenshot_2024.png, hash-named blobs) and it proposes a
descriptive filename for each by READING THE ACTUAL CONTENT:
  - images  -> a vision-capable AWS Bedrock model looks at the real pixels
  - text / code / docx / xlsx / pptx / pdf -> content_reader extracts real text,
    then a cheap Bedrock text model proposes a name from it
  - unreadable (raw binary / media we can't inspect) -> flagged needs_review,
    never guessed from the old name.

Every proposal has a CONFIDENCE score + cited EVIDENCE. Renames are reviewed,
collision-safe, and reversible (manifest). Everything stays on AWS Bedrock.
"""
import os
import re
import json
import time
import threading
from typing import Optional

from .bedrock_client import analyze_file, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .content_reader import read_content
from .doc_images import extract_embedded_images, ocr_describe_images

# Below this many chars of extracted text, treat a doc as "image-heavy" and try
# OCR-ing its embedded images instead of relying on typed text.
_SPARSE_TEXT_CHARS = 40
_IMAGE_DOC_EXTS = {".docx", ".pptx", ".pdf"}

# Models resolved at call time from the central Settings store (UI-settable),
# independently per function — text/docs vs images can use different models.
from . import settings_store

def _default_text_model() -> str:
    return settings_store.get_model("file_namer_text")

def _default_vision_model() -> str:
    return settings_store.get_model("file_namer_vision")

_proposal_cache: dict = {}
_jobs: dict = {}

_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}

# Risk-weighted auto-approve confidence thresholds by action (research-backed:
# confidence should gate decisions in proportion to the action's risk — a
# reversible rename tolerates a lower bar than a move, and destructive actions
# demand the highest bar + explicit human confirmation). See
# docs/content-naming-research.md (Learning 2).
ACTION_RISK_THRESHOLDS = {
    "rename": 0.80,   # reversible (manifest/undo) → lower bar acceptable
    "move":   0.90,   # relocates across folders/sources
    "delete": 0.95,   # destructive → highest bar, always confirm
}


def risk_threshold(action: str = "rename") -> float:
    return ACTION_RISK_THRESHOLDS.get(action, 0.90)


def _get_media_analysis(path: str) -> Optional[dict]:
    """Fetch stored Media Intelligence analysis (transcript/topics/summary) for a
    video/audio file, if it's already been analyzed. Connects the file-namer to
    the media pipeline so videos are named from their real content."""
    try:
        from .media import metadata_store
        return metadata_store.get_file_analysis(path)
    except Exception:
        return None


def _media_evidence(media: dict) -> str:
    """Condense stored media analysis into naming evidence (topics + summary +
    a transcript excerpt)."""
    parts = []
    topics = [t.get("topic") for t in (media.get("topics") or []) if t.get("topic")]
    if topics:
        parts.append("Topics: " + ", ".join(dict.fromkeys(topics))[:400])
    kws = [k.get("keyword") for k in (media.get("keywords") or []) if k.get("keyword")]
    if kws:
        parts.append("Keywords: " + ", ".join(dict.fromkeys(kws))[:300])
    # summary lives in the media file's metadata; also pull a transcript excerpt
    tr = media.get("transcript") or []
    if tr:
        text = " ".join(s.get("text", "") for s in tr[:20])
        if text.strip():
            parts.append("Transcript excerpt: " + text[:1200])
    return "\n".join(parts) if parts else ""

# Names that signal an auto-generated / meaningless filename worth fixing.
_ILL_NAMED_RE = re.compile(
    r"^(img[_\-]?\d+|dsc[_\-]?\d+|dscf?\d+|p\d{6,}|pxl_\d+|"
    r"screen ?shot.*|screenshot.*|photo[_\- ]?\d*|image[_\- ]?\d*|"
    r"untitled.*|document\d*|new ?document.*|scan[_\-]?\d+|capture.*|"
    r"[0-9a-f]{16,}|\d{6,}|copy of.*|final\d*|new\d*|temp\d*|download.*)$",
    re.IGNORECASE)

_INVALID = re.compile(r'[\\/:*?"<>|]')


def is_ill_named(filename: str) -> bool:
    stem = os.path.splitext(filename)[0].strip()
    if not stem:
        return True
    return bool(_ILL_NAMED_RE.match(stem))


def _sanitize(name: str, max_len: int = 120) -> str:
    name = _INVALID.sub("", name or "").strip().strip(". ")
    return name[:max_len]


SYSTEM_PROMPT = """You propose a concise, descriptive FILENAME (no extension) for a \
file, based ONLY on the real content evidence provided. Rules:
1. Base it on the actual content given (image description or extracted text), NEVER \
on the current filename.
2. Cite the specific evidence you used.
3. If the evidence is missing or too weak to name it meaningfully, return low \
confidence and needs_review=true — do NOT invent.
4. Keep it short (3-8 words), human-readable, no extension, no invalid path chars.

Respond ONLY with JSON:
{"proposed": "descriptive name without extension", "confidence": 0.0-1.0,
 "evidence": "the content detail you used", "needs_review": true|false}"""


def propose_for_file(path: str, text_model: Optional[str] = None,
                     vision_model: Optional[str] = None,
                     use_cache: bool = True,
                     conventions: Optional[dict] = None) -> dict:
    """Read a file's real content and propose a better name. The AI ALWAYS reads
    the content (vision+OCR for images, text+OCR for docs). If `conventions` is
    given (a per-file-type map of naming-convention configs, optionally with a
    'default'), the AI's understanding is then formatted INTO that naming
    convention — so AI + your naming format work together, not either/or."""
    text_model = text_model or _default_text_model()
    vision_model = vision_model or _default_vision_model()
    cache_key = path + "|" + (json.dumps(conventions, sort_keys=True) if conventions else "")
    if use_cache and cache_key in _proposal_cache:
        return _proposal_cache[cache_key]

    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    stem = os.path.splitext(filename)[0]
    result = {
        "path": path.replace("\\", "/"),
        "current_name": filename,
        "extension": ext,
        "ill_named": is_ill_named(filename),
    }
    _meta: dict = {}   # AI metadata captured for optional template formatting

    try:
        if ext in IMAGE_EXTENSIONS:
            # Vision model reads the ACTUAL image — including OCR of any text in it
            # (screenshots, diagrams with labels, slide photos).
            meta = analyze_file(path, model_id=vision_model,
                                custom_prompt="Transcribe any visible TEXT in this "
                                "image (OCR) and note what it shows, then propose a "
                                "short descriptive filename (3-8 words) based only on "
                                "what you actually see/read.")
            proposed = _sanitize(meta.get("suggested_name") or meta.get("description", ""))
            desc = meta.get("description") or meta.get("content_summary") or ""
            conf = 0.8 if proposed and desc else 0.3
            _meta = {
                "suggested_name": proposed,
                "category": meta.get("category", ""),
                "description": (meta.get("description") or "")[:60],
                "tags": meta.get("tags", []) or [],
            }
            result.update({
                "kind": "image", "content_read": bool(desc),
                "proposed_name": (proposed + ext) if proposed else filename,
                "proposed_stem": proposed,
                "confidence": conf,
                "evidence": desc[:240],
                "needs_review": conf < 0.7 or not proposed,
                "rationale": "Proposed from AI vision analysis of the image.",
                "model": vision_model,
            })
        elif ext in VIDEO_EXTENSIONS or ext in _AUDIO_EXTS:
            # Videos/audio are named from the Media Intelligence pipeline's real
            # analysis (transcript + extracted topics + visual summary), not from
            # a single frame. Use stored analysis if present; otherwise flag that
            # the media pipeline must run first (never guess from the old name).
            media = _get_media_analysis(path)
            if media and (media.get("topics") or media.get("summary")
                          or media.get("transcript")):
                evidence = _media_evidence(media)
                adv = _ask_text_model(
                    f"This is a {'video' if ext in VIDEO_EXTENSIONS else 'audio'} "
                    f"file. Name it from its transcript/topics.\n\n{evidence}",
                    text_model)
                proposed = _sanitize(adv.get("proposed", ""))
                _meta = {"suggested_name": proposed, "category": "",
                         "description": proposed, "tags": []}
                result.update({
                    "kind": "video" if ext in VIDEO_EXTENSIONS else "audio",
                    "content_read": True,
                    "proposed_name": (proposed + ext) if proposed else filename,
                    "proposed_stem": proposed,
                    "confidence": float(adv.get("confidence", 0)),
                    "evidence": (adv.get("evidence") or evidence)[:240],
                    "needs_review": bool(adv.get("needs_review")) or not proposed,
                    "rationale": "Proposed from Media Intelligence transcript/topics.",
                    "model": text_model,
                })
            else:
                result.update({
                    "kind": "video" if ext in VIDEO_EXTENSIONS else "audio",
                    "content_read": False,
                    "proposed_name": filename, "proposed_stem": stem,
                    "confidence": 0.0, "evidence": "",
                    "needs_review": True,
                    "needs_media_analysis": True,
                    "rationale": "Not yet analyzed. Run Media Intelligence on this "
                                 "file/folder first, then re-run to name it from its "
                                 "transcript & topics.",
                    "model": None,
                })
        else:
            # Text/doc/pdf/pptx: read real content, ask cheap text model.
            ci = read_content(path)
            extracted = (ci.get("content") or "").strip() if ci.get("read") else ""

            # If a doc has little/no extractable TEXT but has embedded images
            # (a Word/PDF/PPTX that's really pasted screenshots or scanned pages),
            # OCR + describe those images with the vision model and name from that.
            ocr_text = ""
            if len(extracted) < _SPARSE_TEXT_CHARS and ext in _IMAGE_DOC_EXTS:
                imgs = extract_embedded_images(path, limit=4)
                if imgs:
                    ocr = ocr_describe_images(imgs, vision_model, context=f"{ext} document")
                    if ocr.get("read"):
                        ocr_text = ocr["text"]

            combined = "\n".join(t for t in (extracted, ocr_text) if t).strip()
            if combined:
                src_note = ("extracted text" if extracted and not ocr_text else
                            "OCR of embedded images" if ocr_text and not extracted else
                            "extracted text + OCR of embedded images")
                body = (f"File extension: {ext}\nContent ({src_note}):\n```\n"
                        + combined[:15000] + "\n```")
                adv = _ask_text_model(body, text_model)
                proposed = _sanitize(adv.get("proposed", ""))
                _meta = {"suggested_name": proposed, "category": "",
                         "description": proposed, "tags": []}
                result.update({
                    "kind": ci.get("kind", "text"),
                    "content_read": True,
                    "ocr_used": bool(ocr_text),
                    "proposed_name": (proposed + ext) if proposed else filename,
                    "proposed_stem": proposed,
                    "confidence": float(adv.get("confidence", 0)),
                    "evidence": (adv.get("evidence") or combined[:240])[:240],
                    "needs_review": bool(adv.get("needs_review")) or not proposed,
                    "rationale": f"Proposed from {src_note}.",
                    "model": text_model,
                })
            else:
                result.update({
                    "kind": ci.get("kind", "binary"), "content_read": False,
                    "proposed_name": filename, "proposed_stem": stem,
                    "confidence": 0.0,
                    "evidence": ci.get("reason", "unreadable"),
                    "needs_review": True,
                    "rationale": "No readable text or images; needs human review (not guessed).",
                    "model": None,
                })
    except Exception as e:
        result.update({
            "kind": "error", "content_read": False,
            "proposed_name": filename, "proposed_stem": stem,
            "confidence": 0.0, "evidence": "", "needs_review": True,
            "rationale": f"error: {e}", "model": None,
        })

    # If a naming convention is supplied, POUR the AI's understanding into it
    # (AI + convention together). The AI already read the content above; here we
    # format that understanding using the file-type's template. Only when we have
    # a usable AI name — never fabricate fields we don't have.
    if conventions and result.get("content_read") and result.get("proposed_stem"):
        formatted = _apply_convention(path, ext, _meta, conventions)
        if formatted:
            result["proposed_name"] = formatted
            result["proposed_stem"] = os.path.splitext(formatted)[0]
            result["formatted_by_convention"] = True

    result["is_change"] = bool(result.get("proposed_stem")) and \
        result["proposed_name"].strip().lower() != filename.strip().lower()
    _proposal_cache[cache_key] = result
    return result


def _apply_convention(path: str, ext: str, meta: dict, conventions: dict) -> Optional[str]:
    """Format the AI-derived metadata into the naming convention for this file's
    type group (falling back to 'default'). Returns the new filename or None."""
    try:
        from .renaming_service import classify_file_type, apply_naming_convention
        group = classify_file_type(path)
        conv = conventions.get(group) or conventions.get("default")
        if not conv:
            return None
        # Shape metadata into what apply_naming_convention expects.
        md = {
            "suggested_name": meta.get("suggested_name", ""),
            "category": meta.get("category", ""),
            "description": meta.get("description", ""),
            "tags": meta.get("tags", []) or [],
        }
        return apply_naming_convention(
            file_path=path,
            template=conv.get("template", "{suggested_name}.{ext}"),
            metadata=md,
            date_format=conv.get("date_format", "%Y-%m-%d"),
            separator=conv.get("separator", "_"),
            case=conv.get("case", "lower"),
            max_length=conv.get("max_length", 255),
            replace_spaces_with=conv.get("replace_spaces_with", "_"),
        )
    except Exception:
        return None


def _ask_text_model(body: str, model_id: str) -> dict:
    from .bedrock_client import get_bedrock_client
    try:
        client = get_bedrock_client()
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": body}]}],
            system=[{"text": SYSTEM_PROMPT}],
            inferenceConfig={"maxTokens": 300, "temperature": 0.0},
        )
        text = ""
        for b in resp["output"]["message"]["content"]:
            if "text" in b:
                text += b["text"]
        text = text.strip()
        for fence in ("```json", "```"):
            if text.startswith(fence):
                text = text[len(fence):]
        if text.endswith("```"):
            text = text[:-3]
        data = json.loads(text.strip())
        if not (data.get("evidence") or "").strip():
            data["needs_review"] = True
            data["confidence"] = min(float(data.get("confidence", 0)), 0.3)
        return data
    except Exception as e:
        return {"proposed": "", "confidence": 0.0, "evidence": "",
                "needs_review": True, "error": str(e)}


# ----------------------------------------------------------------- batch job

def analyze_dir(directory: str, recursive: bool = True, only_illnamed: bool = True,
                max_files: int = 300, text_model: Optional[str] = None,
                vision_model: Optional[str] = None,
                conventions: Optional[dict] = None) -> str:
    text_model = text_model or _default_text_model()
    vision_model = vision_model or _default_vision_model()
    job_id = f"filenamer_{int(time.time())}"
    _jobs[job_id] = {"status": "running", "directory": directory, "total": 0,
                     "done": 0, "current": "", "results": [],
                     "started_at": time.time(), "error": None, "cancelled": False}
    threading.Thread(target=_worker,
                     args=(job_id, directory, recursive, only_illnamed,
                           max_files, text_model, vision_model, conventions),
                     daemon=True).start()
    return job_id


def get_job(job_id):
    return _jobs.get(job_id)


def cancel_job(job_id):
    if job_id in _jobs:
        _jobs[job_id]["cancelled"] = True
        return True
    return False


def _iter_files(directory, recursive, include_hidden=False):
    if recursive:
        for root, dirs, files in os.walk(directory):
            if not include_hidden:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in files:
                if not include_hidden and n.startswith("."):
                    continue
                yield os.path.join(root, n)
    else:
        for n in sorted(os.listdir(directory)):
            fp = os.path.join(directory, n)
            if os.path.isfile(fp) and (include_hidden or not n.startswith(".")):
                yield fp


def _worker(job_id, directory, recursive, only_illnamed, max_files,
            text_model, vision_model, conventions=None):
    job = _jobs[job_id]
    try:
        if not os.path.isdir(directory):
            job["status"] = "error"; job["error"] = "directory not found"; return
        candidates = []
        for fp in _iter_files(directory, recursive):
            if only_illnamed and not is_ill_named(os.path.basename(fp)):
                continue
            candidates.append(fp)
            if len(candidates) >= max_files:
                break
        job["total"] = len(candidates)
        for fp in candidates:
            if job.get("cancelled"):
                job["status"] = "cancelled"; return
            job["current"] = os.path.basename(fp)
            try:
                job["results"].append(propose_for_file(
                    fp, text_model, vision_model, conventions=conventions))
            except Exception as e:
                job["results"].append({
                    "path": fp.replace("\\", "/"),
                    "current_name": os.path.basename(fp),
                    "proposed_name": os.path.basename(fp),
                    "confidence": 0.0, "needs_review": True, "is_change": False,
                    "evidence": "", "rationale": f"error: {e}", "kind": "error"})
            job["done"] += 1
        # rank: changes first, then lowest confidence first (review those)
        job["results"].sort(key=lambda r: (not r.get("is_change", False),
                                           r.get("confidence", 0)))
        job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


# --------------------------------------------------------------- apply (safe)

APPLY_STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "file_renames")


def apply_renames(items: list[dict], confirm: bool = False) -> dict:
    """items: [{path, proposed_name}]. Collision-safe + reversible. Never deletes."""
    if not confirm:
        return {"applied": False, "reason": "confirm=false"}
    import shutil
    os.makedirs(APPLY_STORE, exist_ok=True)
    manifest = os.path.join(APPLY_STORE, f"rename_{int(time.time())}.json")
    res = {"applied": True, "renamed": 0, "skipped": 0, "errors": [],
           "manifest": manifest, "moves": []}
    for it in items:
        src = it.get("path")
        new_name = _sanitize(it.get("proposed_name", ""), max_len=180)
        if not src or not new_name or not os.path.isfile(src):
            res["skipped"] += 1; continue
        parent = os.path.dirname(src)
        if new_name == os.path.basename(src):
            res["skipped"] += 1; continue
        dest = os.path.join(parent, new_name)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(new_name)
            i = 2
            while os.path.exists(os.path.join(parent, f"{stem} ({i}){ext}")):
                i += 1
            dest = os.path.join(parent, f"{stem} ({i}){ext}")
        try:
            shutil.move(src, dest)
            res["moves"].append({"from": src.replace("\\", "/"),
                                 "to": dest.replace("\\", "/")})
            res["renamed"] += 1
            with open(manifest, "w", encoding="utf-8") as f:
                json.dump({"created_at": time.time(), "moves": res["moves"]}, f, indent=2)
        except OSError as e:
            res["errors"].append({"file": src, "error": str(e)})
    return res


def undo_renames(manifest_path: str, confirm: bool = False) -> dict:
    if not confirm:
        return {"undone": False, "reason": "confirm=false"}
    if not os.path.exists(manifest_path):
        return {"undone": False, "reason": "manifest not found"}
    import shutil
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    res = {"undone": True, "restored": 0, "errors": []}
    for mv in reversed(data.get("moves", [])):
        cur, orig = mv["to"], mv["from"]
        try:
            if os.path.isfile(cur) and not os.path.exists(orig):
                shutil.move(cur, orig)
                res["restored"] += 1
        except OSError as e:
            res["errors"].append({"file": cur, "error": str(e)})
    return res
