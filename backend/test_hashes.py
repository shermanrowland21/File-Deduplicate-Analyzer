"""Verify compute_hashes matches existing smart_hash for both small and large files,
and that md5 matches hashlib reference. READ-ONLY test with temp files."""
import os, tempfile, hashlib
from app.services.file_scanner import (
    compute_hashes, smart_hash, compute_file_hash, compute_quick_fingerprint,
    compute_md5, LARGE_FILE_THRESHOLD,
)

def make(path, size):
    # deterministic pseudo-random content
    with open(path, "wb") as f:
        written = 0
        seed = b"".join(bytes([(i*7+13) % 256]) for i in range(4096))
        while written < size:
            f.write(seed[:min(len(seed), size-written)])
            written += min(len(seed), size-written)

tmp = tempfile.mkdtemp()
results = []

# small file (1 MB)
small = os.path.join(tmp, "small.bin"); make(small, 1024*1024)
h = compute_hashes(small, os.path.getsize(small))
ref_smart = smart_hash(small, os.path.getsize(small))
ref_md5 = hashlib.md5(open(small,"rb").read()).hexdigest()
results.append(("small smart matches", h["smart"] == ref_smart))
results.append(("small md5 matches", h["md5"] == ref_md5))

# large file (60 MB) -> fingerprint path
large = os.path.join(tmp, "large.bin"); make(large, 60*1024*1024)
sz = os.path.getsize(large)
h2 = compute_hashes(large, sz)
ref_fp = compute_quick_fingerprint(large, sz)
ref_md5_l = compute_md5(large)
results.append(("large smart==fingerprint", h2["smart"] == ref_fp))
results.append(("large md5 matches", h2["md5"] == ref_md5_l))

for name, ok in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
print("large fingerprint (new):", h2["smart"][:16], "ref:", (ref_fp or '')[:16])

import shutil; shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if all(ok for _,ok in results) else "SOME FAILED")
