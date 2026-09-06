"""
FOLDER RECONCILER — for one chosen folder under Organized/.

Reconciles the immediate subfolders of a chosen parent against (a) each other
(near-name sibling shells created by Google Takeout truncation) and (b) the
authoritative Google Drive structure (via GAM). Answers, per subfolder:

  has_content        : real files live here, no action needed
  empty_with_sibling : this folder is an empty/thin SHELL and a content-bearing
                       near-name twin exists locally  -> MERGE the twin(s) into
                       one clean folder (Drive-authoritative name if we have it)
  empty_no_sibling   : genuinely empty AND no local twin  -> BACKFILL candidate
                       (recreate from Dropbox first, then Google Drive/GAM)

Why this design (per the user):
  - "using GAM — honesty, that is better than trying to analyze our way to it":
    when Drive validation is on, the CANONICAL folder name comes from Drive, not
    from a guess. Local grouping still works offline (fast first pass).
  - Google Takeout truncates long folder names and, on re-export, leaves an
    EMPTY shell alongside a full-named/underscored twin that holds the files.
    We group those by a stable ANCHOR (leading date token + normalized stem)
    so the shell pairs with its content twin regardless of truncation point.
  - Everything is REVERSIBLE: merges move files collision-safe then quarantine
    the emptied shells (manifest); deletes quarantine the dir (manifest). Undo
    restores from the manifest. We never hard-rmdir user data.

READ-ONLY in scan(); merge/delete/backfill perform file ops on confirm.
"""
import os
import re
import csv
import json
import shutil
import subprocess
import threading
import time
from typing import Optional

from . import drive_reconcile as dr

ORGANIZED_ROOT = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
DROPBOX_ROOT = os.environ.get("DROPBOX_ROOT", r"D:\Highland Park Dropbox")
GAM_PATH = os.environ.get("GAM_PATH", r"C:\GAM7\gam.exe")
DEFAULT_ADMIN_USER = os.environ.get("GAM_ADMIN_USER", "admin@example.com")

STORE_DIR = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer")
RECON_DIR = os.path.join(STORE_DIR, "folder_reconcile")

# Folders we create ourselves — never treat as user content.
_OUR_DIRS = ("_EmptyQuarantine", "_MergeQuarantine", "_DeletedSince_",
             "_PreReexport_", "_ReconcileQuarantine")

_scan_jobs: dict = {}


# ---------------------------------------------------------------- name helpers

# Leading date token used across HPL folders: "22.01.21", "22.1.21", etc.
_DATE_RE = re.compile(r"^\s*(\d{2,4}[.\-_]\d{1,2}[.\-_]\d{1,2})")
# trailing dedup/truncation cruft: (1), trailing _/-/space
_DUP_RE = re.compile(r"\(\d+\)\s*$")


def _sanitize(component: str) -> str:
    bad = '<>:"|?*'
    out = "".join("_" if c in bad else c for c in component)
    return out.rstrip(" .") or "_"


def _norm_folder(name: str) -> str:
    """
    Normalize a folder name for sibling grouping: lowercase, strip a trailing
    (N) dedup marker, collapse whitespace, and strip trailing '_'/'-'/space left
    by Takeout truncation. Keeps the meaningful stem.
    """
    s = name.lower()
    s = _DUP_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip("_-. ").strip()
    return s


def _date_key(name: str) -> Optional[str]:
    """Leading date token normalized to dots (stable anchor). None if absent."""
    m = _DATE_RE.match(name)
    if not m:
        return None
    return re.sub(r"[.\-_]", ".", m.group(1))


# How many chars of the post-date title to fold into the anchor. Long enough to
# separate genuinely different same-date sessions (different titles), short
# enough that truncation variants of ONE session still share it. Truncation
# tends to lop off the TAIL (e.g. "…Essentials" vs "…Essentials - Part 2"), so a
# leading title prefix is stable across variants.
_TITLE_PREFIX = 14


# Common boilerplate lead-ins that carry no distinguishing meaning. Stripped
# (from the front, repeatedly) so the anchor's title prefix reflects the ACTUAL
# session topic, not shared filler like "Live Webinar -".
_BOILERPLATE_LEAD = re.compile(
    r"^(?:live\s+webinar|webinar|hplive\s+webinar|hpl\s+live)\s*[-:.\s]*", re.I)


def _title_after_date(name: str) -> str:
    """
    Normalized title with the leading date token AND common boilerplate
    ("Live Webinar -", "Webinar -", …) removed, so the remaining text is the
    distinguishing topic.
    """
    low = _norm_folder(name)
    # drop leading date token + separators
    low = re.sub(r"^\s*\d{2,4}[.\-_]\d{1,2}[.\-_]\d{1,2}[\s.\-_]*", "", low)
    # strip repeated boilerplate lead-ins
    prev = None
    while prev != low:
        prev = low
        low = _BOILERPLATE_LEAD.sub("", low).strip()
    return low.strip()


def _anchor(name: str) -> str:
    """
    Grouping anchor for a subfolder. Uses the leading date token PLUS a short
    prefix of the post-date title. The date keeps a session's truncation
    variants together; the title prefix prevents two DIFFERENT sessions that
    happen to share a date from being wrongly merged.

    Truncation removes the TAIL of a name, so a leading title prefix is stable
    across a session's variants (empty shell, "_"-suffixed, full name).
    """
    dk = _date_key(name)
    if dk:
        title = _title_after_date(name)
        return f"d:{dk}|{title[:_TITLE_PREFIX]}"
    return "n:" + _norm_folder(name)[:24]


# ---------------------------------------------------------------- local scan

def _count_files(path: str) -> int:
    n = 0
    for _root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIRS)]
        n += len(files)
    return n


def _dir_bytes(path: str) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIRS)]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _immediate_subfolders(parent: str) -> list[str]:
    out = []
    try:
        for e in os.scandir(parent):
            if e.is_dir() and not e.name.startswith(_OUR_DIRS):
                out.append(e.name)
    except OSError:
        pass
    return sorted(out)


# ------------------------------------------------------- Drive authoritative

def _account_and_rel(parent: str) -> tuple[str, str, str]:
    """
    Given an absolute parent path UNDER Organized/, return
    (account_label, rel_under_account, rel_from_organized).
    account_label is the first path component under Organized (e.g. the shared
    drive / user folder name). rel_under_account is the path below it.
    """
    ap = os.path.abspath(parent)
    root = os.path.abspath(ORGANIZED_ROOT)
    if not ap.lower().startswith(root.lower()):
        return "", "", ""
    rel = os.path.relpath(ap, root).replace("\\", "/")
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return "", "", ""
    account = parts[0]
    rel_under = "/".join(parts[1:])
    return account, rel_under, rel


def _gam_child_folders(admin_user: str, teamdrive_id: str,
                       parent_id: str) -> list[dict]:
    """
    Fast, TARGETED GAM query: immediate CHILD folders of parent_id inside a
    shared drive. Returns [{id, name}]. Avoids indexing the whole drive.
    """
    out = os.path.join(RECON_DIR, f"children_{parent_id}.csv")
    q = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    args = ["user", admin_user, "print", "filelist",
            "select", "teamdriveid", teamdrive_id,
            "query", q, "fields", "id,name"]
    dr._gam_csv(out, args, timeout=300, expect_rows=False, retries=1)
    rows = dr._read_csv(out)
    return [{"id": (r.get("id") or "").strip(), "name": r.get("name") or ""}
            for r in rows if (r.get("id") or "").strip()]


def _gam_child_files(admin_user: str, teamdrive_id: str,
                     parent_id: str) -> list[dict]:
    """
    Fast, TARGETED GAM query: immediate CHILD FILES (non-folders) of parent_id
    inside a shared drive. Returns [{id, name, mime}]. Skips shortcuts (pointers
    with no downloadable content).
    """
    out = os.path.join(RECON_DIR, f"files_{parent_id}.csv")
    q = (f"'{parent_id}' in parents and "
         f"mimeType != 'application/vnd.google-apps.folder' and trashed = false")
    args = ["user", admin_user, "print", "filelist",
            "select", "teamdriveid", teamdrive_id,
            "query", q, "fields", "id,name,mimeType"]
    dr._gam_csv(out, args, timeout=600, expect_rows=False, retries=1)
    rows = dr._read_csv(out)
    files = []
    for r in rows:
        fid = (r.get("id") or "").strip()
        mime = (r.get("mimeType") or "").strip()
        if not fid:
            continue
        if mime == "application/vnd.google-apps.shortcut":
            continue
        files.append({"id": fid, "name": r.get("name") or "", "mime": mime})
    return files


def _resolve_drive_folder_id(admin_user: str, teamdrive_id: str,
                             rel_under: str) -> Optional[str]:
    """
    Walk the shared drive from its ROOT down the rel_under path components,
    resolving each name to a folder id with a targeted per-level GAM query.
    The shared drive root id == teamdrive_id. Returns the id of the folder at
    rel_under, or None if any component is missing/ambiguous.
    """
    current = teamdrive_id  # shared drive root
    parts = [p for p in rel_under.strip("/").split("/") if p]
    for comp in parts:
        children = _gam_child_folders(admin_user, teamdrive_id, current)
        match = [c for c in children if c["name"].strip().lower() == comp.strip().lower()]
        if not match:
            return None
        current = match[0]["id"]
    return current


def _drive_authoritative_subfolders(parent: str, admin_user: str,
                                    job: dict) -> Optional[dict]:
    """
    Use GAM to fetch the authoritative immediate-subfolder names of `parent`'s
    Drive counterpart. Returns {anchor: authoritative_name} or None if we can't
    resolve it (no GAM / not under Organized / drive not found).

    FAST/TARGETED strategy (does NOT index the whole drive): `parent` is under
    Organized/<account>/<rel...>. <account> maps to a shared-drive id. We walk
    the drive from its root down <rel...> resolving one level at a time, then
    list only the target folder's direct child folders. Each authoritative name
    is anchored so it can be matched to local sibling groups.
    """
    if not dr.gam_available():
        job["drive_note"] = "GAM not available — skipped Drive validation."
        return None
    account, rel_under, _ = _account_and_rel(parent)
    if not account:
        job["drive_note"] = "Folder is not under Organized/ — skipped Drive validation."
        return None

    # Resolve the account to a shared-drive id.
    job["phase"] = "drive_listing"
    teamdrive_id = None
    try:
        for d in dr.list_shared_drives():
            if d["name"].strip().lower() == account.strip().lower():
                teamdrive_id = d["id"]
                break
    except Exception as e:
        job["drive_note"] = f"Shared-drive listing failed: {e}"
        return None
    if teamdrive_id is None:
        job["drive_note"] = (f"No shared drive named '{account}' found; "
                             f"cannot Drive-validate.")
        return None

    job["phase"] = "drive_resolve_path"
    try:
        target_id = _resolve_drive_folder_id(admin_user, teamdrive_id, rel_under)
    except Exception as e:
        job["drive_note"] = f"Drive path resolve failed: {e}"
        return None
    if not target_id:
        job["drive_note"] = (f"Could not locate '{rel_under}' inside shared drive "
                             f"'{account}'.")
        return None

    job["phase"] = "drive_children"
    try:
        children = _gam_child_folders(admin_user, teamdrive_id, target_id)
    except Exception as e:
        job["drive_note"] = f"Drive children fetch failed: {e}"
        return None

    result: dict = {}
    for c in children:
        result[_anchor(c["name"])] = c["name"]
    job["drive_subfolders_found"] = len(result)
    return result


# ---------------------------------------------------- Drive backfill resolve

def _resolve_parent_drive_target(parent: str, admin_user: str) -> tuple:
    """
    Resolve the local `parent` (under Organized/) to (teamdrive_id, target_id,
    note). target_id is the Drive folder id whose CHILDREN correspond to
    `parent`'s subfolders. Returns (None, None, reason) on failure.
    """
    if not dr.gam_available():
        return None, None, "GAM not available."
    account, rel_under, _ = _account_and_rel(parent)
    if not account:
        return None, None, "Folder is not under Organized/."
    teamdrive_id = None
    try:
        for d in dr.list_shared_drives():
            if d["name"].strip().lower() == account.strip().lower():
                teamdrive_id = d["id"]
                break
    except Exception as e:
        return None, None, f"Shared-drive listing failed: {e}"
    if teamdrive_id is None:
        return None, None, f"No shared drive named '{account}'."
    try:
        target_id = _resolve_drive_folder_id(admin_user, teamdrive_id, rel_under)
    except Exception as e:
        return None, None, f"Drive path resolve failed: {e}"
    if not target_id:
        return None, None, f"Could not locate '{rel_under}' in shared drive '{account}'."
    return teamdrive_id, target_id, None


def _drive_subfolder_id_for(admin_user: str, teamdrive_id: str, target_id: str,
                            folder_name: str) -> tuple:
    """
    Among the Drive children of target_id, find the folder matching folder_name
    (exact, else anchor-match to tolerate truncation differences). Returns
    (folder_id, authoritative_drive_name) or (None, None).
    """
    children = _gam_child_folders(admin_user, teamdrive_id, target_id)
    # exact (case-insensitive) first
    for c in children:
        if c["name"].strip().lower() == folder_name.strip().lower():
            return c["id"], c["name"]
    # anchor match (date-token / normalized prefix)
    want = _anchor(folder_name)
    for c in children:
        if _anchor(c["name"]) == want:
            return c["id"], c["name"]
    return None, None


def _drive_folder_file_tree(admin_user: str, teamdrive_id: str,
                            folder_id: str, _depth: int = 0) -> list[dict]:
    """
    Recursively gather every FILE under a Drive folder id, each with its path
    RELATIVE to that folder (so subfolders are recreated locally). Returns
    [{id, name, mime, rel_dir}] where rel_dir is "" for top-level files.
    Bounded recursion depth to avoid pathological trees.
    """
    if _depth > 25:
        return []
    out = []
    for f in _gam_child_files(admin_user, teamdrive_id, folder_id):
        out.append({"id": f["id"], "name": f["name"], "mime": f["mime"],
                    "rel_dir": ""})
    for sub in _gam_child_folders(admin_user, teamdrive_id, folder_id):
        child = _drive_folder_file_tree(admin_user, teamdrive_id, sub["id"],
                                        _depth + 1)
        for c in child:
            c["rel_dir"] = (sub["name"] if not c["rel_dir"]
                            else sub["name"] + "/" + c["rel_dir"])
            out.append(c)
    return out


# ---------------------------------------------------------------- classify

def _classify(parent: str, drive_names: Optional[dict]) -> dict:
    """
    Build the reconciliation report for `parent`'s immediate subfolders.
    """
    subs = _immediate_subfolders(parent)
    info = []
    for name in subs:
        ap = os.path.join(parent, name)
        fc = _count_files(ap)
        info.append({
            "name": name, "path": ap, "files": fc,
            "bytes": _dir_bytes(ap) if fc else 0,
            "anchor": _anchor(name),
            "empty": fc == 0,
        })

    # group by anchor
    groups: dict = {}
    for it in info:
        groups.setdefault(it["anchor"], []).append(it)

    merge_groups = []      # empty shell(s) + content twin(s) sharing an anchor
    backfill = []          # empty, no content twin
    ok = []                # has content, no shell to merge

    for anchor, members in groups.items():
        with_content = [m for m in members if m["files"] > 0]
        empties = [m for m in members if m["files"] == 0]
        drive_name = drive_names.get(anchor) if drive_names else None

        if len(members) > 1 and with_content:
            # near-name sibling group -> merge into one clean folder.
            # canonical: Drive-authoritative name if we have it, else the
            # richest content folder's name (most files).
            richest = max(with_content, key=lambda m: (m["files"], m["bytes"]))
            canonical = drive_name or richest["name"]
            merge_groups.append({
                "anchor": anchor,
                "canonical_name": canonical,
                "canonical_from_drive": bool(drive_name),
                "target_exists": any(m["name"] == canonical for m in members),
                "members": sorted(members, key=lambda m: -m["files"]),
                "total_files": sum(m["files"] for m in members),
                "shell_count": len(empties),
            })
        elif len(members) == 1 and members[0]["files"] == 0:
            m = members[0]
            backfill.append({
                "name": m["name"], "path": m["path"],
                "anchor": anchor,
                "drive_name": drive_name,
                "drive_has_content": None,   # filled by /backfill detect
            })
        else:
            # single folder with content, or multiple empties w/ no content twin
            if with_content:
                for m in with_content:
                    ok.append({"name": m["name"], "path": m["path"],
                               "files": m["files"], "bytes": m["bytes"]})
            for m in empties:
                # empties that share an anchor only with other empties
                backfill.append({
                    "name": m["name"], "path": m["path"], "anchor": anchor,
                    "drive_name": drive_name, "drive_has_content": None,
                })

    return {
        "parent": parent,
        "subfolder_count": len(subs),
        "merge_groups": sorted(merge_groups, key=lambda g: g["canonical_name"].lower()),
        "backfill": sorted(backfill, key=lambda b: b["name"].lower()),
        "ok": sorted(ok, key=lambda o: o["name"].lower()),
        "counts": {
            "subfolders": len(subs),
            "merge_groups": len(merge_groups),
            "shells_to_merge": sum(g["shell_count"] for g in merge_groups),
            "backfill_candidates": len(backfill),
            "ok": len(ok),
        },
        "drive_validated": drive_names is not None,
    }


# ---------------------------------------------------------------- scan job

def scan(parent: str, validate_drive: bool = False,
         admin_user: str = DEFAULT_ADMIN_USER) -> str:
    os.makedirs(RECON_DIR, exist_ok=True)
    job_id = f"foldrecon_{int(time.time())}"
    _scan_jobs[job_id] = {
        "status": "running", "phase": "starting", "parent": parent,
        "validate_drive": validate_drive, "admin_user": admin_user,
        "started_at": time.time(), "report": None, "error": None,
        "drive_note": None, "drive_subfolders_found": 0,
    }
    threading.Thread(target=_scan_worker,
                     args=(job_id, parent, validate_drive, admin_user),
                     daemon=True).start()
    return job_id


def _scan_worker(job_id, parent, validate_drive, admin_user):
    job = _scan_jobs[job_id]
    try:
        if not os.path.isdir(parent):
            job["status"] = "error"
            job["error"] = f"Folder not found: {parent}"
            return
        drive_names = None
        if validate_drive:
            drive_names = _drive_authoritative_subfolders(parent, admin_user, job)
        job["phase"] = "classifying"
        report = _classify(parent, drive_names)
        report["drive_note"] = job.get("drive_note")
        job["report"] = report
        job["phase"] = "complete"
        job["status"] = "completed"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def get_scan_job(job_id: str):
    return _scan_jobs.get(job_id)


# ---------------------------------------------------------------- merge-safe

def _collision_safe(dst: str) -> str:
    if not os.path.exists(dst):
        return dst
    parent = os.path.dirname(dst)
    base = os.path.basename(dst)
    name, ext = os.path.splitext(base)
    i = 1
    while True:
        cand = os.path.join(parent, f"{name} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def _move_file_merge_safe(src: str, dst_dir: str, stats: dict):
    """
    Move one file into dst_dir. If a same-name file already exists AND is
    byte-identical (same size), skip (leave source for the shell to be
    quarantined). If it differs, move under a collision-safe name.
    """
    name = os.path.basename(src)
    target = os.path.join(dst_dir, name)
    try:
        src_size = os.path.getsize(src)
    except OSError:
        src_size = -1
    if os.path.exists(target):
        try:
            if os.path.getsize(target) == src_size:
                stats["skipped_identical"] += 1
                # remove the identical source copy so the shell empties out
                try:
                    os.remove(src)
                    stats["identical_source_removed"] += 1
                except OSError:
                    pass
                return
        except OSError:
            pass
        target = _collision_safe(target)
        stats["renamed_on_collision"] += 1
    os.makedirs(dst_dir, exist_ok=True)
    try:
        shutil.move(src, target)
        stats["moved"] += 1
    except OSError as e:
        stats["errors"].append(f"{src}: {e}")


def _move_tree_into(src_dir: str, dst_dir: str, stats: dict):
    """Move every file under src_dir into dst_dir, preserving subfolders."""
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIRS)]
        rel = os.path.relpath(root, src_dir)
        target_dir = dst_dir if rel == "." else os.path.join(dst_dir, rel)
        for f in files:
            _move_file_merge_safe(os.path.join(root, f), target_dir, stats)


# ---------------------------------------------------------------- quarantine

def _quarantine_dir_name(kind: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"_{kind}Quarantine_{stamp}"


def _quarantine_folder(folder: str, quarantine_root: str, manifest: list):
    """Move an (expected-empty-of-real-files) shell folder into quarantine."""
    name = os.path.basename(folder.rstrip("\\/"))
    dest = _collision_safe(os.path.join(quarantine_root, name))
    os.makedirs(quarantine_root, exist_ok=True)
    shutil.move(folder, dest)
    manifest.append({"original": folder, "quarantined_to": dest})


# ---------------------------------------------------------------- merge action

def merge_group(parent: str, canonical_name: str, member_names: list[str],
                confirm: bool = False) -> dict:
    """
    Merge near-name sibling subfolders under `parent` into one clean folder
    named `canonical_name`. Files from every member are moved (collision-safe)
    into the canonical folder; emptied shells are quarantined (reversible).
    """
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.isdir(parent):
        return {"error": f"parent not found: {parent}"}

    canonical_name = _sanitize(canonical_name)
    canonical_path = os.path.join(parent, canonical_name)
    os.makedirs(canonical_path, exist_ok=True)

    stats = {"moved": 0, "skipped_identical": 0, "identical_source_removed": 0,
             "renamed_on_collision": 0, "errors": [], "quarantined_shells": 0}
    q_root = os.path.join(parent, _quarantine_dir_name("Merge"))
    manifest_entries = []

    # move each member (except the canonical target itself) into canonical
    for mname in member_names:
        src_dir = os.path.join(parent, mname)
        if os.path.abspath(src_dir) == os.path.abspath(canonical_path):
            continue
        if not os.path.isdir(src_dir):
            stats["errors"].append(f"missing member: {mname}")
            continue
        _move_tree_into(src_dir, canonical_path, stats)
        # after moving files, the shell should be empty of files -> quarantine it
        if _count_files(src_dir) == 0:
            try:
                _quarantine_folder(src_dir, q_root, manifest_entries)
                stats["quarantined_shells"] += 1
            except OSError as e:
                stats["errors"].append(f"quarantine {mname}: {e}")
        else:
            stats["errors"].append(
                f"{mname}: files remain after merge, left in place (not quarantined)")

    manifest = {
        "action": "merge", "parent": parent,
        "canonical_path": canonical_path, "canonical_name": canonical_name,
        "members": member_names, "quarantine_root": q_root,
        "shells": manifest_entries, "at": time.time(), "stats": stats,
    }
    mpath = _write_manifest("merge", manifest)
    manifest["manifest_file"] = mpath
    return manifest


# ---------------------------------------------------------------- delete action

def delete_empty(paths: list[str], confirm: bool = False) -> dict:
    """
    Quarantine empty folders (reversible). Refuses any folder that still
    contains files, as a safety guard.
    """
    if not confirm:
        return {"error": "confirm=true required"}
    quarantined = []
    skipped = []
    errors = []
    # group quarantine per parent so undo is localized
    by_parent: dict = {}
    for p in paths:
        by_parent.setdefault(os.path.dirname(p.rstrip("\\/")), []).append(p)

    manifest_entries = []
    for parent, plist in by_parent.items():
        q_root = os.path.join(parent, _quarantine_dir_name("Empty"))
        for folder in plist:
            if not os.path.isdir(folder):
                skipped.append({"path": folder, "reason": "not found"})
                continue
            fc = _count_files(folder)
            if fc > 0:
                skipped.append({"path": folder,
                                "reason": f"not empty ({fc} files) — refused"})
                continue
            try:
                _quarantine_folder(folder, q_root, manifest_entries)
                quarantined.append(folder)
            except OSError as e:
                errors.append(f"{folder}: {e}")

    manifest = {
        "action": "delete_empty", "quarantined": manifest_entries,
        "at": time.time(),
    }
    mpath = _write_manifest("delete", manifest)
    return {
        "quarantined_count": len(quarantined), "quarantined": quarantined,
        "skipped": skipped, "errors": errors, "manifest_file": mpath,
    }


# ---------------------------------------------------------------- undo

def undo(manifest_file: str, confirm: bool = False) -> dict:
    """Reverse a merge or delete using its manifest."""
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.exists(manifest_file):
        return {"error": f"manifest not found: {manifest_file}"}
    with open(manifest_file, "r", encoding="utf-8") as f:
        man = json.load(f)

    restored, errors = 0, []
    action = man.get("action")
    if action in ("merge", "delete_empty"):
        shells = man.get("shells") if action == "merge" else man.get("quarantined")
        for entry in (shells or []):
            orig = entry["original"]
            quar = entry["quarantined_to"]
            try:
                if os.path.exists(orig):
                    errors.append(f"exists, skipped restore: {orig}")
                    continue
                os.makedirs(os.path.dirname(orig), exist_ok=True)
                shutil.move(quar, orig)
                restored += 1
            except OSError as e:
                errors.append(f"{quar}: {e}")
        # NOTE: for merge, files moved INTO canonical are not un-merged
        # automatically (they were consolidated on purpose). Restoring the
        # shell folders brings the structure back; a full file-level un-merge
        # is intentionally out of scope to avoid re-creating duplicates.
    else:
        return {"error": f"unknown manifest action: {action}"}

    return {"action": action, "restored": restored, "errors": errors,
            "note": ("Shell folders restored. For merges, consolidated files "
                     "stay in the canonical folder by design.")}


# ---------------------------------------------------------------- backfill

def _dropbox_candidate(parent: str, folder_name: str) -> Optional[dict]:
    """
    Look for the same relative path under the local Dropbox root that HAS files.
    Maps Organized/<account>/<rel>/<folder> -> Dropbox/<rel>/<folder> (account
    dropped, since Dropbox has no per-account top). Best-effort.
    """
    account, rel_under, _ = _account_and_rel(parent)
    if not os.path.isdir(DROPBOX_ROOT):
        return None
    rel = (rel_under + "/" + folder_name).strip("/")
    cand = os.path.join(DROPBOX_ROOT, rel.replace("/", os.sep))
    if os.path.isdir(cand):
        fc = _count_files(cand)
        if fc > 0:
            return {"source": "dropbox", "path": cand, "files": fc}
    return None


def detect_backfill(parent: str, folder_names: list[str],
                    admin_user: str = DEFAULT_ADMIN_USER) -> dict:
    """
    Read-only: for each empty folder, report where content could come from —
    local Dropbox (preferred) or Google Drive (GAM). Drive detection reuses the
    authoritative subfolder scan (folder present in Drive w/ children).
    """
    results = []
    # Resolve the Drive target once so we can (a) confirm the folder exists and
    # (b) count downloadable files inside it — the real signal for backfill.
    teamdrive_id, target_id, note = _resolve_parent_drive_target(parent, admin_user)

    for name in folder_names:
        entry = {"name": name, "dropbox": None, "drive": None}
        db = _dropbox_candidate(parent, name)
        if db:
            entry["dropbox"] = db
        if teamdrive_id and target_id:
            try:
                sub_id, drive_name = _drive_subfolder_id_for(
                    admin_user, teamdrive_id, target_id, name)
                if sub_id:
                    tree = _drive_folder_file_tree(admin_user, teamdrive_id, sub_id)
                    entry["drive"] = {
                        "folder_id": sub_id,
                        "file_count": len(tree),
                        "authoritative_name": drive_name,
                        "rename_to": drive_name if drive_name != name else None,
                    }
            except Exception as e:
                entry["drive_error"] = str(e)
        results.append(entry)
    return {"parent": parent, "candidates": results, "drive_note": note}


def backfill_from_dropbox(parent: str, folder_name: str, dropbox_path: str,
                          confirm: bool = False) -> dict:
    """Copy files from a local Dropbox folder into the (empty) local folder."""
    if not confirm:
        return {"error": "confirm=true required"}
    if not os.path.isdir(dropbox_path):
        return {"error": f"dropbox source not found: {dropbox_path}"}
    dest = os.path.join(parent, _sanitize(folder_name))
    os.makedirs(dest, exist_ok=True)
    stats = {"copied": 0, "bytes": 0, "errors": []}
    for root, dirs, files in os.walk(dropbox_path):
        dirs[:] = [d for d in dirs if not d.startswith(_OUR_DIRS)]
        rel = os.path.relpath(root, dropbox_path)
        tdir = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(tdir, exist_ok=True)
        for f in files:
            src = os.path.join(root, f)
            tgt = os.path.join(tdir, f)
            if os.path.exists(tgt):
                try:
                    if os.path.getsize(tgt) == os.path.getsize(src):
                        continue
                except OSError:
                    pass
                tgt = _collision_safe(tgt)
            try:
                shutil.copy2(src, tgt)
                stats["copied"] += 1
                stats["bytes"] += os.path.getsize(tgt)
            except OSError as e:
                stats["errors"].append(f"{src}: {e}")
    return {"action": "backfill_dropbox", "dest": dest, "source": dropbox_path,
            "stats": stats}


# ------------------------------------------------------- Drive backfill jobs

_backfill_jobs: dict = {}


def backfill_from_drive(parent: str, folder_name: str,
                        admin_user: str = DEFAULT_ADMIN_USER,
                        confirm: bool = False) -> str:
    """
    Start a background job that downloads every file inside the Drive folder
    matching `folder_name` into the local folder, recreating subfolder
    structure. Native Google docs are exported to Office formats (via GAM).
    Returns a job id (Drive downloads can take a while).
    """
    if not confirm:
        raise ValueError("confirm=true required")
    os.makedirs(RECON_DIR, exist_ok=True)
    job_id = f"drivebf_{int(time.time())}"
    _backfill_jobs[job_id] = {
        "status": "running", "phase": "resolving", "parent": parent,
        "folder_name": folder_name, "downloaded": 0, "total": 0,
        "errors": [], "dest": None, "started_at": time.time(),
    }
    threading.Thread(target=_backfill_drive_worker,
                     args=(job_id, parent, folder_name, admin_user),
                     daemon=True).start()
    return job_id


def get_backfill_job(job_id: str):
    return _backfill_jobs.get(job_id)


def _backfill_drive_worker(job_id, parent, folder_name, admin_user):
    job = _backfill_jobs[job_id]
    try:
        teamdrive_id, target_id, note = _resolve_parent_drive_target(parent, admin_user)
        if not (teamdrive_id and target_id):
            job["status"] = "error"; job["error"] = note or "Drive target unresolved"
            return
        job["phase"] = "locating_folder"
        sub_id, drive_name = _drive_subfolder_id_for(admin_user, teamdrive_id,
                                                     target_id, folder_name)
        if not sub_id:
            job["status"] = "error"
            job["error"] = f"'{folder_name}' not found in Drive under this parent."
            return
        # Use the AUTHORITATIVE Drive name for the destination folder.
        canonical = _sanitize(drive_name or folder_name)
        job["canonical_name"] = canonical
        job["renamed_from"] = folder_name if canonical != _sanitize(folder_name) else None

        job["phase"] = "listing_files"
        tree = _drive_folder_file_tree(admin_user, teamdrive_id, sub_id)
        job["total"] = len(tree)
        if not tree:
            job["status"] = "completed"; job["phase"] = "empty_in_drive"
            job["note"] = "Drive folder has no files to download."
            return

        dest = os.path.join(parent, canonical)
        os.makedirs(dest, exist_ok=True)
        job["dest"] = dest
        job["phase"] = "downloading"
        for f in tree:
            rel_dir = f.get("rel_dir") or ""
            target_dir = dest
            if rel_dir:
                safe_parts = [_sanitize(p) for p in rel_dir.split("/") if p]
                target_dir = os.path.join(dest, *safe_parts)
            os.makedirs(target_dir, exist_ok=True)
            try:
                ok = dr._gam_download(admin_user, f["id"], f["mime"], target_dir)
                if ok:
                    job["downloaded"] += 1
                else:
                    job["errors"].append(f"download failed: {f['name']}")
            except Exception as e:
                job["errors"].append(f"{f['name']}: {e}")

        # If we downloaded into a DIFFERENT (authoritative) name than the local
        # empty shell, quarantine the now-orphaned empty shell so we don't leave
        # a stray truncated folder behind. Reversible (manifest).
        manifest_entries = []
        shell = os.path.join(parent, _sanitize(folder_name))
        if (canonical != _sanitize(folder_name) and os.path.isdir(shell)
                and _count_files(shell) == 0):
            try:
                q_root = os.path.join(parent, _quarantine_dir_name("Backfill"))
                _quarantine_folder(shell, q_root, manifest_entries)
                job["quarantined_shell"] = shell
            except OSError as e:
                job["errors"].append(f"quarantine shell {folder_name}: {e}")
        if manifest_entries:
            mpath = _write_manifest("backfill_drive", {
                "action": "delete_empty", "quarantined": manifest_entries,
                "dest": dest, "at": time.time(),
            })
            job["manifest_file"] = mpath

        job["status"] = "completed"; job["phase"] = "complete"
        job["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)


# ---------------------------------------------------------------- manifest io

def _write_manifest(kind: str, data: dict) -> str:
    os.makedirs(RECON_DIR, exist_ok=True)
    path = os.path.join(RECON_DIR, f"{kind}_{int(time.time()*1000)}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path
