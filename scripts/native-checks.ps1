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
        'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'PYTHONUTF8',
        'AUTH_MODE', 'AUTH_PRINCIPALS_JSON', 'DEPLOYMENT_MODE', 'DATA_MODE', 'WORKER_MODE',
        'SSH_PASSWORD', 'SSH_USER', 'SSH_HOST', 'SSH_KEY_PATH', 'SSH_KNOWN_HOSTS',
        'SSH_ALLOWED_HOSTS', 'SSH_ALLOWED_SERVICES', 'SSH_AUTO_ADD_HOST_KEYS',
        'CLICKHOUSE_HOST', 'CLICKHOUSE_USER', 'CLICKHOUSE_PASSWORD',
        'POSTGRES_HOST', 'POSTGRES_USER', 'POSTGRES_PASSWORD'
    )
    $previous = @{}
    foreach ($name in $names) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:NOC_AGENT_ENV_FILE = if ($env:OS -eq 'Windows_NT') { 'NUL' } else { '/dev/null' }
    $env:DATABASE_URL = 'sqlite+aiosqlite:///:memory:'
    $env:PYTHONUTF8 = '1'
    $env:AUTH_MODE = 'local'
    $env:AUTH_PRINCIPALS_JSON = '[]'
    $env:DEPLOYMENT_MODE = 'development'
    $env:DATA_MODE = 'live' # Unit fixtures inject repositories; never bootstrap live sources.
    $env:WORKER_MODE = 'embedded'
    return $previous
}

function Restore-CheckEnvironment {
    param([hashtable]$Previous)
    foreach ($name in $Previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $Previous[$name], 'Process')
    }
}
