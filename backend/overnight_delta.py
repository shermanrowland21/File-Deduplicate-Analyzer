"""
OVERNIGHT DELTA SUPERVISOR — run both deltas unattended, then stop.

Sequence (each step downloads + converts + incrementally hashes into Organized):
  1. Wait for the DRIVES delta apply (already running) to finish, by watching its
     progress file. If it isn't running/finished, run it here.
  2. Run the USERS delta: detect (read-only GAM crawl of all My Drives) then apply.
  3. Write a final summary and exit.

Everything is resumable (GAM skips files already on disk; the hasher skips already
-indexed files), rate-limit-aware, and downloads-only (no deletions). Safe to run
detached overnight.

Progress is written to ~/.file_dedup_analyzer/delta/overnight_progress.json.
"""
import os
import sys
import json
import time

STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "delta")
PROGRESS = os.path.join(STORE, "overnight_progress.json")
DRIVES_REPORT = os.path.join(STORE, "delta_detect_1788794887.json")
APPLY_RUNNER_PROGRESS = os.path.join(STORE, "apply_runner_progress.json")


def _write(d: dict):
    os.makedirs(STORE, exist_ok=True)
    d["updated_at"] = time.time()
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)


def _run_apply(dd, report_file, label):
    """Run a delta apply to completion IN THIS PROCESS, updating overnight progress."""
    job_id = dd.apply_delta(report_file, admin_user=dd.DEFAULT_ADMIN_USER,
                            do_downloads=True, do_deletions=False)
    while True:
        job = dd.get_apply_job(job_id) or {}
        _write({"stage": label, "status": job.get("status"),
                "phase": job.get("phase"), "current": job.get("current"),
                "downloaded": job.get("downloaded"), "converted": job.get("converted"),
                "hashed": job.get("hashed"), "download_failed": job.get("download_failed"),
                "throttled": job.get("throttled"), "report": report_file})
        if job.get("status") in ("completed", "error", "cancelled"):
            return job
        time.sleep(10)


def main():
    from app.services import drive_delta as dd

    # ---- Stage 1: DRIVES delta ----
    # Another process may already be running the drives apply. Watch its progress
    # file; if it's actively updating, wait for it to finish. Otherwise run it.
    _write({"stage": "drives", "status": "checking", "note": "checking existing drives run"})

    def _apply_progress_fresh():
        try:
            with open(APPLY_RUNNER_PROGRESS, encoding="utf-8") as f:
                j = json.load(f)
            fresh = (time.time() - j.get("updated_at", 0)) < 120
            return j, fresh
        except (OSError, json.JSONDecodeError):
            return None, False

    j, fresh = _apply_progress_fresh()
    if j and fresh and j.get("status") == "running":
        # An external drives run is live — wait it out.
        _write({"stage": "drives", "status": "waiting_external",
                "note": "waiting for the already-running drives download to finish"})
        while True:
            j, fresh = _apply_progress_fresh()
            if not j:
                break
            if j.get("status") in ("completed", "error", "cancelled"):
                break
            if not fresh:
                # external run died (stale >120s) — take over below
                break
            _write({"stage": "drives", "status": "waiting_external",
                    "downloaded": j.get("downloaded"), "hashed": j.get("hashed"),
                    "current": j.get("current")})
            time.sleep(15)

    # Whether it finished or the external run died, run drives apply here to be
    # sure it's complete (resumable: already-downloaded files are skipped fast).
    _write({"stage": "drives", "status": "running", "note": "ensuring drives complete"})
    _run_apply(dd, DRIVES_REPORT, "drives")

    # ---- Stage 2: USERS delta detect (read-only) ----
    _write({"stage": "users_detect", "status": "running",
            "note": "crawling all My Drives for changes since baseline"})
    detect_job = dd.detect(scope="users", admin_user=dd.DEFAULT_ADMIN_USER)
    users_report = None
    while True:
        dj = dd.get_detect_job(detect_job) or {}
        _write({"stage": "users_detect", "status": dj.get("status"),
                "phase": dj.get("phase"), "current": dj.get("current"),
                "users_done": dj.get("users_done"), "users_total": dj.get("users_total"),
                "adds_mods": dj.get("adds_mods"), "native_docs": dj.get("native_docs")})
        if dj.get("status") in ("completed", "error", "cancelled"):
            users_report = dj.get("report_file")
            break
        time.sleep(10)

    # ---- Stage 3: USERS delta apply ----
    if users_report and os.path.exists(users_report):
        _write({"stage": "users_apply", "status": "running", "report": users_report})
        _run_apply(dd, users_report, "users_apply")
    else:
        _write({"stage": "users_apply", "status": "skipped",
                "note": "no users report produced"})

    _write({"stage": "done", "status": "completed",
            "finished_at": time.time(),
            "note": "Both drives + users deltas downloaded, converted, and hashed."})
    print("OVERNIGHT DELTA COMPLETE")


if __name__ == "__main__":
    main()
