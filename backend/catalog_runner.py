"""
Standalone runner for a catalog refresh (metadata-only crawl). Runs
INDEPENDENTLY of the web server so a long crawl survives backend restarts.
Writes live progress to a per-root JSON file.

Usage:
    python catalog_runner.py "<root>"
    python catalog_runner.py            # refresh ALL registered roots
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services import catalog as cat


def _label(root):
    if not root:
        return "ALL"
    base = os.path.abspath(root).replace("\\", "_").replace("/", "_").replace(":", "")
    return base.strip("_") or "root"


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else None
    progress_file = os.path.join(
        os.path.expanduser("~"), ".file_dedup_analyzer", "catalog",
        f"crawl_progress_{_label(root)}.json")
    os.makedirs(os.path.dirname(progress_file), exist_ok=True)

    if root:
        cat.register_root(root)
    job_id = cat.refresh(root)
    print(f"[catalog_runner] started {job_id} root={root or 'ALL'}", flush=True)

    while True:
        job = cat.get_refresh_job(job_id)
        if job is None:
            break
        snap = {k: v for k, v in job.items() if k != "cancelled"}
        snap["job_id"] = job_id
        snap["updated_at"] = time.time()
        try:
            tmp = progress_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, ensure_ascii=False)
            os.replace(tmp, progress_file)
        except OSError:
            pass
        if job.get("status") in ("completed", "error", "cancelled"):
            print(f"[catalog_runner] {job.get('status')}: files={job.get('files_seen')} "
                  f"dirs={job.get('dirs_seen')} pruned={job.get('pruned')} "
                  f"elapsed={job.get('elapsed_seconds')}s", flush=True)
            break
        print(f"[catalog_runner] {job.get('phase')} files={job.get('files_seen')} "
              f"dirs={job.get('dirs_seen')} upserted={job.get('upserted')}", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    main()
