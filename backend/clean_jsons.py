"""
Remove Google Takeout metadata sidecars from the Organized/ tree.

SAFETY:
  - DRY RUN by default (pass --delete to actually remove).
  - Only removes .json files that are provably Takeout metadata:
      1. <name>.<realext>.json      (double-extension sidecar, e.g. foo.pdf.json)
      2. *-info.json / *-i.json
      3. Shared Drive Metadata.json
      4. <name>.json WHEN a sibling file exists in the same folder whose name
         starts with the same base (the sidecar pairs with a real file)
  - Also removes archive_browser.html and Workspaces.json.
  - A standalone <name>.json with NO matching sibling and no metadata pattern is
    KEPT (could be real user data).
  - Never touches non-.json/.html files.
"""
import os
import sys

TARGET = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
DELETE = "--delete" in sys.argv

_SIDECAR_INNER_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "rtf",
    "odt", "ods", "odp", "html", "htm",
    "jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "heic", "webp", "svg",
    "psd", "ai", "eps",
    "mp4", "mov", "m4v", "avi", "mkv", "wmv", "webm", "mpg", "mpeg",
    "mp3", "m4a", "wav", "aac", "flac", "ogg", "wma",
    "zip", "rar", "7z", "gz",
    "gdoc", "gsheet", "gslides", "gform", "gdraw", "gsite", "gmap", "gscript",
    "prproj", "aep", "als", "wig",
}
_NAME_CLUTTER = {"archive_browser.html", "workspaces.json"}


import re

# Google truncates long names, producing sidecars whose "-info" tail is cut to
# "-inf", "-in", "-i", or even "-", optionally followed by a "(N)" dedup suffix,
# then ".json".  e.g. "Report.pdf-.json", "file.xlsx-in.json", "x-info(1).json"
_TRUNC_INFO_RE = re.compile(r"-(?:info|inf|in|i)?(?:\(\d+\))?\.json$")


def is_metadata_by_name(low: str) -> bool:
    if low in _NAME_CLUTTER:
        return True
    if low == "shared drive metadata.json":
        return True
    if low.endswith("-comments.html"):
        return True
    # double-extension sidecar: <name>.<realext>.json (+ optional -info/(N))
    if low.endswith(".json"):
        stem = low[:-5]
        # strip a trailing "-info"/"-inf"/etc and "(N)" before checking inner ext
        stem2 = re.sub(r"-(?:info|inf|in|i)?(?:\(\d+\))?$", "", stem)
        stem2 = re.sub(r"\(\d+\)$", "", stem2)
        inner_ext = stem2.rsplit(".", 1)[-1] if "." in stem2 else ""
        if inner_ext in _SIDECAR_INNER_EXTS:
            return True
    # truncated "-info" sidecar tail (covers -.json / -in.json / -inf.json / -info(N).json)
    if _TRUNC_INFO_RE.search(low):
        return True
    return False


def main():
    removed = 0
    removed_bytes = 0
    kept_standalone = []
    samples = []
    for root, dirs, files in os.walk(TARGET):
        fileset = set(files)
        # precompute basenames present (without extension) for sibling matching
        base_no_ext = {}
        for f in files:
            b = os.path.splitext(f)[0].lower()
            base_no_ext.setdefault(b, 0)
            base_no_ext[b] += 1
        for f in files:
            low = f.lower()
            is_clutter = False
            if is_metadata_by_name(low):
                is_clutter = True
            elif low.endswith(".json"):
                # folder-context: does a sibling non-json file share this base?
                base = low[:-5]  # strip .json
                # normalize base for fuzzy match (Takeout truncates names differently
                # for the .json vs the real file, e.g. "Transactio" vs "Transactions (")
                base_prefix = base.rstrip("( ")[:20]
                for other in files:
                    if other == f:
                        continue
                    ol = other.lower()
                    if not ol.endswith(".json") and (
                        ol == base or ol.startswith(base) or
                        (len(base_prefix) >= 12 and ol.startswith(base_prefix))
                    ):
                        is_clutter = True
                        break
                # content-based: tiny json full of Takeout metadata keys = sidecar
                if not is_clutter:
                    full = os.path.join(root, f)
                    try:
                        if os.path.getsize(full) < 12000:
                            with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                                head = fh.read(600)
                            meta_keys = ('"permissions"', '"content_last_modified"',
                                         '"last_modified_by_any_user"', '"drive_id"',
                                         '"mime_type"', '"download_url"')
                            if any(k in head for k in meta_keys):
                                is_clutter = True
                    except OSError:
                        pass
            if is_clutter:
                full = os.path.join(root, f)
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    sz = 0
                if len(samples) < 30:
                    samples.append(full[len(TARGET):])
                if DELETE:
                    try:
                        os.remove(full)
                    except OSError:
                        continue
                removed += 1
                removed_bytes += sz
            elif low.endswith(".json"):
                if len(kept_standalone) < 30:
                    kept_standalone.append(os.path.join(root, f)[len(TARGET):])

    mode = "DELETED" if DELETE else "WOULD DELETE (dry run)"
    print(f"{mode}: {removed} clutter files  ({round(removed_bytes/1024/1024,1)} MB)")
    print(f"KEPT standalone .json (no sidecar match): {len(kept_standalone)}")
    print("\n--- sample clutter (first 30) ---")
    for s in samples:
        print("  " + s)
    if kept_standalone:
        print("\n--- sample KEPT standalone .json (first 30) — verify these are real data ---")
        for s in kept_standalone:
            print("  " + s)


if __name__ == "__main__":
    main()

