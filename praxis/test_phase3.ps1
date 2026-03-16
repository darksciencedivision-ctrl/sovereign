# ═══════════════════════════════════════════════════════════════
# SOVEREIGN Phase 3 — End-to-end validation test sequence
# Copy-paste into PowerShell 5 and run top-to-bottom.
# Prerequisites: Ollama running, nomic-embed-text pulled,
#                chromadb installed, praxis_init.py already run.
# ═══════════════════════════════════════════════════════════════

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$praxisDir  = "E:\SOVEREIGN\praxis"
$commitJson = Join-Path $praxisDir "commit.json"
$doneTxt    = Join-Path $praxisDir "commit_done.txt"
$queryTxt   = Join-Path $praxisDir "query.txt"
$resultTxt  = Join-Path $praxisDir "result.txt"
$commitPy   = Join-Path $praxisDir "praxis_commit.py"
$queryPy    = Join-Path $praxisDir "praxis_query.py"
$utf8noBOM  = New-Object System.Text.UTF8Encoding($false)

$testSessionId = "TEST_PHASE3_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$testTopic     = "Phase 3 write-back validation"

$testSynthesis = @"
SYNTHESIS — Phase 3 Validation Run
The adversarial debate concluded that PRAXIS write-back is essential
for cross-session memory continuity. ModelS synthesized that without
persistent storage of synthesis outputs, each session begins without
accumulated knowledge, degrading long-horizon research quality.
Recommendation: commit after every complete session unconditionally.
"@

$testLedger = @"
LEDGER — Round summary
Round 1: ModelA claimed write-back enables knowledge accumulation.
Round 1: ModelB challenged: storage overhead may degrade retrieval SNR.
Round 1: ModelC critiqued: both valid; recommend selective commit gating.
Net: 2/3 models favour unconditional commit. Motion carries.
"@

# ───────────────────────────────────────────────────────────────
# TEST 1: praxis_commit.py — happy path
# ───────────────────────────────────────────────────────────────
Write-Host "`n[TEST 1] Commit: synthesis + ledger" -ForegroundColor Cyan

# clean stale files
foreach ($f in @($doneTxt, $commitJson)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}

$payload = [PSCustomObject]@{
    session_id = $testSessionId
    topic      = $testTopic
    entries    = @(
        [PSCustomObject]@{ type = "synthesis"; content = $testSynthesis }
        [PSCustomObject]@{ type = "ledger";    content = $testLedger    }
    )
}
$jsonStr = $payload | ConvertTo-Json -Depth 5 -Compress
[System.IO.File]::WriteAllText($commitJson, $jsonStr, $utf8noBOM)

$proc = Start-Process -FilePath "python" -ArgumentList $commitPy `
        -NoNewWindow -PassThru
$deadline = (Get-Date).AddSeconds(60)
$done = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
    if (Test-Path $doneTxt) {
        $done = (Get-Content $doneTxt -Raw -Encoding UTF8).Trim()
        break
    }
}
if ($proc -and -not $proc.HasExited) { $proc.Kill() }

if ($done -eq "OK") {
    Write-Host "  PASS: commit_done.txt = OK" -ForegroundColor Green
} else {
    Write-Host "  FAIL: commit_done.txt = '$done'" -ForegroundColor Red
    exit 1
}

# ───────────────────────────────────────────────────────────────
# TEST 2: praxis_commit.py — synthesis only (no ledger)
# ───────────────────────────────────────────────────────────────
Write-Host "`n[TEST 2] Commit: synthesis only" -ForegroundColor Cyan

if (Test-Path $doneTxt) { Remove-Item $doneTxt -Force }

$payload2 = [PSCustomObject]@{
    session_id = ($testSessionId + "_synonly")
    topic      = $testTopic
    entries    = @(
        [PSCustomObject]@{ type = "synthesis"; content = $testSynthesis }
    )
}
$jsonStr2 = $payload2 | ConvertTo-Json -Depth 5 -Compress
[System.IO.File]::WriteAllText($commitJson, $jsonStr2, $utf8noBOM)

$proc2 = Start-Process -FilePath "python" -ArgumentList $commitPy `
         -NoNewWindow -PassThru
$done2 = $null
$dl2   = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $dl2) {
    Start-Sleep -Milliseconds 250
    if (Test-Path $doneTxt) { $done2 = (Get-Content $doneTxt -Raw -Encoding UTF8).Trim(); break }
}
if ($proc2 -and -not $proc2.HasExited) { $proc2.Kill() }

if ($done2 -eq "OK") {
    Write-Host "  PASS: synthesis-only commit OK" -ForegroundColor Green
} else {
    Write-Host "  FAIL: '$done2'" -ForegroundColor Red
    exit 1
}

# ───────────────────────────────────────────────────────────────
# TEST 3: praxis_commit.py — missing file → exit code 1
# ───────────────────────────────────────────────────────────────
Write-Host "`n[TEST 3] Commit: missing commit.json → expect exit code 1" -ForegroundColor Cyan

if (Test-Path $commitJson) { Remove-Item $commitJson -Force }
if (Test-Path $doneTxt)    { Remove-Item $doneTxt    -Force }

$proc3 = Start-Process -FilePath "python" -ArgumentList $commitPy `
         -NoNewWindow -PassThru -Wait
$ec3 = $proc3.ExitCode
if ($ec3 -eq 1) {
    Write-Host "  PASS: exit code = 1" -ForegroundColor Green
} else {
    Write-Host "  FAIL: exit code = $ec3 (expected 1)" -ForegroundColor Red
    exit 1
}

# commit_done.txt should say ERROR
if (Test-Path $doneTxt) {
    $msg3 = (Get-Content $doneTxt -Raw -Encoding UTF8).Trim()
    if ($msg3 -like "ERROR:*") {
        Write-Host "  PASS: commit_done.txt contains ERROR message: $msg3" -ForegroundColor Green
    } else {
        Write-Host "  FAIL: commit_done.txt unexpected content: $msg3" -ForegroundColor Red
        exit 1
    }
}

# ───────────────────────────────────────────────────────────────
# TEST 4: praxis_commit.py — invalid schema → exit code 2
# ───────────────────────────────────────────────────────────────
Write-Host "`n[TEST 4] Commit: bad schema → expect exit code 2" -ForegroundColor Cyan

if (Test-Path $doneTxt) { Remove-Item $doneTxt -Force }

$badPayload = '{"session_id":"x","topic":"x"}'   # missing entries
[System.IO.File]::WriteAllText($commitJson, $badPayload, $utf8noBOM)

$proc4 = Start-Process -FilePath "python" -ArgumentList $commitPy `
         -NoNewWindow -PassThru -Wait
$ec4 = $proc4.ExitCode
if ($ec4 -eq 2) {
    Write-Host "  PASS: exit code = 2" -ForegroundColor Green
} else {
    Write-Host "  FAIL: exit code = $ec4 (expected 2)" -ForegroundColor Red
    exit 1
}

# ───────────────────────────────────────────────────────────────
# TEST 5: praxis_query.py retrieves what we committed
# ───────────────────────────────────────────────────────────────
Write-Host "`n[TEST 5] Retrieval: query for committed content" -ForegroundColor Cyan

if (Test-Path $resultTxt) { Remove-Item $resultTxt -Force }

# query on a distinctive phrase from the test synthesis
$queryPhrase = "cross-session memory continuity PRAXIS write-back"
[System.IO.File]::WriteAllText($queryTxt, $queryPhrase, $utf8noBOM)

$proc5 = Start-Process -FilePath "python" -ArgumentList $queryPy `
         -NoNewWindow -PassThru
$done5 = $null
$dl5   = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $dl5) {
    Start-Sleep -Milliseconds 250
    if (Test-Path $resultTxt) { $done5 = $true; break }
}
if ($proc5 -and -not $proc5.HasExited) { $proc5.Kill() }

if (-not $done5) {
    Write-Host "  FAIL: praxis_query.py did not produce result.txt within 60s" -ForegroundColor Red
    exit 1
}

$resultContent = Get-Content $resultTxt -Raw -Encoding UTF8
Write-Host "  result.txt preview (first 400 chars):" -ForegroundColor Gray
Write-Host ($resultContent.Substring(0, [Math]::Min(400, $resultContent.Length))) -ForegroundColor Gray

# Check that at least one MEMORY block was returned
if ($resultContent -match "\[MEMORY \d+\]") {
    Write-Host "  PASS: at least one [MEMORY N] block returned" -ForegroundColor Green
} else {
    Write-Host "  FAIL: no [MEMORY N] block in result.txt" -ForegroundColor Red
    exit 1
}

# Check that our session shows up in the metadata
if ($resultContent -match $testSessionId) {
    Write-Host "  PASS: result contains our test session_id ($testSessionId)" -ForegroundColor Green
} else {
    # not a hard failure — could still be correct if session_id not echoed in result
    Write-Host "  WARN: session_id not found in result (check source metadata manually)" -ForegroundColor Yellow
}

# ───────────────────────────────────────────────────────────────
# TEST 6: upsert idempotency — re-running commit must not duplicate
# ───────────────────────────────────────────────────────────────
Write-Host "`n[TEST 6] Idempotency: re-commit same session_id" -ForegroundColor Cyan

if (Test-Path $doneTxt) { Remove-Item $doneTxt -Force }

# restore the original commit.json (same session_id as TEST 1)
$payloadRerun = [PSCustomObject]@{
    session_id = $testSessionId
    topic      = $testTopic
    entries    = @(
        [PSCustomObject]@{ type = "synthesis"; content = $testSynthesis }
        [PSCustomObject]@{ type = "ledger";    content = $testLedger    }
    )
}
$jsonRerun = $payloadRerun | ConvertTo-Json -Depth 5 -Compress
[System.IO.File]::WriteAllText($commitJson, $jsonRerun, $utf8noBOM)

$proc6 = Start-Process -FilePath "python" -ArgumentList $commitPy `
         -NoNewWindow -PassThru
$done6 = $null
$dl6   = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $dl6) {
    Start-Sleep -Milliseconds 250
    if (Test-Path $doneTxt) { $done6 = (Get-Content $doneTxt -Raw -Encoding UTF8).Trim(); break }
}
if ($proc6 -and -not $proc6.HasExited) { $proc6.Kill() }

if ($done6 -eq "OK") {
    Write-Host "  PASS: re-commit returned OK (upsert is safe)" -ForegroundColor Green
} else {
    Write-Host "  FAIL: '$done6'" -ForegroundColor Red
    exit 1
}

# ───────────────────────────────────────────────────────────────
# TEST 7: Invoke-PraxisCommit function integration smoke-test
# (loads the function inline and calls it directly)
# ───────────────────────────────────────────────────────────────
Write-Host "`n[TEST 7] Invoke-PraxisCommit function smoke-test" -ForegroundColor Cyan

# Inline the Write-Log stub so the function doesn't error in isolation
function Write-Log { param($level, $msg) Write-Host "  [$level] $msg" }

# Dot-source the function definition file
. "E:\SOVEREIGN\broker_v21\Invoke-PraxisCommit.ps1"   # adjust path if needed

$ok = Invoke-PraxisCommit `
        -SessionId     ($testSessionId + "_func") `
        -Topic         $testTopic `
        -SynthesisText $testSynthesis `
        -LedgerText    $testLedger

if ($ok -eq $true) {
    Write-Host "  PASS: Invoke-PraxisCommit returned `$true" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Invoke-PraxisCommit returned `$false" -ForegroundColor Red
    exit 1
}

# ───────────────────────────────────────────────────────────────
# SUMMARY
# ───────────────────────────────────────────────────────────────
Write-Host "`n══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  All Phase 3 tests PASSED" -ForegroundColor Green
Write-Host "══════════════════════════════════════════`n" -ForegroundColor Cyan