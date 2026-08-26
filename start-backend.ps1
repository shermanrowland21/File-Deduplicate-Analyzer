# Start Backend Only
$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $rootDir "backend\venv"

# Find Python executable
$pythonCmd = $null
foreach ($cmd in @("python", "python3", "py")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found -and (& $cmd --version 2>$null)) {
        $pythonCmd = $cmd
        break
    }
}
if (-not $pythonCmd) {
    Write-Host "ERROR: Python 3.11+ is required but not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}
Write-Host "Using: $pythonCmd" -ForegroundColor Gray

if (-not (Test-Path $venvPath)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    & $pythonCmd -m venv "$venvPath"
}

$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
. $activateScript

Write-Host "Installing dependencies..." -ForegroundColor Yellow
& "$venvPath\Scripts\pip.exe" install -r "$rootDir\backend\requirements.txt" --quiet

Write-Host ""
Write-Host "Starting backend at http://localhost:8000" -ForegroundColor Green
Write-Host "API docs at http://localhost:8000/docs" -ForegroundColor Green
Write-Host ""

Set-Location "$rootDir\backend"
& "$venvPath\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
