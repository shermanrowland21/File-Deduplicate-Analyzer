"""
Standalone READ-ONLY Drive delta detect runner.

Runs drive_delta.detect() synchronously (so this process owns the job thread),
polling the in-memory job until it finishes, and writes progress to a JSON file
we can read from outside. Read-only: GAM print filelist queries only; no
downloads, no deletions.

Usage:
  set DELTA_BASELINE, ORGANIZED_ROOT, GAM_ADMIN_USER, GAM_PATH in env, then:
  python delta_detect_runner.py <scope>       # scope = drives | users | all
"""
import os
import sys
import json
import time

PROGRESS = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer",
                        "delta", "detect_runner_progress.json")


def _write(d: dict):
    os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)


def main():
    scope = sys.argv[1] if len(sys.argv) > 1 else "drives"
    from app.services import drive_delta as dd

    _write({"status": "starting", "scope": scope,
            "baseline": dd.BASELINE, "started_at": time.time()})

    job_id = dd.detect(scope=scope, admin_user=dd.DEFAULT_ADMIN_USER)

    while True:
        job = dd.get_detect_job(job_id) or {}
        snap = {
            "job_id": job_id,
            "status": job.get("status"),
            "phase": job.get("phase"),
            "scope": scope,
            "baseline": dd.BASELINE,
            "current": job.get("current"),
            "drives_total": job.get("drives_total"),
            "drives_done": job.get("drives_done"),
            "users_total": job.get("users_total"),
            "users_done": job.get("users_done"),
            "adds_mods": job.get("adds_mods"),
            "deletions": job.get("deletions"),
            "native_docs": job.get("native_docs"),
            "report_file": job.get("report_file"),
            "errors": job.get("errors"),
            "elapsed_seconds": job.get("elapsed_seconds"),
            "updated_at": time.time(),
        }
        _write(snap)
        if job.get("status") in ("completed", "error", "cancelled"):
            break
        time.sleep(5)

    print(json.dumps(snap, ensure_ascii=False))


if __name__ == "__main__":
    main()
