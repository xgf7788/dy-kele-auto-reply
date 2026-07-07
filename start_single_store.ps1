<#
.SYNOPSIS
    Interactive store picker — choose one store to run.

.DESCRIPTION
    Reads config/accounts.yaml, shows a numbered list of enabled stores,
    and lets you pick one to launch. Useful when you want to restart just
    one store or test a specific store in isolation.

.EXAMPLE
    .\start_single_store.ps1
    .\start_single_store.ps1 -Port 8900
#>
param(
    [int]$Port = 8899
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $ScriptDir "config\accounts.yaml"

if (-not (Test-Path $ConfigFile)) {
    Write-Host "ERROR: Config file not found: $ConfigFile" -ForegroundColor Red
    exit 1
}

# Read store list from YAML
$storeList = python -c @"
import yaml, sys
try:
    with open(r'$ConfigFile', 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    stores = data.get('stores', []) if data else []
    for s in stores:
        if s.get('enabled', True):
            print(f"{s['store_id']}|{s.get('name', s['store_id'])}")
except Exception as e:
    print(f'ERROR:{e}', file=sys.stderr)
    sys.exit(1)
"@

if ($LASTEXITCODE -ne 0 -or -not $storeList) {
    Write-Host "ERROR: Failed to read store configs or no enabled stores found." -ForegroundColor Red
    exit 1
}

$stores = @()
$i = 1
Write-Host ""
Write-Host "=== Available Stores ===" -ForegroundColor Cyan
$storeList | ForEach-Object {
    $parts = $_ -split '\|'
    $stores += @{ Id = $parts[0]; Name = $parts[1] }
    Write-Host "  [$i] $($parts[1])  (id: $($parts[0]))" -ForegroundColor White
    $i++
}
Write-Host "  [a] Run ALL stores"
Write-Host "  [q] Quit"
Write-Host ""

$choice = Read-Host "Pick a store number (or a/q)"

if ($choice -eq 'q') {
    Write-Host "Cancelled." -ForegroundColor Yellow
    exit 0
}

if ($choice -eq 'a') {
    Write-Host "Launching all stores..." -ForegroundColor Green
    & (Join-Path $ScriptDir "start_all_stores.ps1")
    exit 0
}

try {
    $idx = [int]$choice - 1
    if ($idx -lt 0 -or $idx -ge $stores.Count) {
        Write-Host "ERROR: Invalid choice. Pick 1-$($stores.Count)." -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "ERROR: Please enter a number." -ForegroundColor Red
    exit 1
}

$store = $stores[$idx]
Write-Host "Starting: $($store.Name) ($($store.Id)) on port $Port" -ForegroundColor Green

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "cd '$ScriptDir'; python main.py --store $($store.Id) --port $Port"
)
