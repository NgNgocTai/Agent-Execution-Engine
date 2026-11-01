function Resolve-CheckExecutable {
    param([string]$FilePath)
    if (Test-Path -LiteralPath $FilePath -PathType Leaf) {
        return (Resolve-Path -LiteralPath $FilePath).Path
    }
    return $FilePath
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    Write-Host "== $Label =="
    & $FilePath @Arguments
    $nativeExitCode = $LASTEXITCODE
    if ($nativeExitCode -ne 0) {
        # Do not echo arguments: caller-owned scripts may pass credentials.
        throw "$Label failed (exit code $nativeExitCode)."
    }
}

function Set-CheckEnvironment {
    $names = @(
        'NOC_AGENT_ENV_FILE', 'DATABASE_URL', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
        'DEEPSEEK_API_KEY', 'OPENROUTER_API_KEY', 'RAGAS_EVAL_API_KEY',
        'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'
    )
    $previous = @{}
    foreach ($name in $names) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:NOC_AGENT_ENV_FILE = if ($env:OS -eq 'Windows_NT') { 'NUL' } else { '/dev/null' }
    $env:DATABASE_URL = 'sqlite+aiosqlite:///:memory:'
    return $previous
}

function Restore-CheckEnvironment {
    param([hashtable]$Previous)
    foreach ($name in $Previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $Previous[$name], 'Process')
    }
}
