# One-command demo: broker + simulator + assistant + web panel.
#
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1            # voice + web
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 --text     # keyboard instead of mic
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 --no-voice # web panel only
#
# Ctrl+C stops everything that was started. See scripts/demo.py for details.

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No .venv found. Create it first (README, 'Швидкий старт')."
    exit 1
}
Set-Location $root
& $python (Join-Path $root "scripts\demo.py") @args
exit $LASTEXITCODE
