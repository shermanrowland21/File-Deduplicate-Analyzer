"""
Standalone runner for the full-MD5 index build. Runs INDEPENDENTLY of the web
server so an 8-hour job survives backend restarts. Writes live progress to a
JSON file that can be polled from anywhere.

Usage:
    python md5_runner.py "<root>" [min_size_mb]
"""
import sys
import os
import json
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services import md5_index as mi

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else r"E:\Google Drive Files\Organized"
    min_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    # Per-root progress file so multiple concurrent runners (e.g. E: and D:)
    # don't overwrite each other's progress.
    label = mi._safe_label(root)
    progress_file = os.path.join(
        os.path.expanduser("~"), ".file_dedup_analyzer", "md5_index",
        f"runner_progress_{label}.json")
    global PROGRESS_FILE
    PROGRESS_FILE = progress_file
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)

    job_id = mi.build_index(root, min_size=min_mb * 1024 * 1024)
    print(f"[md5_runner] started job {job_id} on {root} (>= {min_mb} MB)", flush=True)

    # Poll the in-process job and mirror it to a progress file until done.
    while True:
        job = mi.get_build_job(job_id)
        if job is None:
            break
        snap = {k: v for k, v in job.items() if k != "cancelled"}
        snap["job_id"] = job_id
        snap["updated_at"] = time.time()
        try:
            tmp = PROGRESS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, ensure_ascii=False)
            os.replace(tmp, PROGRESS_FILE)
        except OSError:
            pass
        status = job.get("status")
        if status in ("completed", "error", "cancelled"):
            print(f"[md5_runner] job {status}: hashed={job.get('files_hashed')} "
                  f"bytes={job.get('bytes_hashed')} errors={job.get('errors')} "
                  f"elapsed={job.get('elapsed_seconds')}s", flush=True)
            break
        # heartbeat line for the process log
        print(f"[md5_runner] {job.get('phase')} seen={job.get('files_seen')} "
              f"hashed={job.get('files_hashed')} skipped={job.get('skipped_current')} "
              f"GB={job.get('bytes_hashed',0)/1024**3:.1f} cur={os.path.basename(job.get('current','') or '')[:50]}",
              flush=True)
        time.sleep(15)


if __name__ == "__main__":
    main()
