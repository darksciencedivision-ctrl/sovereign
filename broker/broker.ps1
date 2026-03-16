# broker.ps1 - SOVEREIGN Broker (Phase 9 stabilized)
# Save as: E:\SOVEREIGN\broker_v21\broker.ps1
# Encoding: UTF-8 (no BOM) Ã¢â‚¬â€ use Notepad "Save As" -> UTF-8
#
# Called by cycle_runner_v3.py as:
#   powershell -File broker.ps1 -Root E:\SOVEREIGN [-Once]
#
# Paths derived from -Root (E:\SOVEREIGN):
#   Topic file    : <Root>\broker_v21\inbox\topic.txt
#   Synthesis out : <Root>\praxis\logs\synthesis.txt
#   Dialog log    : <Root>\logs\dialog.txt
#   System log    : <Root>\logs\system.txt
#   STOP files    : <Root>\STOP  and  <Root>\praxis\STOP

param(
    [string]$Root              = "E:\SOVEREIGN",
    [string]$OllamaBaseUrl     = "http://127.0.0.1:11434",
    [string]$ModelA            = "deepseek-r1:8b",
    [string]$ModelB            = "dolphin-llama3:8b",
    [string]$ModelC            = "qwen3:8b",
    [string]$ModelSynth        = "dolphin3:8b",
    [int]$DebateRounds         = 2,
    [int]$MaxTokensPerTurn     = 768,
    [int]$MaxTokensSynth       = 1024,
    [double]$DebateTemp        = 0.7,
    [double]$SynthTemp         = 0.3,
    [int]$Seed                 = 0,
    [int]$TurnTimeoutSec       = 180,
    [int]$SynthTimeoutSec      = 240,
    [switch]$Once
)

# NOTE: -Once is retained only for backward compatibility with existing call sites.
# Broker v21 already runs exactly one session and exits; -Once is effectively a no-op.
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------
# Paths Ã¢â‚¬â€ all derived from $Root = E:\SOVEREIGN
# ------------------------------------------------------------
$RootResolved   = (Resolve-Path $Root).Path
$InboxDir       = Join-Path $RootResolved "broker_v21\inbox"
$LogsDir        = Join-Path $RootResolved "logs"
$PraxisLogsDir  = Join-Path $RootResolved "praxis\logs"
$StopFile       = Join-Path $RootResolved "STOP"
$StopPraxis     = Join-Path $RootResolved "praxis\STOP"
$TopicFile      = Join-Path $InboxDir "topic.txt"
$DialogLog      = Join-Path $LogsDir "dialog.txt"
$SystemLog      = Join-Path $LogsDir "system.txt"
$SynthesisFile  = Join-Path $PraxisLogsDir "synthesis.txt"

# Ensure directories exist
foreach ($dir in @($InboxDir, $LogsDir, $PraxisLogsDir)) {
    New-Item -ItemType Directory -Force $dir | Out-Null
}
foreach ($file in @($DialogLog, $SystemLog)) {
    if (-not (Test-Path $file)) { New-Item -ItemType File -Force $file | Out-Null }
}

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
function Get-UtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss")
}

function Log-System {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-UtcStamp), $Message
    Add-Content -Path $SystemLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Log-Dialog {
    param([string]$Message)
    Add-Content -Path $DialogLog -Value $Message -Encoding UTF8
}

function Test-StopRequested {
    return ((Test-Path $StopFile) -or (Test-Path $StopPraxis))
}

# ------------------------------------------------------------
# Topic / session helpers
# ------------------------------------------------------------
function Get-RawTopicPayload {
    if (-not (Test-Path $TopicFile)) {
        throw "Topic file not found: $TopicFile"
    }
    return (Get-Content $TopicFile -Raw -Encoding UTF8)
}

function Get-SessionIdFromText {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $m = [regex]::Match($Text, '\[TOPIC\s+session_id=([^\]\s]+)\]', 'IgnoreCase')
    if ($m.Success) { return $m.Groups[1].Value.Trim() }
    $m = [regex]::Match($Text, '\[SYNTH\s+session_id=([^\]\s]+)\]', 'IgnoreCase')
    if ($m.Success) { return $m.Groups[1].Value.Trim() }
    return $null
}

function Get-CleanTopic {
    param([string]$RawText)
    $text = $RawText -replace '\r\n', "`n"
    $text = $text -replace '(?is)\[TOPIC[^\]]*\]', ''
    $text = $text -replace '(?is)\[/TOPIC\]', ''
    return $text.Trim()
}

function New-SessionId {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $short = [guid]::NewGuid().ToString("N").Substring(0,8)
    return "{0}_{1}" -f $stamp, $short
}

# ------------------------------------------------------------
# Text helpers
# ------------------------------------------------------------
function Normalize-Text {
    param([string]$Text)
    if ($null -eq $Text) { return "" }
    return ($Text -replace '\r\n', "`n" -replace '\r', "`n").Trim()
}

function Extract-Section {
    param(
        [string]$Text,
        [string[]]$Names
    )
    $body = Normalize-Text $Text
    foreach ($name in $Names) {
        # Tagged block: [SECTION]...[/SECTION]
        $m = [regex]::Match($body, "(?is)\[$name\](.*?)\[/$name\]")
        if ($m.Success) { return (Normalize-Text $m.Groups[1].Value) }

        # Loose header: SECTION: ... (until next all-caps header or end)
        $m = [regex]::Match($body, "(?ims)^\s*$name\s*:\s*(.*?)(?=^\s*[A-Z_][A-Z_ ]{2,29}\s*:|\Z)")
        if ($m.Success) { return (Normalize-Text $m.Groups[1].Value) }
    }
    return ""
}

# ------------------------------------------------------------
# Schema validation
# ------------------------------------------------------------
function Test-TurnSchema {
    param([string]$Text)
    $claim       = Extract-Section -Text $Text -Names @("CLAIM")
    $challenge   = Extract-Section -Text $Text -Names @("CHALLENGE")
    $evidence    = Extract-Section -Text $Text -Names @("EVIDENCE")
    $uncertainty = Extract-Section -Text $Text -Names @("UNCERTAINTY","UNCERTAINTIES")
    return (
        -not [string]::IsNullOrWhiteSpace($claim)       -and
        -not [string]::IsNullOrWhiteSpace($challenge)   -and
        -not [string]::IsNullOrWhiteSpace($evidence)    -and
        -not [string]::IsNullOrWhiteSpace($uncertainty)
    )
}

function Test-SynthesisSchema {
    param([string]$Text)
    $claim    = Extract-Section -Text $Text -Names @("CLAIM")
    $evidence = Extract-Section -Text $Text -Names @("EVIDENCE")
    $counters = Extract-Section -Text $Text -Names @("COUNTERARGUMENTS","CHALLENGE")
    $uncs     = Extract-Section -Text $Text -Names @("UNCERTAINTIES","UNCERTAINTY")
    $final    = Extract-Section -Text $Text -Names @("FINAL_SYNTHESIS","SYNTHESIS")
    return (
        -not [string]::IsNullOrWhiteSpace($claim)    -and
        -not [string]::IsNullOrWhiteSpace($evidence) -and
        -not [string]::IsNullOrWhiteSpace($counters) -and
        -not [string]::IsNullOrWhiteSpace($uncs)     -and
        -not [string]::IsNullOrWhiteSpace($final)
    )
}

function Convert-TurnToCanonicalBlock {
    param([string]$SpeakerTag, [string]$Text)
    $claim       = Extract-Section -Text $Text -Names @("CLAIM")
    $challenge   = Extract-Section -Text $Text -Names @("CHALLENGE")
    $evidence    = Extract-Section -Text $Text -Names @("EVIDENCE")
    $uncertainty = Extract-Section -Text $Text -Names @("UNCERTAINTY","UNCERTAINTIES")
    return (
        "[$SpeakerTag] CLAIM: $claim`r`n" +
        "[$SpeakerTag] CHALLENGE: $challenge`r`n" +
        "[$SpeakerTag] EVIDENCE: $evidence`r`n" +
        "[$SpeakerTag] UNCERTAINTY: $uncertainty"
    )
}

# ------------------------------------------------------------
# Ollama call
# ------------------------------------------------------------
function Invoke-OllamaGenerate {
    param(
        [string]$Model,
        [string]$Prompt,
        [int]$MaxTokens,
        [double]$Temperature,
        [int]$SeedValue,
        [int]$TimeoutSec
    )

    $uri     = "$OllamaBaseUrl/api/generate"
    $payload = @{
        model   = $Model
        prompt  = $Prompt
        stream  = $false
        options = @{
            num_predict = $MaxTokens
            temperature = $Temperature
            seed        = $SeedValue
        }
    }

    if ($Model -match '^qwen3(:|$)' -or $Model -match '^qwen3-') {
        $payload["think"] = $false
    }

    if ($Model -match '^qwen3(:|$)' -or $Model -match '^qwen3-') {
        $payload["think"] = $false
    }
    if ($payload.ContainsKey("think")) {
        Log-System "OLLAMA PATCH [$Model]: think=false applied"
    }
    $json = $payload | ConvertTo-Json -Depth 6 -Compress

    try {
        $resp = Invoke-RestMethod `
            -Uri $uri `
            -Method Post `
            -ContentType "application/json" `
            -Body $json `
            -TimeoutSec $TimeoutSec

        if ($null -eq $resp) { throw "Null response from Ollama." }
        if ($resp.error)     { throw $resp.error }

        return (Normalize-Text $resp.response)
    }
    catch {
        $detail = $_.Exception.Message
        try {
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $detail = "$detail | DETAILS: $($_.ErrorDetails.Message)"
            }
        } catch {}
        throw $detail
    }
}

# ------------------------------------------------------------
# Prompt builders
# ------------------------------------------------------------
function New-TurnPrompt {
    param([string]$Topic, [string]$Transcript, [string]$RoleName, [int]$TurnNumber)
    return @"
You are participant $RoleName in a structured adversarial debate.

TOPIC:
$Topic

TRANSCRIPT SO FAR:
$Transcript

Return ONLY these four sections in plain text with no markdown fences and no preamble:

CLAIM:
<one concise core claim>

CHALLENGE:
<one direct challenge or pressure test>

EVIDENCE:
<2-4 sentences of supporting reasoning tied to the debate context>

UNCERTAINTY:
<one sentence describing what remains uncertain>

This is turn $TurnNumber. Do not omit any section.
"@
}

function New-SynthesisPrompt {
    param([string]$Topic, [string]$Transcript, [string]$SessionId)
    return @"
You are the synthesis model for a structured adversarial debate.

SESSION_ID: $SessionId

TOPIC:
$Topic

FULL TRANSCRIPT:
$Transcript

Produce a final synthesis integrating the debate faithfully.

Return ONLY these sections in plain text with no markdown fences and no preamble:

CLAIM:
<the strongest balanced claim>

EVIDENCE:
<bullet list or sentences of the strongest supporting evidence>

COUNTERARGUMENTS:
<main opposing or limiting arguments raised in the debate>

UNCERTAINTIES:
<remaining uncertainties or unresolved issues>

FINAL_SYNTHESIS:
<a concise final synthesis paragraph>

Do not omit any section.
"@
}

# ------------------------------------------------------------
# Turn execution
# ------------------------------------------------------------
function Run-Turn {
    param(
        [string]$ModelName,
        [string]$RoleName,
        [string]$Topic,
        [string]$Transcript,
        [int]$TurnNumber
    )

    if (Test-StopRequested) { throw "STOP requested before $RoleName T$TurnNumber." }

    $prompt = New-TurnPrompt -Topic $Topic -Transcript $Transcript `
                             -RoleName $RoleName -TurnNumber $TurnNumber

    Log-System ">>> $RoleName T$TurnNumber on $ModelName (timeout=${TurnTimeoutSec}s)"

    try {
        $raw = Invoke-OllamaGenerate `
            -Model $ModelName -Prompt $prompt `
            -MaxTokens $MaxTokensPerTurn -Temperature $DebateTemp `
            -SeedValue $Seed -TimeoutSec $TurnTimeoutSec
    }
    catch {
        Log-System "OLLAMA ERROR [$ModelName] $RoleName T${TurnNumber}: $($_.Exception.Message)"
        throw
    }

    Log-System "<<< $RoleName T$TurnNumber returned $($raw.Length) chars"

    if (-not (Test-TurnSchema $raw)) {
        Log-System "Schema invalid, retrying: $RoleName T$TurnNumber"
        if (Test-StopRequested) { throw "STOP requested before retry $RoleName T$TurnNumber." }

        try {
            $raw = Invoke-OllamaGenerate `
                -Model $ModelName -Prompt $prompt `
                -MaxTokens $MaxTokensPerTurn -Temperature $DebateTemp `
                -SeedValue $Seed -TimeoutSec $TurnTimeoutSec
        }
        catch {
            Log-System "OLLAMA ERROR [$ModelName] retry $RoleName T${TurnNumber}: $($_.Exception.Message)"
            throw
        }

        Log-System "<<< $RoleName T$TurnNumber retry returned $($raw.Length) chars"

        if (-not (Test-TurnSchema $raw)) {
            $head = if ($raw.Length -gt 500) { $raw.Substring(0,500) } else { $raw }
            Log-System "Schema still invalid after retry: $RoleName T$TurnNumber"
            Log-System "$RoleName RAW HEAD: $head"
            throw "$RoleName schema-invalid at T$TurnNumber after retry."
        }
    }

    return $raw
}

# ------------------------------------------------------------
# Synthesis execution
# ------------------------------------------------------------
function Run-Synthesis {
    param([string]$Topic, [string]$Transcript, [string]$SessionId)

    if (Test-StopRequested) { throw "STOP requested before synthesis." }

    $prompt = New-SynthesisPrompt -Topic $Topic -Transcript $Transcript -SessionId $SessionId

    Log-System ">>> Synthesis on $ModelSynth (timeout=${SynthTimeoutSec}s)"

    try {
        $raw = Invoke-OllamaGenerate `
            -Model $ModelSynth -Prompt $prompt `
            -MaxTokens $MaxTokensSynth -Temperature $SynthTemp `
            -SeedValue $Seed -TimeoutSec $SynthTimeoutSec
    }
    catch {
        Log-System "OLLAMA ERROR [$ModelSynth] synthesis: $($_.Exception.Message)"
        throw
    }

    Log-System "<<< Synthesis returned $($raw.Length) chars"

    if (-not (Test-SynthesisSchema $raw)) {
        Log-System "Synthesis schema invalid, retrying"
        if (Test-StopRequested) { throw "STOP requested before synthesis retry." }

        try {
            $raw = Invoke-OllamaGenerate `
                -Model $ModelSynth -Prompt $prompt `
                -MaxTokens $MaxTokensSynth -Temperature $SynthTemp `
                -SeedValue $Seed -TimeoutSec $SynthTimeoutSec
        }
        catch {
            Log-System "OLLAMA ERROR [$ModelSynth] synthesis retry: $($_.Exception.Message)"
            throw
        }

        Log-System "<<< Synthesis retry returned $($raw.Length) chars"

        if (-not (Test-SynthesisSchema $raw)) {
            $head = if ($raw.Length -gt 700) { $raw.Substring(0,700) } else { $raw }
            Log-System "Synthesis still invalid after retry"
            Log-System "SYNTH RAW HEAD: $head"
            throw "Synthesis schema-invalid after retry."
        }
    }

    return $raw
}

# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------
Log-System "SOVEREIGN Broker started"
Log-System "Models: A=$ModelA B=$ModelB C=$ModelC Synth=$ModelSynth"
Log-System "Config: Rounds=$DebateRounds MaxTokens/Turn=$MaxTokensPerTurn DebateTemp=$DebateTemp SynthTemp=$SynthTemp Seed=$Seed TurnTimeout=${TurnTimeoutSec}s SynthTimeout=${SynthTimeoutSec}s"
Log-System "Paths: TopicFile=$TopicFile SynthesisFile=$SynthesisFile"

Write-Host ""
Write-Host "=== SOVEREIGN BROKER ==="
Write-Host "Root         : $RootResolved"
Write-Host "Topic file   : $TopicFile"
Write-Host "Synthesis out: $SynthesisFile"
Write-Host "STOP file    : $StopFile"
Write-Host ""

if (Test-StopRequested) {
    Log-System "STOP file present at startup. Exiting."
    exit 0
}

# Read topic
$rawTopicPayload = Get-RawTopicPayload
$sessionId = $env:SOVEREIGN_SESSION_ID

if ([string]::IsNullOrWhiteSpace($sessionId)) {
    $sessionId = Get-SessionIdFromText -Text $rawTopicPayload
}

if ([string]::IsNullOrWhiteSpace($sessionId)) {
    $sessionId = New-SessionId
    Log-System "No session_id found in env or topic payload. Generated session_id=$sessionId"
}
else {
    Log-System "Using session_id=$sessionId"
}

$topic = Get-CleanTopic -RawText $rawTopicPayload
if ([string]::IsNullOrWhiteSpace($topic)) {
    Log-System "ERROR: Topic text is empty after parsing."
    exit 1
}

$sessionNum       = 1
$transcriptBuilder = [System.Text.StringBuilder]::new()
$transcript       = ""
$turnCounter      = 0

Log-System "Session $sessionNum started. session_id=$sessionId Topic: $topic"

Log-Dialog ""
Log-Dialog "============================================================"
Log-Dialog "[SESSION $sessionNum] session_id=$sessionId TOPIC: $topic"
Log-Dialog "============================================================"
Log-Dialog ""

# Static role/model map; rounds iterate this sequence.
$roleModels = @(
    @{ Role = "A"; Model = $ModelA },
    @{ Role = "B"; Model = $ModelB },
    @{ Role = "C"; Model = $ModelC }
)

try {
    for ($r = 1; $r -le $DebateRounds; $r++) {
        foreach ($entry in $roleModels) {
            if (Test-StopRequested) { throw "STOP requested during debate." }

            $turnCounter++
            $role  = $entry.Role
            $model = $entry.Model
            $tag   = "$role T$turnCounter"

            $rawTurn       = Run-Turn -ModelName $model -RoleName $role `
                                      -Topic $topic -Transcript $transcript `
                                      -TurnNumber $turnCounter
            $canonicalTurn = Convert-TurnToCanonicalBlock -SpeakerTag $tag -Text $rawTurn

            Log-Dialog $canonicalTurn
            Log-Dialog ""

            if ($transcriptBuilder.Length -gt 0) {
                [void]$transcriptBuilder.Append("`r`n`r`n")
            }
            [void]$transcriptBuilder.Append($canonicalTurn)
            $transcript = $transcriptBuilder.ToString()
        }
    }

    $synth = Run-Synthesis -Topic $topic -Transcript $transcript -SessionId $sessionId

    # Write synthesis Ã¢â‚¬â€ overwrites file for this session
    # Historical sessions are preserved in session_graph.json after backfill
    # Write synthesis - overwrites file for this session
    # Historical sessions are preserved in session_graph.json after backfill
    if ([string]::IsNullOrWhiteSpace($synth)) {
        throw "Synthesis output is null or empty."
    }

    $finalOutput = "[SYNTH session_id=$sessionId]`r`n$($synth.Trim())`r`n[/SYNTH]`r`n"
    Set-Content -Path $SynthesisFile -Value $finalOutput -Encoding UTF8 -NoNewline

    Log-System "Synthesis written: $SynthesisFile"
    Log-System "Session $sessionNum completed. session_id=$sessionId"

    exit 0
}
catch {
    Log-System "Session $sessionNum aborted. session_id=$sessionId ERROR: $($_.Exception.Message)"
    exit 1
}
