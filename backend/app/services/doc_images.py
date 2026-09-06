"""
Extract embedded images from documents so they can be OCR'd / described by a
vision model. Handles the "Word doc that's really just pasted screenshots" and
"PDF of scanned pages" cases where there's little/no extractable TEXT but the
real content lives in images.

Uses only libraries already installed: python-docx (docx zip media), pypdf
(pdf embedded images), Pillow (normalize to JPEG/PNG for Bedrock). No new deps.
"""
import io
import os
import zipfile
from typing import Optional

# Bedrock vision accepts jpeg/png/gif/webp. We normalize everything to JPEG.
_MAX_DIM = 2000          # downscale huge images (Bedrock size limits + cost)
_MIN_DIM = 40            # skip tiny icons/bullets that carry no naming signal


def _normalize(raw: bytes) -> Optional[dict]:
    """Return {'format','bytes'} as JPEG/PNG, or None if too small / unreadable."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
        w, h = im.size
        if w < _MIN_DIM or h < _MIN_DIM:
            return None
        if max(w, h) > _MAX_DIM:
            scale = _MAX_DIM / max(w, h)
            im = im.resize((int(w * scale), int(h * scale)))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        return {"format": "jpeg", "bytes": out.getvalue()}
    except Exception:
        return None


def _docx_images(path: str, limit: int) -> list[dict]:
    """Pull embedded images out of a .docx (they live in word/media/* in the zip)."""
    imgs = []
    try:
        with zipfile.ZipFile(path) as z:
            media = [n for n in z.namelist()
                     if n.startswith("word/media/")
                     and n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"))]
            # bigger files first — more likely to be a real screenshot vs an icon
            media.sort(key=lambda n: z.getinfo(n).file_size, reverse=True)
            for n in media:
                if len(imgs) >= limit:
                    break
                norm = _normalize(z.read(n))
                if norm:
                    imgs.append(norm)
    except (zipfile.BadZipFile, OSError):
        pass
    return imgs


def _pptx_images(path: str, limit: int) -> list[dict]:
    """Embedded images in a .pptx live in ppt/media/*."""
    imgs = []
    try:
        with zipfile.ZipFile(path) as z:
            media = [n for n in z.namelist()
                     if n.startswith("ppt/media/")
                     and n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"))]
            media.sort(key=lambda n: z.getinfo(n).file_size, reverse=True)
            for n in media:
                if len(imgs) >= limit:
                    break
                norm = _normalize(z.read(n))
                if norm:
                    imgs.append(norm)
    except (zipfile.BadZipFile, OSError):
        pass
    return imgs


def _pdf_images(path: str, limit: int) -> list[dict]:
    """Extract embedded images from a PDF via pypdf (catches pasted screenshots
    and single-image scanned pages)."""
    imgs = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        for page in reader.pages[:30]:
            if len(imgs) >= limit:
                break
            try:
                for img in page.images:
                    if len(imgs) >= limit:
                        break
                    norm = _normalize(img.data)
                    if norm:
                        imgs.append(norm)
            except Exception:
                continue
    except Exception:
        pass
    return imgs


def extract_embedded_images(path: str, limit: int = 4) -> list[dict]:
    """Return up to `limit` embedded images (normalized JPEG bytes) from a
    docx/pptx/pdf, largest first. Empty list if none or unsupported type."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return _docx_images(path, limit)
    if ext == ".pptx":
        return _pptx_images(path, limit)
    if ext == ".pdf":
        return _pdf_images(path, limit)
    return []


def ocr_describe_images(images: list[dict], model_id: str,
                        context: str = "") -> dict:
    """Send extracted images to a vision Bedrock model to OCR any visible text
    AND describe the content, so an image-heavy doc can be named from what's
    actually in the pictures. Returns {read, text, model} — stays on Bedrock."""
    if not images:
        return {"read": False, "text": "", "reason": "no embedded images"}
    try:
        from .bedrock_client import get_bedrock_client
        client = get_bedrock_client()
        content = []
        for im in images[:4]:
            content.append({"image": {"format": im["format"],
                                      "source": {"bytes": im["bytes"]}}})
        content.append({"text":
            "These images were embedded in a document" +
            (f" ({context})" if context else "") +
            ". Transcribe any visible TEXT (OCR) and briefly describe what the "
            "images show, so the document can be given a descriptive name. "
            "Report only what you actually see; do not speculate."})
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 700, "temperature": 0.0},
        )
        text = ""
        for b in resp["output"]["message"]["content"]:
            if "text" in b:
                text += b["text"]
        text = text.strip()
        return {"read": bool(text), "text": text, "model": model_id,
                "image_count": len(images)}
    except Exception as e:
        return {"read": False, "text": "", "reason": f"vision OCR failed: {e}"}
