# ═══════════════════════════════════════════════════════════════
# Invoke-PraxisCommit  —  Phase 3 write-back  (drop into broker.ps1)
# Same IPC pattern as Invoke-PraxisQuery.
# Returns $true on success, $false on any failure. Never throws.
# ═══════════════════════════════════════════════════════════════
function Invoke-PraxisCommit {
    param(
        [string]$SessionId,
        [string]$Topic,
        [string]$SynthesisText,
        [string]$LedgerText  = "",      # optional
        [int]   $TimeoutSec  = 60,      # caller-overridable; matches broker Praxis settings
        [string]$PythonExe   = "python" # caller passes $PraxisPython from broker config
    )

    $praxisDir  = "E:\SOVEREIGN\praxis"
    $commitJson = Join-Path $praxisDir "commit.json"
    $doneTxt    = Join-Path $praxisDir "commit_done.txt"
    $scriptPath = Join-Path $praxisDir "praxis_commit.py"
    $pollMs     = 250
    $timeoutSec = $TimeoutSec           # use caller value, not hardcoded 60

    # ── build entries list ─────────────────────────────────────
    $entries = @()

    if (-not [string]::IsNullOrWhiteSpace($SynthesisText)) {
        $entries += [PSCustomObject]@{ type = "synthesis"; content = $SynthesisText }
    }
    if (-not [string]::IsNullOrWhiteSpace($LedgerText)) {
        $entries += [PSCustomObject]@{ type = "ledger"; content = $LedgerText }
    }

    if ($entries.Count -eq 0) {
        Write-Log "WARN" "Invoke-PraxisCommit: nothing to commit (both synthesis and ledger empty)"
        return $false
    }

    # ── build payload ──────────────────────────────────────────
    $payload = [PSCustomObject]@{
        session_id = $SessionId
        topic      = $Topic
        entries    = $entries
    }

    $jsonStr   = $payload | ConvertTo-Json -Depth 5 -Compress
    $utf8noBOM = New-Object System.Text.UTF8Encoding($false)

    # ── clean stale done file ──────────────────────────────────
    if (Test-Path $doneTxt) { Remove-Item $doneTxt -Force }

    # ── write commit.json BOM-free ─────────────────────────────
    try {
        [System.IO.File]::WriteAllText($commitJson, $jsonStr, $utf8noBOM)
    } catch {
        Write-Log "ERROR" "Invoke-PraxisCommit: failed to write commit.json — $_"
        return $false
    }

    # ── spawn praxis_commit.py ─────────────────────────────────
    $proc = $null
    try {
        $proc = Start-Process -FilePath $PythonExe `
                              -ArgumentList $scriptPath `
                              -NoNewWindow `
                              -PassThru
    } catch {
        Write-Log "ERROR" "Invoke-PraxisCommit: failed to spawn praxis_commit.py — $_"
        return $false
    }

    # ── poll commit_done.txt ───────────────────────────────────
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    $result   = $null

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds $pollMs
        if (Test-Path $doneTxt) {
            $result = (Get-Content $doneTxt -Raw -Encoding UTF8).Trim()
            break
        }
    }

    # kill process if still running
    if ($proc -and -not $proc.HasExited) {
        try { $proc.Kill() } catch {}
    }

    # ── evaluate result ────────────────────────────────────────
    if ($null -eq $result) {
        Write-Log "ERROR" "Invoke-PraxisCommit: timed out after ${timeoutSec}s"
        return $false
    }

    if ($result -eq "OK") {
        Write-Log "INFO" "Invoke-PraxisCommit: committed session=$SessionId topic=$Topic"
        return $true
    } else {
        Write-Log "ERROR" "Invoke-PraxisCommit: script reported — $result"
        return $false
    }
}