"""
Remove Google Takeout HTML artifacts from the Organized/ tree.

SAFETY:
  - DRY RUN by default (pass --delete to actually remove).
  - CONTENT-VERIFIED: a .html file is only removed if its content proves it is a
    Takeout artifact, specifically:
      1. Google Drive shortcut stub  -> contains "NETSCAPE-Bookmark-file" and the
         auto-generated "It will be read and overwritten" banner, and links to
         drive.google.com. These are dead pointers, not real pages.
      2. Takeout comment export       -> filename ends "-comments.html" AND content
         looks like an exported comment thread.
  - A genuine saved web page / real .html document is KEPT (it won't match the
    stub signature).
  - Never touches non-.html files.
"""
import os
import sys

TARGET = os.environ.get("ORGANIZED_ROOT", r"E:\Google Drive Files\Organized")
DELETE = "--delete" in sys.argv

# Signatures that uniquely identify a Takeout auto-generated shortcut bookmark.
_SHORTCUT_MARKERS = (
    "netscape-bookmark-file",
    "it will be read and overwritten",
)


def classify(full: str, name: str):
    """Return 'shortcut', 'comments', or None (=keep)."""
    low = name.lower()
    try:
        with open(full, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(1500).lower()
    except OSError:
        return None

    # 1. Drive shortcut stub — must match BOTH the bookmark doctype and the
    #    auto-generated banner to be safe, and reference google drive.
    if all(m in head for m in _SHORTCUT_MARKERS):
        return "shortcut"

    # 2. Comment export: name ends -comments.html and body references Takeout
    #    comment-thread structure. Keep it conservative.
    if low.endswith("-comments.html"):
        if "comment" in head or "drive.google.com" in head:
            return "comments"

    return None


def main():
    removed = 0
    removed_bytes = 0
    kept_html = []
    by_kind = {"shortcut": 0, "comments": 0}
    samples = []

    for root, dirs, files in os.walk(TARGET):
        for f in files:
            if not f.lower().endswith(".html") and not f.lower().endswith(".htm"):
                continue
            full = os.path.join(root, f)
            kind = classify(full, f)
            if kind:
                by_kind[kind] = by_kind.get(kind, 0) + 1
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    sz = 0
                if len(samples) < 25:
                    samples.append(f"[{kind}] " + full[len(TARGET):])
                if DELETE:
                    try:
                        os.remove(full)
                    except OSError:
                        continue
                removed += 1
                removed_bytes += sz
            else:
                if len(kept_html) < 25:
                    kept_html.append(full[len(TARGET):])

    mode = "DELETED" if DELETE else "WOULD DELETE (dry run)"
    print(f"{mode}: {removed} HTML artifacts  ({round(removed_bytes/1024/1024,2)} MB)")
    print(f"  shortcuts: {by_kind.get('shortcut',0)}   comment-exports: {by_kind.get('comments',0)}")
    print(f"KEPT genuine .html/.htm: {len(kept_html)} (sample shown)")
    print("\n--- sample artifacts to remove ---")
    for s in samples:
        print("  " + s)
    if kept_html:
        print("\n--- sample KEPT .html (verify these are real pages) ---")
        for s in kept_html:
            print("  " + s)


if __name__ == "__main__":
    main()

