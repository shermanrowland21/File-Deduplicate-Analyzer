"""
File Deduplicate Analyzer - Main FastAPI Application
Serves both the API and the built React frontend from the same port.
"""
import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .routers import scanner, duplicates, analysis, renaming, models, browser, media, visual_search, archives

app = FastAPI(
    title="File Deduplicate Analyzer",
    description="File deduplication and AI-powered file analysis/renaming via AWS Bedrock",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers
app.include_router(scanner.router, prefix="/api/scanner", tags=["Scanner"])
app.include_router(duplicates.router, prefix="/api/duplicates", tags=["Duplicates"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["Analysis"])
app.include_router(renaming.router, prefix="/api/renaming", tags=["Renaming"])
app.include_router(models.router, prefix="/api/models", tags=["Models"])
app.include_router(browser.router, prefix="/api/browser", tags=["Browser"])
app.include_router(media.router, prefix="/api/media", tags=["Media Analysis"])
app.include_router(visual_search.router, prefix="/api/visual", tags=["Visual Search"])
app.include_router(archives.router, prefix="/api/archives", tags=["Archives"])


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


# Serve the built React frontend
FRONTEND_DIR = Path(r"C:\Users\sherm\Documents\GitHub\File-Deduplicate-Analyzer\frontend\dist")

if FRONTEND_DIR.exists():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="static")

    # Catch-all: serve index.html for any non-API route (SPA routing)
    @app.get("/{full_path:path}")
    async def serve_frontend(request: Request, full_path: str):
        # Don't intercept API routes
        if full_path.startswith("api/"):
            return None
        # Serve actual files if they exist in dist
        file_path = FRONTEND_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        # Otherwise serve index.html (SPA fallback)
        return FileResponse(str(FRONTEND_DIR / "index.html"))
