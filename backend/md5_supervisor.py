"""
CRASH-PROOF MD5 SUPERVISOR.

Runs a FULL-file MD5 index (ALL files, min_size=0) over a root and AUTO-RESTARTS
the job whenever it dies, until the whole tree is fully indexed. Full MD5 is
required because it matches Google Drive's md5Checksum — the basis for BOTH
deduplication AND repairing the mangled Google-Takeout folder/file structure.

Why a supervisor:
  - The underlying build is already hardened (one bad file never aborts it), but
    a supervisor guarantees the JOB itself is resurrected if the whole process
    ever dies (OOM, crash, machine hiccup, being killed by accident).
  - The index is resumable: a re-run skips every file already hashed (matched by
    path+size+mtime), so restarts cost nothing but a quick metadata re-walk of
    the already-done files.

Completion:
  - After each pass we check how many NEW files were hashed. When a full pass
    hashes 0 new files (everything already current), the tree is fully indexed
    and the supervisor exits.

Runs standalone (independent of the web server) so it survives backend restarts.
Progress + heartbeat written to a per-root JSON file.

Usage:
    python md5_supervisor.py "<root>"
"""
import sys
import os
import json
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services import md5_index as mi

MAX_PASSES = 50          # safety cap; normally completes in 1-2 passes
RESTART_WAIT = 8         # seconds between restarts after a failure


def _paths(root):
    d = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "md5_index")
    os.makedirs(d, exist_ok=True)
    label = mi._safe_label(root)
    return (os.path.join(d, f"supervisor_{label}.json"),
            os.path.join(d, f"supervisor_{label}.log"))


def _log(logf, msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(logf, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _write_progress(progress_file, data):
    try:
        tmp = progress_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, progress_file)
    except OSError:
        pass


def run_pass(root, progress_file, logf, pass_no):
    """Run ONE full-MD5 build pass to completion. Returns the final job dict."""
    job_id = mi.build_index(root, min_size=0)   # 0 = ALL files
    _log(logf, f"pass {pass_no}: started job {job_id}")
    while True:
        job = mi.get_build_job(job_id)
        if job is None:
            _log(logf, f"pass {pass_no}: job vanished")
            return {"status": "error", "files_hashed": 0}
        snap = {k: v for k, v in job.items() if k != "cancelled"}
        snap.update({"job_id": job_id, "pass": pass_no,
                     "supervisor_updated_at": time.time(), "root": root})
        _write_progress(progress_file, snap)
        status = job.get("status")
        if status in ("completed", "error", "cancelled"):
            _log(logf, f"pass {pass_no}: {status} "
                       f"hashed={job.get('files_hashed')} "
                       f"seen={job.get('files_seen')} "
                       f"skipped={job.get('skipped_current')} "
                       f"errors={job.get('errors')} "
                       f"GB={job.get('bytes_hashed',0)/1024**3:.1f}")
            return job
        _log(logf, f"pass {pass_no}: {job.get('phase')} "
                   f"hashed={job.get('files_hashed')} "
                   f"seen={job.get('files_seen')} "
                   f"errors={job.get('errors')} "
                   f"GB={job.get('bytes_hashed',0)/1024**3:.1f} "
                   f"cur={os.path.basename(job.get('current','') or '')[:50]}")
        time.sleep(15)


def main():
    if len(sys.argv) < 2:
        print("usage: python md5_supervisor.py <root>")
        return
    root = sys.argv[1]
    progress_file, logf = _paths(root)
    _log(logf, f"=== MD5 SUPERVISOR START root={root} (full MD5, all files) ===")

    if not os.path.isdir(root):
        _log(logf, f"root not found: {root}")
        return

    for pass_no in range(1, MAX_PASSES + 1):
        try:
            job = run_pass(root, progress_file, logf, pass_no)
        except Exception:
            _log(logf, f"pass {pass_no}: SUPERVISOR caught crash:\n{traceback.format_exc()}")
            time.sleep(RESTART_WAIT)
            continue

        hashed = (job or {}).get("files_hashed", 0)
        status = (job or {}).get("status")

        if status == "completed" and hashed == 0:
            _log(logf, f"pass {pass_no}: tree fully indexed (0 new files). DONE.")
            # final stats
            try:
                st = mi.open_index(root).stats()
                _log(logf, f"FINAL: {st['indexed_files']:,} files, "
                           f"{st['indexed_bytes']/1024**4:.2f} TB indexed for {root}")
            except Exception:
                pass
            _write_progress(progress_file, {"status": "done", "root": root,
                                            "finished_at": time.time()})
            return

        if status == "completed":
            _log(logf, f"pass {pass_no}: completed, hashed {hashed} new; "
                       f"running another pass to catch stragglers.")
            time.sleep(2)
            continue

        # error / cancelled -> wait and resume (resumable, so no lost work)
        _log(logf, f"pass {pass_no}: status={status}; restarting in {RESTART_WAIT}s (resumable).")
        time.sleep(RESTART_WAIT)

    _log(logf, "hit MAX_PASSES; stopping. Re-run supervisor to continue if needed.")


if __name__ == "__main__":
    main()
