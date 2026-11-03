# Start the native Windows Mosquitto broker with the project's config (no Docker).
#
# Install once:  winget install EclipseFoundation.Mosquitto
# Run:           powershell -ExecutionPolicy Bypass -File scripts\mosquitto.ps1
#
# The installer also registers a "mosquitto" Windows service (default config:
# localhost:1883, anonymous, no persistence). If that service already listens on
# 1883 the assistant can use it as is, and this script just says so.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $env:ProgramFiles "mosquitto\mosquitto.exe"
$conf = Join-Path $root "config\mosquitto\mosquitto-windows.conf"

if (-not (Test-Path $exe)) {
    Write-Host "Mosquitto not found: $exe"
    Write-Host "Install it: winget install EclipseFoundation.Mosquitto"
    exit 1
}

$busy = Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = (Get-Process -Id $busy[0].OwningProcess).ProcessName
    Write-Host "Port 1883 is already in use by '$owner' - a broker is running, nothing to do."
    exit 0
}

New-Item -ItemType Directory -Force (Join-Path $root "data\mosquitto") | Out-Null
Set-Location $root
Write-Host "Mosquitto on 127.0.0.1:1883 (Ctrl+C to stop)"
& $exe -c $conf
