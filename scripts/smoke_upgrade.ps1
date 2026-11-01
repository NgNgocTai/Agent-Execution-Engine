param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$rootDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'native-checks.ps1')
$project = 'noc-upgrade-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$compose = @('compose', '--env-file', (Join-Path $rootDir '.env.example'), '--project-name', $project, '--file', (Join-Path $rootDir 'compose.yml'))
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$previousPort = $env:NOC_WEB_PORT
$previousDelay = $env:NOC_FIXTURE_DELAY_SECONDS
$fixtureEnvironment = @{}
foreach ($name in @('NOC_RUNTIME_ENV_FILE','DATA_MODE','DEPLOYMENT_MODE','AUTH_MODE','AUTH_PRINCIPALS_JSON','CONTROL_DATABASE_NAME','CONTROL_DATABASE_USER','CONTROL_DATABASE_PASSWORD')) {
    $fixtureEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$env:NOC_RUNTIME_ENV_FILE = Join-Path $rootDir '.env.example'
$env:DATA_MODE = 'fixture'
$env:DEPLOYMENT_MODE = 'development'
$env:AUTH_MODE = 'local'
$env:AUTH_PRINCIPALS_JSON = '[]'
$env:CONTROL_DATABASE_NAME = 'noc_agent_control'
$env:CONTROL_DATABASE_USER = 'noc'
$env:CONTROL_DATABASE_PASSWORD = 'local-development-only'
$env:NOC_WEB_PORT = [string]$port
$env:NOC_FIXTURE_DELAY_SECONDS = '3'
$baseUrl = "http://127.0.0.1:$port"
$started = $false

function Invoke-SmokeDocker {
    param([string[]]$CommandArgs)
    $output = & docker @compose @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Compose smoke command failed.' }
    return $output
}

function Wait-SmokeReady {
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        try { if ((Invoke-RestMethod "$baseUrl/api/config" -TimeoutSec 2).providers -contains 'mock') { return } } catch { }
        Start-Sleep -Seconds 1
    }
    throw 'Isolated fixture stack did not become ready.'
}

try {
    $context = & docker context inspect | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Docker context is unavailable' }
    $endpoint = if ($env:DOCKER_HOST) { $env:DOCKER_HOST } else { $context[0].Endpoints.docker.Host }
    if ($endpoint -notmatch '^(npipe|unix)://') { throw 'Smoke rehearsal requires a local Docker daemon' }
    $up = @('up', '--detach', '--wait')
    $started = $true
    if (-not $SkipBuild) { $up += '--build' }
    Invoke-SmokeDocker -CommandArgs $up | Out-Null
    Wait-SmokeReady
    $run = Invoke-RestMethod "$baseUrl/api/runs" -Method Post -ContentType 'application/json' -Body (@{message='Fixture alarm investigation';provider='mock';idempotency_key=[guid]::NewGuid().ToString('N')} | ConvertTo-Json)
    if ($run.run_id -notmatch '^[a-f0-9]{32}$') { throw 'Unexpected fixture run ID' }
    Invoke-SmokeDocker -CommandArgs @('stop', 'backend') | Out-Null
    $completed = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        $state = Invoke-SmokeDocker -CommandArgs @('exec','-T','db','psql','-U','noc','-d','noc_agent_control','-At','-c',"SELECT status FROM runs WHERE id='$($run.run_id)'")
        if ($state -eq 'succeeded') { $completed=$true; break }
        if ($state -eq 'failed') { throw 'Fixture run failed while web was stopped' }
        Start-Sleep -Seconds 1
    }
    if (-not $completed) { throw 'External worker did not finish while web was stopped' }
    Invoke-SmokeDocker -CommandArgs @('start', 'backend') | Out-Null
    Wait-SmokeReady
    $snapshot = Invoke-RestMethod "$baseUrl/api/runs/$($run.run_id)"
    if ($snapshot.status -ne 'succeeded' -or -not ($snapshot.messages | Where-Object role -eq 'assistant')) { throw 'Snapshot was not restored after web restart' }

    Invoke-SmokeDocker -CommandArgs @('exec','-T','db','pg_dump','-U','noc','-d','noc_agent_control','--format=custom','--file=/tmp/upgrade-control.dump') | Out-Null
    Invoke-SmokeDocker -CommandArgs @('exec','-T','db','createdb','-U','noc','noc_upgrade_restore') | Out-Null
    Invoke-SmokeDocker -CommandArgs @('exec','-T','db','pg_restore','-U','noc','-d','noc_upgrade_restore','/tmp/upgrade-control.dump') | Out-Null
    foreach ($table in @('conversations','messages','runs','run_events','audit_entries')) {
        $sourceCount = Invoke-SmokeDocker -CommandArgs @('exec','-T','db','psql','-U','noc','-d','noc_agent_control','-At','-c',"SELECT count(*) FROM $table")
        $restoreCount = Invoke-SmokeDocker -CommandArgs @('exec','-T','db','psql','-U','noc','-d','noc_upgrade_restore','-At','-c',"SELECT count(*) FROM $table")
        if ($sourceCount -ne $restoreCount) { throw "PostgreSQL restore count mismatch: $table" }
    }
    Write-Host 'Fixture stack, external worker, web restart, snapshot replay and PostgreSQL backup/restore passed.'
} finally {
    # This generated project name is the ownership boundary. Never prune the
    # daemon, reuse a user project name or remove unrelated named volumes.
    if ($started -and $project -match '^noc-upgrade-smoke-[a-f0-9]{12}$') {
        & docker @compose down --volumes --remove-orphans
    }
    $env:NOC_WEB_PORT = $previousPort
    $env:NOC_FIXTURE_DELAY_SECONDS = $previousDelay
    foreach ($name in $fixtureEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $fixtureEnvironment[$name], 'Process')
    }
}
