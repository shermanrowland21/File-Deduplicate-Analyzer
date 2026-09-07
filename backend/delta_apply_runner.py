"""
Delta APPLY runner (downloads only by default).

Runs drive_delta.apply_delta() against a saved detect report, owning the job
thread, polling it to a progress file we can read from outside. Downloads
adds/mods into Organized/ (converting Google-native docs to Office), then
targeted-hashes exactly what was downloaded (no full-tree re-walk).

Usage:
  set DELTA_BASELINE, ORGANIZED_ROOT, GAM_ADMIN_USER, GAM_PATH in env, then:
  python delta_apply_runner.py <report_file> [deletions]
    deletions = "del" to also quarantine deletions; omit for downloads-only.
"""
import os
import sys
import json
import time

PROGRESS = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer",
                        "delta", "apply_runner_progress.json")


def _write(d: dict):
    os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)


def main():
    if len(sys.argv) < 2:
        print("usage: delta_apply_runner.py <report_file> [del]")
        sys.exit(1)
    report_file = sys.argv[1]
    do_deletions = len(sys.argv) > 2 and sys.argv[2].lower() == "del"

    from app.services import drive_delta as dd

    _write({"status": "starting", "report_file": report_file,
            "do_deletions": do_deletions, "started_at": time.time()})

    job_id = dd.apply_delta(report_file, admin_user=dd.DEFAULT_ADMIN_USER,
                            do_downloads=True, do_deletions=do_deletions)

    while True:
        job = dd.get_apply_job(job_id) or {}
        snap = {
            "job_id": job_id,
            "status": job.get("status"),
            "phase": job.get("phase"),
            "current": job.get("current"),
            "do_deletions": do_deletions,
            "adds_mods_total": job.get("adds_mods_total"),
            "downloaded": job.get("downloaded"),
            "converted": job.get("converted"),
            "download_failed": job.get("download_failed"),
            "quarantined": job.get("quarantined"),
            "downloaded_paths_count": job.get("downloaded_paths_count"),
            "downloaded_paths_file": job.get("downloaded_paths_file"),
            "hash_stats": job.get("hash_stats"),
            "errors": (job.get("errors") or [])[-10:],
            "error_count": len(job.get("errors") or []),
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
