# Move files sorted by sort-downloads.ps1 back to where they came from.
# Usage: undo-sort.ps1 -Since "2026-11-17 09:00"   (or -Last 5 for the last 5 moves)
param([datetime]$Since, [int]$Last = 0, [switch]$DryRun)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$rows = Get-ChildItem (Join-Path $here 'logs') -Filter 'sort_log_*.csv' | Sort-Object Name |
        ForEach-Object { Import-Csv $_.FullName -Encoding UTF8 } | Where-Object { $_.Note -notlike 'ERROR*' }
if ($Since) { $rows = $rows | Where-Object { [datetime]$_.Time -ge $Since } }
if ($Last -gt 0) { $rows = $rows | Select-Object -Last $Last }
if (-not $Since -and $Last -eq 0) { Write-Output 'Specify -Since or -Last.'; return }

foreach ($r in ($rows | Sort-Object Time -Descending)) {
    if (-not (Test-Path -LiteralPath $r.To))   { Write-Output "missing: $($r.To)"; continue }
    if (Test-Path -LiteralPath $r.From)        { Write-Output "exists, skipped: $($r.From)"; continue }
    if ($DryRun) { Write-Output "[DRY] $($r.To) -> $($r.From)"; continue }
    $parent = Split-Path -Parent $r.From
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Move-Item -LiteralPath $r.To -Destination $r.From
    Write-Output "restored: $($r.From)"
}
