# File Deduplicate Analyzer - Startup Script (Windows PowerShell)
# This script starts both the backend and frontend servers

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " File Deduplicate Analyzer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "ERROR: Python is not installed or not in PATH" -ForegroundColor Red
    exit 1
}

# Check Node.js
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Host "ERROR: Node.js is not installed or not in PATH" -ForegroundColor Red
    exit 1
}

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Setup Backend
Write-Host "[1/4] Setting up Python virtual environment..." -ForegroundColor Yellow
$venvPath = Join-Path $rootDir "backend\venv"
if (-not (Test-Path $venvPath)) {
    python -m venv "$venvPath"
}

$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
. $activateScript

Write-Host "[2/4] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r "$rootDir\backend\requirements.txt" --quiet

# Setup Frontend
Write-Host "[3/4] Installing frontend dependencies..." -ForegroundColor Yellow
Set-Location "$rootDir\frontend"
npm install --silent 2>$null

Write-Host "[4/4] Starting servers..." -ForegroundColor Yellow
Write-Host ""

# Start backend in background
$backendJob = Start-Job -ScriptBlock {
    param($rootDir, $venvPath)
    $activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
    . $activateScript
    Set-Location "$rootDir\backend"
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
} -ArgumentList $rootDir, $venvPath

# Start frontend in background
$frontendJob = Start-Job -ScriptBlock {
    param($rootDir)
    Set-Location "$rootDir\frontend"
    npm run dev
} -ArgumentList $rootDir

Write-Host "Backend:  http://localhost:8000" -ForegroundColor Green
Write-Host "Frontend: http://localhost:3000" -ForegroundColor Green
Write-Host "API Docs: http://localhost:8000/docs" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop both servers" -ForegroundColor Gray
Write-Host ""

try {
    while ($true) {
        # Stream output from both jobs
        Receive-Job $backendJob -ErrorAction SilentlyContinue
        Receive-Job $frontendJob -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
}
finally {
    Write-Host "`nShutting down..." -ForegroundColor Yellow
    Stop-Job $backendJob -ErrorAction SilentlyContinue
    Stop-Job $frontendJob -ErrorAction SilentlyContinue
    Remove-Job $backendJob -ErrorAction SilentlyContinue
    Remove-Job $frontendJob -ErrorAction SilentlyContinue
    Write-Host "Done." -ForegroundColor Green
}
