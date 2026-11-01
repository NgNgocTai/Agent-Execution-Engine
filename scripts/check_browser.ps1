param([string]$ReportDirectory = '')
$ErrorActionPreference = 'Stop'
$rootDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$frontendDir = Join-Path $rootDir 'frontend'
. (Join-Path $PSScriptRoot 'native-checks.ps1')
if (-not $ReportDirectory) { $ReportDirectory = Join-Path $rootDir '.check-results/browser' }
$ReportDirectory = [IO.Path]::GetFullPath($ReportDirectory)
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$previousEnvironment = Set-CheckEnvironment
$container = 'noc-browser-checks-' + [guid]::NewGuid().ToString('N')
$commands = @'
tar -C /src --exclude=node_modules --exclude=dist --exclude='.env*' --exclude='*.tsbuildinfo' --exclude=playwright-report --exclude=test-results -cf /tmp/frontend.tar .
tar -C /work -xf /tmp/frontend.tar
npm ci --no-audit --no-fund
npm run lint
npm run typecheck
npm test
npm run build
npm run test:e2e -- --output=/reports/test-results
'@
try {
    Invoke-CheckedCommand -Label 'Browser fixture checks' -FilePath 'docker' -Arguments @(
        'run', '--rm', '--init', '--name', $container, '--label', 'codex.task=noc-browser-checks',
        '--memory', '2g', '--cpus', '2', '--pids-limit', '256',
        '--mount', "type=bind,source=$frontendDir,target=/src,readonly",
        '--mount', "type=bind,source=$ReportDirectory,target=/reports",
        '--tmpfs', '/work:rw,exec,size=1g', '--workdir', '/work',
        '--env', 'PLAYWRIGHT_HTML_OUTPUT_DIR=/reports/playwright-report',
        'mcr.microsoft.com/playwright:v1.64.1-noble@sha256:47b510dd9230bf0b9bfc9b20102241e577ea7a99334ab4c3ca19f19770e9b8ea',
        'sh', '-ec', $commands
    )
    Write-Host 'Browser checks passed.'
} finally {
    try {
        $ownedName = & docker ps --all --filter 'label=codex.task=noc-browser-checks' --filter "name=^/$container$" --format '{{.Names}}' 2>$null
        if ($LASTEXITCODE -eq 0 -and $ownedName -eq $container) { & docker rm --force $container | Out-Null }
    } catch { }
    Restore-CheckEnvironment -Previous $previousEnvironment
}
