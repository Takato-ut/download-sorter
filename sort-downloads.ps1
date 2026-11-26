# Download auto-sorter. Rules live in rules.json (same folder). Unmatched files stay in Downloads.
# Usage: sort-downloads.ps1 [-DryRun] [-Recheck]   (-Recheck: re-match unmatched translations/supplements now)
param([switch]$DryRun, [switch]$Recheck)

$ErrorActionPreference = 'Stop'
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfgPath = Join-Path $here 'rules.json'
if (-not (Test-Path -LiteralPath $cfgPath)) { throw "rules.json not found. Copy rules.example.json to rules.json and edit it." }
$cfg     = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$src     = [Environment]::ExpandEnvironmentVariables($cfg.source)
$docRoot = [Environment]::ExpandEnvironmentVariables($cfg.documentsRoot)
$logDir  = Join-Path $here 'logs'
$logFile = Join-Path $logDir ('sort_log_{0}.csv' -f (Get-Date -Format 'yyyyMM'))
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Write-SortLog($from, $to, $rule, $note) {
    $row = [pscustomobject]@{ Time = (Get-Date -Format 's'); From = $from; To = $to; Rule = $rule; Note = $note }
    if ($DryRun) { Write-Output ("[DRY] {0} -> {1}  ({2}) {3}" -f $from, $to, $rule, $note); return }
    $row | Export-Csv -Path $logFile -Append -NoTypeInformation -Encoding UTF8
}

function Test-Locked($path) {
    try { $s = [IO.File]::Open($path, 'Open', 'ReadWrite', 'None'); $s.Close(); return $false } catch { return $true }
}

function Get-FreeName($dir, $name) {
    $base = [IO.Path]::GetFileNameWithoutExtension($name); $ext = [IO.Path]::GetExtension($name)
    $i = 2
    do { $cand = Join-Path $dir ("{0} ({1}){2}" -f $base, $i, $ext); $i++ } while (Test-Path -LiteralPath $cand)
    return $cand
}

$cutoff = (Get-Date).AddMinutes(-[int]$cfg.minAgeMinutes)
$pr = $cfg.papers
$py = if ($pr) { [Environment]::ExpandEnvironmentVariables($pr.python) } else { $null }
$usePy = $pr -and $pr.enabled -and $py -and (Test-Path -LiteralPath $py)
$paperTool = Join-Path $here 'paper_tool.py'
$paperRoot = if ($pr) { Join-Path $docRoot $pr.paperRoot } else { $null }

# Ask paper_tool.py where a paper file belongs. Returns parsed JSON or $null.
function Invoke-Classify($path, $kind, $minCount) {
    $a = @('-I', $paperTool, 'classify', $path, $kind, $paperRoot, "$minCount")
    if ($DryRun) { $a += 'dry' }
    $prev = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8
    try { $out = & $py @a 2>$null } finally { [Console]::OutputEncoding = $prev }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    try { return ($out | Select-Object -Last 1 | ConvertFrom-Json) } catch { return $null }
}

# ---- Extracted-zip detection
# A zip counts as extracted when >= extractedZip.minRatio of its files exist with the same size
# under a folder named like the zip (or like its top-level folder) in Downloads or Documents.
$zc = $cfg.extractedZip
$script:dirIndex = $null
function Get-DirIndex {
    if ($null -eq $script:dirIndex) {
        $script:dirIndex = @{}
        foreach ($root in @($src, $docRoot)) {
            foreach ($d in Get-ChildItem -LiteralPath $root -Directory -Recurse -ErrorAction SilentlyContinue) {
                $k = $d.Name.ToLowerInvariant()
                if (-not $script:dirIndex.ContainsKey($k)) { $script:dirIndex[$k] = New-Object System.Collections.Generic.List[string] }
                $script:dirIndex[$k].Add($d.FullName)
            }
        }
    }
    return $script:dirIndex
}

function Test-ZipExtracted($zipPath) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try { $a = [IO.Compression.ZipFile]::OpenRead($zipPath) } catch { return $null }
    try {
        $entries = @($a.Entries | Where-Object { $_.Name } | ForEach-Object { [pscustomobject]@{ Path = $_.FullName.Replace('/', '\'); Size = $_.Length } })
    } finally { $a.Dispose() }
    if ($entries.Count -eq 0) { return $null }
    $stem = [IO.Path]::GetFileNameWithoutExtension($zipPath) -replace '\s*\(\d+\)$', ''
    $tops = @($entries | ForEach-Object { ($_.Path -split '\\')[0] } | Sort-Object -Unique)
    $names = @($stem) + @($tops | Where-Object { $entries.Count -gt 1 })
    $idx = Get-DirIndex
    $cands = New-Object System.Collections.Generic.List[string]
    foreach ($n in $names) { $k = $n.ToLowerInvariant(); if ($idx.ContainsKey($k)) { $idx[$k] | ForEach-Object { $cands.Add($_) } } }
    foreach ($c in @($cands | Sort-Object -Unique)) {
        # entries may be relative to the candidate itself, to its parent (Extract All keeps the top folder), or nested one level
        foreach ($root in @($c, (Split-Path -Parent $c))) {
            $hit = 0
            foreach ($e in $entries) {
                $p = Join-Path $root $e.Path
                if ((Test-Path -LiteralPath $p -PathType Leaf) -and ((Get-Item -LiteralPath $p).Length -eq $e.Size)) { $hit++ }
            }
            if ($hit / $entries.Count -ge [double]$zc.minRatio) { return [pscustomobject]@{ Folder = $c; Hit = $hit; Total = $entries.Count } }
        }
    }
    return $null
}

# Main papers first, so translations/supplements arriving in the same run find their paper folder.
$kindOrder = @{ 'main' = 0; 'trans' = 1; 'supp' = 1 }
$files = Get-ChildItem -LiteralPath $src -File | Sort-Object {
    $n = $_.Name; $r = $cfg.rules | Where-Object { $n -match $_.pattern } | Select-Object -First 1
    if ($r -and $r.paperKind -and $kindOrder.ContainsKey($r.paperKind)) { $kindOrder[$r.paperKind] } elseif ($r) { 0 } else { 0 }
}, Name

foreach ($f in $files) {
    $name = $f.Name
    if ($cfg.skipPatterns | Where-Object { $name -match $_ }) { continue }
    # Use the later of creation/write time: browsers set creation time at download.
    $t = @($f.CreationTime, $f.LastWriteTime) | Sort-Object -Descending | Select-Object -First 1
    if ($t -gt $cutoff) { continue }

    if ($zc -and $zc.enabled -and $f.Extension -ieq '.zip' -and -not (Test-Locked $f.FullName)) {
        $ex = Test-ZipExtracted $f.FullName
        if ($ex) {
            $destDir = Join-Path $docRoot $zc.dest
            $dest = Join-Path $destDir $name
            if (Test-Path -LiteralPath $dest) { $dest = Get-FreeName $destDir $name }
            try {
                if (-not $DryRun) {
                    if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
                    Move-Item -LiteralPath $f.FullName -Destination $dest
                }
                Write-SortLog $f.FullName $dest $zc.ruleName ("extracted to $($ex.Folder) ($($ex.Hit)/$($ex.Total) files match)")
            } catch { Write-SortLog $f.FullName $dest $zc.ruleName ("ERROR: " + $_.Exception.Message) }
            continue
        }
    }

    $rule = $cfg.rules | Where-Object { $name -match $_.pattern } | Select-Object -First 1
    $isPdf = $f.Extension -ieq '.pdf'
    $kind  = if ($rule) { $rule.paperKind } elseif ($isPdf -and $pr.detectUnmatchedPdf) { 'main' } else { $null }
    $tryPaper = $usePy -and $kind
    if (-not $rule -and -not $tryPaper) { continue }
    if (Test-Locked $f.FullName) { continue }

    $ruleName = if ($rule) { $rule.name } else { $pr.ruleName }
    $destDir  = if ($rule) { Join-Path $docRoot $rule.dest } else { $null }
    $note     = ''
    if ($tryPaper) {
        $minCount = if ($rule) { 1 } else { 2 }
        $res = Invoke-Classify $f.FullName $kind $minCount
        if (-not $res -or $res.status -eq 'neterror') { continue }        # tool/Crossref failure: retry next run
        if ($res.status -eq 'ok' -and -not $rule -and ($pr.acceptTypes -notcontains $res.type)) { continue }
        if ($res.status -eq 'ok') {
            if ($res.note -like 'new paper*') { $script:newPaper = $true }
            $destDir = Join-Path $paperRoot $res.folder
            if ($res.name -ne $name) { $note = "renamed from '$name'; " }
            $note += "$($res.note) doi=$($res.doi)"
            $name = $res.name
        } elseif (-not $rule) { continue }                                 # unmatched PDF, not a paper: leave it
        elseif ($kind -eq 'main') {
            # paper not identified: still give it its own folder, named after the file
            $destDir = Join-Path $paperRoot ([IO.Path]::GetFileNameWithoutExtension($name))
            $note = 'paper not identified; kept name'
        } else { $note = 'parent paper not found' }
    }

    $dest = Join-Path $destDir $name
    try {
        if (Test-Path -LiteralPath $dest) {
            if ((Get-FileHash -LiteralPath $dest).Hash -eq (Get-FileHash -LiteralPath $f.FullName).Hash) {
                $destDir = Join-Path $docRoot $cfg.duplicateDest
                $dest    = Join-Path $destDir $name
                $note    = ($note + ' duplicate of existing file').Trim()
                if (Test-Path -LiteralPath $dest) { $dest = Get-FreeName $destDir $name }
            } else {
                $dest = Get-FreeName $destDir $name
                $note = ($note + ' name collision -> numbered').Trim()
            }
        }
        if (-not $DryRun) {
            if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
            Move-Item -LiteralPath $f.FullName -Destination $dest
        }
        Write-SortLog $f.FullName $dest $ruleName $note
    } catch {
        Write-SortLog $f.FullName $dest $ruleName ("ERROR: " + $_.Exception.Message)
    }
}

# ---- A new paper folder may be the home of translations/supplements parked in the unmatched folders earlier.
if ($usePy -and ($script:newPaper -or $Recheck)) {
    $parked = @(
        @{ Dir = Join-Path $docRoot ($cfg.rules | Where-Object { $_.paperKind -eq 'trans' } | Select-Object -First 1).dest; Kind = 'trans' },
        @{ Dir = Join-Path $docRoot ($cfg.rules | Where-Object { $_.paperKind -eq 'supp' }  | Select-Object -First 1).dest; Kind = 'supp' }
    )
    foreach ($p in $parked) {
        if (-not (Test-Path -LiteralPath $p.Dir)) { continue }
        foreach ($f in Get-ChildItem -LiteralPath $p.Dir -File) {
            if (Test-Locked $f.FullName) { continue }
            $res = Invoke-Classify $f.FullName $p.Kind 1
            if (-not $res -or $res.status -ne 'ok') { continue }
            $destDir = Join-Path $paperRoot $res.folder
            $dest = Join-Path $destDir $res.name
            if (Test-Path -LiteralPath $dest) { $dest = Get-FreeName $destDir $res.name }
            try {
                if (-not $DryRun) { Move-Item -LiteralPath $f.FullName -Destination $dest }
                Write-SortLog $f.FullName $dest $cfg.papers.recheckRuleName "$($res.note) doi=$($res.doi)"
            } catch { Write-SortLog $f.FullName $dest $cfg.papers.recheckRuleName ("ERROR: " + $_.Exception.Message) }
        }
    }
}
