"""
Archive extraction service.
Handles Google Takeout multi-part zips, standard zips, tar.gz, 7z.

Google Takeout specifics:
- Often comes as multiple numbered zip files (takeout-xxx-001.zip, 002.zip, etc.)
- Each zip is independent (not split archive), just separate chunks of your data
- JSON metadata sidecar files alongside media files
- Nested folder structure: Takeout/Google Photos/2023/...
"""
import os
import zipfile
import tarfile
import threading
import time
import shutil
from pathlib import Path
from typing import Optional

# Track extraction jobs
_extract_jobs: dict = {}


def get_extraction_job(job_id: str) -> Optional[dict]:
    """Get status of an extraction job."""
    return _extract_jobs.get(job_id)


def find_archives(directory: str) -> list[dict]:
    """
    Find all archive files in a directory.
    Returns info about each archive found.
    """
    archives = []
    archive_extensions = {".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2", ".7z", ".rar"}

    try:
        for item in sorted(Path(directory).iterdir()):
            if item.is_file():
                # Check extension (handle .tar.gz)
                name_lower = item.name.lower()
                is_archive = False
                for ext in archive_extensions:
                    if name_lower.endswith(ext):
                        is_archive = True
                        break

                if is_archive:
                    archives.append({
                        "path": str(item).replace("\\", "/"),
                        "filename": item.name,
                        "size": item.stat().st_size,
                        "size_human": _human_size(item.stat().st_size),
                        "extension": item.suffix.lower(),
                    })
    except (OSError, PermissionError) as e:
        pass

    return archives


def extract_archives(
    source_dir: str,
    output_dir: Optional[str] = None,
    archives: Optional[list[str]] = None,
) -> str:
    """
    Extract archives from source_dir (or specific archive paths) into output_dir.
    Runs in background thread. Returns job_id for polling.

    If output_dir is None, extracts alongside the archives in a subfolder.
    """
    job_id = f"extract_{int(time.time())}_{Path(source_dir).name[:20]}"

    if output_dir is None:
        output_dir = os.path.join(source_dir, "extracted")

    _extract_jobs[job_id] = {
        "status": "running",
        "source_dir": source_dir,
        "output_dir": output_dir.replace("\\", "/"),
        "phase": "discovering",
        "total_archives": 0,
        "processed_archives": 0,
        "current_archive": "",
        "files_extracted": 0,
        "errors": [],
        "progress": 0,
        "elapsed_seconds": 0,
        "started_at": time.time(),
        "cancelled": False,
    }

    thread = threading.Thread(
        target=_extract_worker,
        args=(job_id, source_dir, output_dir, archives),
        daemon=True,
    )
    thread.start()

    return job_id


def cancel_extraction(job_id: str) -> bool:
    """Cancel a running extraction."""
    if job_id not in _extract_jobs:
        return False
    _extract_jobs[job_id]["cancelled"] = True
    return True


def _extract_worker(
    job_id: str,
    source_dir: str,
    output_dir: str,
    specific_archives: Optional[list[str]],
):
    """Background extraction worker."""
    job = _extract_jobs[job_id]

    try:
        os.makedirs(output_dir, exist_ok=True)

        # Find archives to extract
        if specific_archives:
            archive_paths = specific_archives
        else:
            found = find_archives(source_dir)
            archive_paths = [a["path"] for a in found]

        job["total_archives"] = len(archive_paths)

        if not archive_paths:
            job["status"] = "completed"
            job["phase"] = "complete"
            job["progress"] = 100
            job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
            return

        job["phase"] = "extracting"
        total_files = 0

        for i, archive_path in enumerate(archive_paths):
            if job["cancelled"]:
                job["status"] = "cancelled"
                job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
                return

            job["current_archive"] = os.path.basename(archive_path)
            job["processed_archives"] = i
            job["progress"] = int((i / len(archive_paths)) * 100)

            try:
                extracted = _extract_single(archive_path, output_dir, job)
                total_files += extracted
            except Exception as e:
                job["errors"].append(f"{os.path.basename(archive_path)}: {str(e)}")

        job["processed_archives"] = len(archive_paths)
        job["files_extracted"] = total_files
        job["status"] = "completed"
        job["phase"] = "complete"
        job["progress"] = 100
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)


def _extract_single(archive_path: str, output_dir: str, job: dict) -> int:
    """Extract a single archive file. Returns count of files extracted."""
    path_lower = archive_path.lower()
    files_extracted = 0

    if path_lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                members = zf.namelist()
                for member in members:
                    if job.get("cancelled"):
                        return files_extracted
                    # Skip directories and __MACOSX junk
                    if member.endswith("/") or "__MACOSX" in member:
                        continue
                    try:
                        zf.extract(member, output_dir)
                        files_extracted += 1
                        job["files_extracted"] = job.get("files_extracted", 0) + 1
                    except (OSError, zipfile.BadZipFile) as e:
                        # Skip files that fail (permission issues, bad encoding)
                        pass
        except zipfile.BadZipFile as e:
            raise RuntimeError(f"Bad zip file: {e}")

    elif path_lower.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:gz") as tf:
            members = tf.getmembers()
            for member in members:
                if job.get("cancelled"):
                    return files_extracted
                if member.isfile():
                    try:
                        tf.extract(member, output_dir)
                        files_extracted += 1
                        job["files_extracted"] = job.get("files_extracted", 0) + 1
                    except (OSError, tarfile.TarError):
                        pass

    elif path_lower.endswith(".tar"):
        with tarfile.open(archive_path, "r:") as tf:
            members = tf.getmembers()
            for member in members:
                if job.get("cancelled"):
                    return files_extracted
                if member.isfile():
                    try:
                        tf.extract(member, output_dir)
                        files_extracted += 1
                        job["files_extracted"] = job.get("files_extracted", 0) + 1
                    except (OSError, tarfile.TarError):
                        pass

    elif path_lower.endswith(".gz") and not path_lower.endswith(".tar.gz"):
        import gzip
        out_name = Path(archive_path).stem
        out_path = os.path.join(output_dir, out_name)
        with gzip.open(archive_path, "rb") as gz_in:
            with open(out_path, "wb") as f_out:
                shutil.copyfileobj(gz_in, f_out)
        files_extracted = 1

    else:
        raise RuntimeError(f"Unsupported archive format: {Path(archive_path).suffix}")

    return files_extracted


def _human_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"
