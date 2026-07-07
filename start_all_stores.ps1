<#
.SYNOPSIS
    Start all configured stores in separate processes.

.DESCRIPTION
    Reads config/accounts.yaml, then launches each enabled store in its own
    PowerShell window. Each store gets a unique health-check port starting
    from 8899. If one store crashes or needs a restart, the others keep running.

.EXAMPLE
    .\start_all_stores.ps1
#>
param(
    [int]$BasePort = 8899
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $ScriptDir "config\accounts.yaml"

if (-not (Test-Path $ConfigFile)) {
    Write-Host "ERROR: Config file not found: $ConfigFile" -ForegroundColor Red
    exit 1
}

# Read store names from YAML using Python (always available in this project)
$storeIds = python -c @"
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

if ($LASTEXITCODE -ne 0 -or -not $storeIds) {
    Write-Host "ERROR: Failed to read store configs or no stores found." -ForegroundColor Red
    Write-Host $storeIds
    exit 1
}

$index = 0
$storeIds | ForEach-Object {
    $parts = $_ -split '\|'
    $storeId = $parts[0]
    $storeName = $parts[1]
    $port = $BasePort + $index

    Write-Host "Starting: $storeName ($storeId) on port $port" -ForegroundColor Green

    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "cd '$ScriptDir'; python main.py --store $storeId --port $port"
    )

    $index++
    Start-Sleep -Seconds 2  # Stagger to avoid port conflicts
}

Write-Host ""
Write-Host "All $index store(s) launched." -ForegroundColor Cyan
Write-Host "Health endpoints:"
$index = 0
$storeIds | ForEach-Object {
    $parts = $_ -split '\|'
    $port = $BasePort + $index
    Write-Host "  http://localhost:$port" -ForegroundColor DarkGray
    $index++
}
