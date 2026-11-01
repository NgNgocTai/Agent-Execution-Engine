param(
    [ValidateSet('Docker', 'Local')][string]$Mode = 'Docker',
    [string]$PythonExecutable = 'python',
    [string]$NpmExecutable = 'npm',
    [string]$ReportDirectory = ''
)

$ErrorActionPreference = 'Stop'
$rootDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$frontendDir = Join-Path $rootDir 'frontend'
. (Join-Path $PSScriptRoot 'native-checks.ps1')
$PythonExecutable = Resolve-CheckExecutable -FilePath $PythonExecutable
$NpmExecutable = Resolve-CheckExecutable -FilePath $NpmExecutable
$previousEnvironment = Set-CheckEnvironment
try {
    & (Join-Path $rootDir 'backend/scripts/run_eval_suite.ps1') `
        -Mode $Mode -PythonExecutable $PythonExecutable -ReportDirectory $ReportDirectory
    if ($Mode -eq 'Docker') {
        # Build in a disposable filesystem; do not overwrite host node_modules
        # or copy frontend dotenv files into the build environment.
        $frontendCommands = @'
tar -C /src --exclude=node_modules --exclude=dist --exclude='.env*' --exclude='*.tsbuildinfo' -cf /tmp/frontend.tar .
tar -C /work -xf /tmp/frontend.tar
npm ci --no-audit --no-fund
npm run lint
npm run typecheck
npm run build
'@
        Invoke-CheckedCommand -Label 'Frontend checks' -FilePath 'docker' -Arguments @(
            'run', '--rm', '--init',
            '--mount', "type=bind,source=$frontendDir,target=/src,readonly",
            '--tmpfs', '/work:rw,exec,size=1g', '--workdir', '/work',
            'node:22-alpine', 'sh', '-ec', $frontendCommands
        )
    } else {
        Push-Location $frontendDir
        try {
            Invoke-CheckedCommand -Label 'Frontend install' -FilePath $NpmExecutable -Arguments @('ci', '--no-audit', '--no-fund')
            foreach ($check in @('lint', 'typecheck', 'build')) {
                Invoke-CheckedCommand -Label "Frontend $check" -FilePath $NpmExecutable -Arguments @('run', $check)
            }
        } finally {
            Pop-Location
        }
    }
    Write-Host 'All checks passed.'
} finally {
    Restore-CheckEnvironment -Previous $previousEnvironment
}
