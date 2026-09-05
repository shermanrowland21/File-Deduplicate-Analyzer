"""
Content reader (Phase B foundation) — reads the ACTUAL bytes/content of a file so
the LLM advisor classifies from real content, never from filename/path guesses.

Guiding principle (per user): READ THE FILE. Tokens don't matter; a correct
decision does. So:
  - text / code / markup / config: read the full content (large head+tail slice
    for very big text files).
  - Office (.docx/.xlsx/.pptx): extract the real text.
  - PDF: extract text.
  - HTML: keep raw markup (framework markers live there).
  - media / binary / unknown: cannot be read as text -> return a clear
    "unreadable_as_text" marker with metadata so the LLM must escalate, not guess.

Every result states exactly whether real content was obtained (`read: true`) or
not (`read: false`, with a reason). The LLM contract keys off this.
"""
import os
from typing import Optional

# Full content for these; big ones get head+tail.
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".html", ".htm", ".cshtml", ".razor", ".aspx", ".ascx", ".jsp", ".php",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".scss", ".less",
    ".py", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".vb", ".sql", ".sh", ".ps1", ".bat", ".gitignore", ".dockerignore",
    ".svg", ".gradle", ".properties", ".lock",
}
_DOCX_EXTS = {".docx"}
_XLSX_EXTS = {".xlsx", ".xlsm"}
_PPTX_EXTS = {".pptx"}
_PDF_EXTS = {".pdf"}

# Big text files: read this much from the front and this much from the back.
_TEXT_HEAD = 200_000   # ~200 KB
_TEXT_TAIL = 50_000


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _read_text(path: str, size: int) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size <= _TEXT_HEAD + _TEXT_TAIL:
                content = f.read()
                truncated = False
            else:
                head = f.read(_TEXT_HEAD)
                f.seek(max(0, size - _TEXT_TAIL))
                tail = f.read(_TEXT_TAIL)
                content = head + "\n\n...[middle omitted]...\n\n" + tail
                truncated = True
        return {"read": True, "kind": "text", "content": content, "truncated": truncated}
    except Exception as e:
        return {"read": False, "kind": "text", "reason": f"text read failed: {e}"}


def _read_docx(path: str) -> dict:
    try:
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for tbl in d.tables:
            for row in tbl.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)
        return {"read": True, "kind": "docx", "content": text[:400_000],
                "truncated": len(text) > 400_000}
    except Exception as e:
        return {"read": False, "kind": "docx", "reason": f"docx extract failed: {e}"}


def _read_xlsx(path: str) -> dict:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets[:10]:
            lines.append(f"# Sheet: {ws.title}")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 200:
                    lines.append("...[more rows omitted]...")
                    break
                vals = [str(c) for c in row if c is not None]
                if vals:
                    lines.append(" | ".join(vals))
        wb.close()
        text = "\n".join(lines)
        return {"read": True, "kind": "xlsx", "content": text[:400_000],
                "truncated": len(text) > 400_000}
    except Exception as e:
        return {"read": False, "kind": "xlsx", "reason": f"xlsx extract failed: {e}"}


def _read_pptx(path: str) -> dict:
    try:
        from pptx import Presentation
        prs = Presentation(path)
        lines = []
        for i, slide in enumerate(prs.slides):
            lines.append(f"# Slide {i + 1}")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for p in shape.text_frame.paragraphs:
                        t = "".join(r.text for r in p.runs)
                        if t.strip():
                            lines.append(t)
        text = "\n".join(lines)
        return {"read": True, "kind": "pptx", "content": text[:400_000],
                "truncated": len(text) > 400_000}
    except Exception as e:
        return {"read": False, "kind": "pptx", "reason": f"pptx extract failed: {e}"}


def _read_pdf(path: str) -> dict:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        lines = []
        for i, page in enumerate(reader.pages[:50]):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                lines.append(t)
        text = "\n".join(lines)
        if not text.strip():
            return {"read": False, "kind": "pdf",
                    "reason": "PDF has no extractable text (likely scanned/image-only)"}
        return {"read": True, "kind": "pdf", "content": text[:400_000],
                "truncated": len(text) > 400_000, "pages": len(reader.pages)}
    except Exception as e:
        return {"read": False, "kind": "pdf", "reason": f"pdf extract failed: {e}"}


def read_content(path: str) -> dict:
    """
    Read the ACTUAL content of a file for LLM classification.
    Returns a dict always containing:
      path, ext, size, read (bool)
      if read: content (str), kind, truncated
      if not read: reason (why it couldn't be read as text)
    Never guesses; media/binary explicitly return read=False so the caller/LLM
    must escalate rather than infer from the name.
    """
    ext = _ext(path)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return {"path": path, "ext": ext, "size": 0, "read": False,
                "reason": f"stat failed: {e}"}

    base = {"path": path, "ext": ext, "size": size}

    if ext in _TEXT_EXTS:
        return {**base, **_read_text(path, size)}
    if ext in _DOCX_EXTS:
        return {**base, **_read_docx(path)}
    if ext in _XLSX_EXTS:
        return {**base, **_read_xlsx(path)}
    if ext in _PPTX_EXTS:
        return {**base, **_read_pptx(path)}
    if ext in _PDF_EXTS:
        return {**base, **_read_pdf(path)}

    # Media / binary / unknown: cannot be read as text. Be explicit.
    return {**base, "read": False, "kind": "binary",
            "reason": f"'{ext or 'no-ext'}' is not text-readable "
                      "(media/binary) — classify by metadata or escalate; do NOT guess"}
