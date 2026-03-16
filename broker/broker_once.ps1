# broker_once.ps1 — SOVEREIGN v3 helper
# Purpose: run ONE broker session and exit (for automation).
# Uses the existing broker.ps1 logic by launching it in a child PowerShell,
# feeding a topic via inbox/topic.txt, then sending STOP after synthesis appears.

param(
    [Parameter(Mandatory=$true)][string]$BrokerPath,
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Topic,
    [int]$TimeoutSec = 240,
    [int]$PollMs = 250
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Inbox = Join-Path $Root "inbox"
$Logs  = Join-Path $Root "logs"
$TopicFile = Join-Path $Inbox "topic.txt"
$StopFile  = Join-Path $Root "STOP"
$SynthFile = Join-Path $Logs "synthesis.txt"

foreach ($p in @($Inbox, $Logs)) { if (!(Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null } }

# Ensure STOP is clear
Remove-Item -Force $StopFile -ErrorAction SilentlyContinue

# Start broker as background process
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "powershell.exe"
$psi.Arguments = "-ExecutionPolicy Bypass -File `"$BrokerPath`" -Root `"$Root`""
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true

$p = New-Object System.Diagnostics.Process
$p.StartInfo = $psi
[void]$p.Start()

# Feed topic
$Topic | Out-File -Encoding utf8 -FilePath $TopicFile

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$sawSynth = $false

while ((Get-Date) -lt $deadline) {
    if (Test-Path $SynthFile) {
        $len = (Get-Item $SynthFile).Length
        if ($len -gt 0) { $sawSynth = $true; break }
    }
    Start-Sleep -Milliseconds $PollMs
}

# Stop broker cleanly
New-Item -ItemType File -Force $StopFile | Out-Null

# Give it a moment to exit
Start-Sleep -Milliseconds 600
if (-not $p.HasExited) { try { $p.Kill() } catch {} }

if (-not $sawSynth) {
    throw "broker_once timeout: synthesis not written to $SynthFile within $TimeoutSec sec"
}

Write-Host ("OK: synthesis written: " + $SynthFile)
