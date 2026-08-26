"""
File Deduplicate Analyzer - Main FastAPI Application
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import scanner, duplicates, analysis, renaming, models, browser, media

app = FastAPI(
    title="File Deduplicate Analyzer",
    description="File deduplication and AI-powered file analysis/renaming via AWS Bedrock",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scanner.router, prefix="/api/scanner", tags=["Scanner"])
app.include_router(duplicates.router, prefix="/api/duplicates", tags=["Duplicates"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["Analysis"])
app.include_router(renaming.router, prefix="/api/renaming", tags=["Renaming"])
app.include_router(models.router, prefix="/api/models", tags=["Models"])
app.include_router(browser.router, prefix="/api/browser", tags=["Browser"])
app.include_router(media.router, prefix="/api/media", tags=["Media Analysis"])


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}
