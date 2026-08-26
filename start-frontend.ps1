# Start Frontend Only
$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Set-Location "$rootDir\frontend"

if (-not (Test-Path "node_modules")) {
    Write-Host "Installing dependencies..." -ForegroundColor Yellow
    npm install
}

Write-Host ""
Write-Host "Starting frontend at http://localhost:3000" -ForegroundColor Green
Write-Host ""

npm run dev
