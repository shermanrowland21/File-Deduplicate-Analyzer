# Launch both MD5 supervisors as DETACHED background processes that survive
# Kiro closing/crashing. Each supervisor already auto-restarts its own job and
# resumes (path+size+mtime), so this makes the whole thing fire-and-forget
# overnight. Logs go to detached_*.log.
$backend = "C:\Users\sherm\Documents\GitHub\File-Deduplicate-Analyzer\backend"
$py = Join-Path $backend "venv\Scripts\python.exe"

$jobs = @(
  @{ root = "E:\Google Drive Files\Organized"; log = "detached_organized.log" },
  @{ root = "E:\Dropbox-Snapshot";             log = "detached_snapshot.log" }
)

foreach ($j in $jobs) {
  $logPath = Join-Path $backend $j.log
# Quote the root so paths with spaces (E:\Google Drive Files\Organized) stay one arg
  $argLine = 'md5_supervisor.py "{0}"' -f $j.root
  $p = Start-Process -FilePath $py -ArgumentList $argLine -WorkingDirectory $backend `
        -WindowStyle Hidden -RedirectStandardOutput $logPath `
        -RedirectStandardError ($logPath + ".err") -PassThru
  Write-Output "launched detached supervisor PID $($p.Id) for $($j.root) -> $($j.log)"
}
