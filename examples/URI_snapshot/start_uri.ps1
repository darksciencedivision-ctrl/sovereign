$ErrorActionPreference = 'Stop'
$CanonicalRoot = 'E:\SOVEREIGN\URI'

Write-Host 'SOVEREIGN URI canonical launch'
Write-Host "Root : $CanonicalRoot"

if (-not (Test-Path $CanonicalRoot)) {
    throw "Canonical root not found: $CanonicalRoot"
}

Push-Location $CanonicalRoot
try {
    if (-not (Test-Path '.\app.py')) {
        throw 'app.py not found in canonical root.'
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & py -3 .\app.py
    }
    else {
        & python .\app.py
    }
}
finally {
    Pop-Location
}
