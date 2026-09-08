"""
Whole-tree folder reconcile runner (detached, unattended).

Walks all of E:\\Google Drive Files\\Organized deepest-first, merges split-twin
folders (Takeout truncation artifacts) and quarantines empty folders — reversible
via a master manifest. Local grouping only (no GAM). Writes progress to a file.

Usage:
  python reconcile_tree_runner.py preview     # dry run: counts + examples
  python reconcile_tree_runner.py apply       # do it (reversible)
"""
import os
import sys
import json
import time

STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "folder_reconcile")
PROGRESS = os.path.join(STORE, "tree_runner_progress.json")


def _write(d: dict):
    os.makedirs(STORE, exist_ok=True)
    d["updated_at"] = time.time()
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "preview"
    from app.services import folder_reconciler as fr

    if mode == "preview":
        _write({"mode": "preview", "status": "running"})
        rep = fr.reconcile_tree_preview()
        rep["mode"] = "preview"; rep["status"] = "completed"
        _write(rep)
        print(json.dumps({k: v for k, v in rep.items()
                          if k not in ("example_merges", "example_empties")}))
        return

    if mode == "apply":
        job_id = fr.reconcile_tree(confirm=True)
        while True:
            j = fr.get_tree_job(job_id) or {}
            _write({"mode": "apply", "status": j.get("status"),
                    "phase": j.get("phase"), "current": j.get("current"),
                    "parents_done": j.get("parents_done"),
                    "merges": j.get("merges"),
                    "shells_quarantined": j.get("shells_quarantined"),
                    "files_moved": j.get("files_moved"),
                    "empties_quarantined": j.get("empties_quarantined"),
                    "errors": j.get("errors"),
                    "master_manifest": j.get("master_manifest")})
            if j.get("status") in ("completed", "error", "cancelled"):
                break
            time.sleep(5)
        print("RECONCILE TREE DONE")
        return

    print("usage: reconcile_tree_runner.py [preview|apply]")


if __name__ == "__main__":
    main()
