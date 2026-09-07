"""
Standalone runner for the Drive blueprint crawl (shared drives only). Runs
independently of the web server so it survives crashes; resumable per-drive.
Progress written to blueprint_progress.json.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GAM_PATH", r"C:\GAM7\gam.exe")
os.environ.setdefault("GAM_ADMIN_USER", "sherman@hplapidary.com")
from app.services import drive_blueprint as bp

PROGRESS = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer",
                        "blueprint", "blueprint_progress.json")


def main():
    admin = os.environ.get("GAM_ADMIN_USER", "sherman@hplapidary.com")
    os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
    job_id = bp.build(admin_user=admin, force=False)
    print(f"[blueprint] started {job_id} admin={admin}", flush=True)
    while True:
        job = bp.get_build_job(job_id)
        if job is None:
            break
        snap = {k: v for k, v in job.items() if k != "cancelled"}
        snap.update({"job_id": job_id, "updated_at": time.time()})
        try:
            tmp = PROGRESS + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, ensure_ascii=False)
            os.replace(tmp, PROGRESS)
        except OSError:
            pass
        if job.get("status") in ("completed", "error", "cancelled"):
            print(f"[blueprint] {job.get('status')}: drives_done={job.get('drives_done')}/"
                  f"{job.get('drives_total')} files={job.get('files_indexed')} "
                  f"errors={len(job.get('errors',[]))}", flush=True)
            break
        print(f"[blueprint] {job.get('phase')} drive={job.get('current')} "
              f"done={job.get('drives_done')}/{job.get('drives_total')} "
              f"files={job.get('files_indexed')}", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    main()
