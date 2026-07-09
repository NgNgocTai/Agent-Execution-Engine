param(
    [switch]$ListConversations,
    [switch]$ListTurns,
    [int]$Limit = 10,
    [string]$ConversationId = "",
    [int]$TurnIndex = 0,
    [string]$UserMessageContains = "",
    [string]$CaseId = "",
    [string]$Name = "",
    [string]$Model = "",
    [string]$ComparisonGroup = "",
    [string]$ReferenceAnswer = "",
    [string]$SnapshotVersion = "v1",
    [ValidateSet("answer_quality", "rule_based", "needs_regeneration")]
    [string]$QualityScope = "answer_quality",
    [ValidateSet("tune", "holdout")]
    [string]$Split = "tune",
    [switch]$DryRun,
    [switch]$Overwrite
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Resolve-Path (Join-Path $scriptDir "..")

$runnerArgs = @("scripts/capture_ragas_snapshot.py")

if ($ListConversations) {
    $runnerArgs += "--list-conversations"
    $runnerArgs += "--limit"
    $runnerArgs += "$Limit"
}
elseif ($ListTurns) {
    if (-not $ConversationId) {
        throw "Missing -ConversationId for -ListTurns."
    }
    $runnerArgs += "--list-turns"
    $runnerArgs += "--conversation-id"
    $runnerArgs += $ConversationId
}
else {
    if (-not $ConversationId -and -not $UserMessageContains) {
        throw "Missing -ConversationId or -UserMessageContains. Use -ListConversations first if needed."
    }

    if ($ConversationId) {
        $runnerArgs += "--conversation-id"
        $runnerArgs += $ConversationId
    }

    if ($TurnIndex -ne 0) {
        $runnerArgs += "--turn-index"
        $runnerArgs += "$TurnIndex"
    }
    if ($UserMessageContains) {
        $runnerArgs += "--user-message-contains"
        $runnerArgs += $UserMessageContains
    }
    if ($CaseId) {
        $runnerArgs += "--case-id"
        $runnerArgs += $CaseId
    }
    if ($Name) {
        $runnerArgs += "--name"
        $runnerArgs += $Name
    }
    if ($Model) {
        $runnerArgs += "--model"
        $runnerArgs += $Model
    }
    if ($ComparisonGroup) {
        $runnerArgs += "--comparison-group"
        $runnerArgs += $ComparisonGroup
    }
    if ($ReferenceAnswer) {
        $runnerArgs += "--reference-answer"
        $runnerArgs += $ReferenceAnswer
    }
    if ($SnapshotVersion) {
        $runnerArgs += "--snapshot-version"
        $runnerArgs += $SnapshotVersion
    }
    if ($QualityScope) {
        $runnerArgs += "--quality-scope"
        $runnerArgs += $QualityScope
    }
    if ($Split) {
        $runnerArgs += "--split"
        $runnerArgs += $Split
    }
    if ($DryRun) {
        $runnerArgs += "--dry-run"
    }
    if ($Overwrite) {
        $runnerArgs += "--overwrite"
    }
}

Push-Location $backendDir
try {
    Write-Host ""
    Write-Host "== Refresh Frozen Ragas Snapshots ==" -ForegroundColor Cyan
    Write-Host "This reads persisted conversations and writes frozen observations."
    Write-Host "It does not call LLM providers or live tools."
    Write-Host ""

    docker compose run --rm `
        -v "${backendDir}:/app" `
        -e PYTHONPATH=/app `
        backend python @runnerArgs
}
finally {
    Pop-Location
}
