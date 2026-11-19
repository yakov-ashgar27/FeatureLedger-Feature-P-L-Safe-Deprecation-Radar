# Windows (PowerShell) equivalent of `make sync-github`: pulls a real repository's merged pull requests
# and direct pushes into FeatureLedger, then prints the ledger. No make, curl or Python needed.
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\sync-github.ps1 -Repo owner/name [-Team team-key] [-Wait 30]
#
# Reads FL_API_TOKEN, GITHUB_TOKEN and API_PORT from .env in the repository root.
param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$Team = "",
    [int]$Wait = 30
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) { Write-Host "No .env in $root (copy .env.example to .env first)." -ForegroundColor Red; exit 1 }
function Get-EnvValue($name) {
    $line = Get-Content $envFile | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$name=", "").Trim() } else { return "" }
}
$token = Get-EnvValue "FL_API_TOKEN"
$gh = Get-EnvValue "GITHUB_TOKEN"
$port = Get-EnvValue "API_PORT"; if (-not $port) { $port = "8000" }
$webPort = Get-EnvValue "WEB_PORT"; if (-not $webPort) { $webPort = "3000" }
$api = "http://localhost:$port"
if (-not $token) { Write-Host "Set FL_API_TOKEN in .env (any long random string) and restart: docker compose up -d" -ForegroundColor Red; exit 1 }
if (-not $gh -or $gh -eq "PASTE_YOUR_TOKEN_HERE") { Write-Host "GITHUB_TOKEN is not set in .env yet." -ForegroundColor Red; exit 1 }
$h = @{ Authorization = "Bearer $token" }
try { Invoke-RestMethod "$api/health" | Out-Null }
catch { Write-Host "FeatureLedger is not answering on $api. Run: docker compose up --build -d   (then wait ~1 minute)" -ForegroundColor Red; exit 1 }

$body = @{ repo = $Repo } ; if ($Team) { $body.default_team_key = $Team }
$r = Invoke-RestMethod -Method Post "$api/api/v1/integrations/github/sync" -Headers $h -ContentType "application/json" -Body ($body | ConvertTo-Json)
Write-Host "Sync started for $($r.repo). Waiting $Wait seconds..." -ForegroundColor Cyan
Start-Sleep -Seconds $Wait

$i = (Invoke-RestMethod "$api/api/v1/integrations" -Headers $h).integrations | Where-Object { $_.kind -eq "github" }
Write-Host "GitHub integration: status=$($i.status) last synced=$($i.last_synced_at) error=$($i.last_error)"
try {
    $p = Invoke-RestMethod "$api/api/v1/pnl?window=3" -Headers $h
    $p.features | Select-Object key, classification, @{n = "eng $"; e = { [math]::Round($_.c_eng) } }, @{n = "confidence"; e = { $_.confidence.level } } | Format-Table -AutoSize
}
catch {
    Write-Host "No P&L yet. Either the sync is still running (run this script again in a minute), or the repository" -ForegroundColor Yellow
    Write-Host "has no merged pull requests or direct commits FeatureLedger can count. Logs: docker compose logs api --tail 50" -ForegroundColor Yellow
}
Write-Host "Dashboard: http://localhost:$webPort  (sign in with the 'Admin' button)" -ForegroundColor Green
