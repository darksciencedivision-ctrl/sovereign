param(
    [switch]$IncludeLogs
)

$ErrorActionPreference = 'Stop'
$LegacyRoot = 'E:\URI'
$CanonicalRoot = 'E:\SOVEREIGN\URI'
$CriticalLegacyFiles = @(
    'app.py',
    'system_prompt.txt',
    'templates\index.html'
)
$CopyItems = @(
    'conversations',
    'memory',
    'uploads',
    'artifacts',
    'system_prompt.txt'
)
if ($IncludeLogs) {
    $CopyItems += 'logs'
}

function Test-CriticalSource {
    foreach ($item in $CriticalLegacyFiles) {
        $path = Join-Path $LegacyRoot $item
        if (-not (Test-Path $path)) {
            throw "Critical legacy file missing: $path"
        }
    }
}

function Invoke-SafeRoboCopy {
    param([string]$Source,[string]$Destination)
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    & robocopy $Source $Destination /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XO /FFT /NFL /NDL /NP /NJH /NJS | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Robocopy failed for $Source -> $Destination with exit code $LASTEXITCODE"
    }
}

function Copy-StagedFile {
    param([string]$Source,[string]$Destination)
    $src = Get-Item -LiteralPath $Source
    $copy = $true
    if (Test-Path -LiteralPath $Destination) {
        $dst = Get-Item -LiteralPath $Destination
        $copy = $src.LastWriteTimeUtc -gt $dst.LastWriteTimeUtc -or $src.Length -ne $dst.Length
    }
    if ($copy) {
        $parent = Split-Path -Parent $Destination
        if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
        Write-Host "Copied file: $Source -> $Destination"
    }
    else {
        Write-Host "Kept existing destination file: $Destination"
    }
}

Write-Host 'SOVEREIGN URI staged migration'
Write-Host "Legacy root    : $LegacyRoot"
Write-Host "Canonical root : $CanonicalRoot"

if (-not (Test-Path $LegacyRoot)) {
    throw "Legacy root not found: $LegacyRoot"
}

New-Item -ItemType Directory -Path $CanonicalRoot -Force | Out-Null
Test-CriticalSource

foreach ($item in $CopyItems) {
    $sourcePath = Join-Path $LegacyRoot $item
    if (-not (Test-Path $sourcePath)) {
        Write-Warning "Skipping missing source path: $sourcePath"
        continue
    }

    $destinationPath = Join-Path $CanonicalRoot $item
    if ((Get-Item -LiteralPath $sourcePath) -is [System.IO.DirectoryInfo]) {
        Write-Host "Mirroring directory (non-destructive): $sourcePath -> $destinationPath"
        Invoke-SafeRoboCopy -Source $sourcePath -Destination $destinationPath
    }
    else {
        Copy-StagedFile -Source $sourcePath -Destination $destinationPath
    }
}

$checks = @(
    (Join-Path $CanonicalRoot 'app.py'),
    (Join-Path $CanonicalRoot 'system_prompt.txt'),
    (Join-Path $CanonicalRoot 'templates\index.html')
)

foreach ($path in $checks) {
    if (Test-Path $path) {
        Write-Host "Verified: $path"
    }
    else {
        Write-Warning "Missing after migration: $path"
    }
}

Write-Host 'Migration completed. Legacy root preserved in place.'
