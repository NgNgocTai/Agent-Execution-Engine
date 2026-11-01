param(
    [ValidateSet('Docker', 'Local')][string]$Mode = 'Docker',
    [string]$PythonExecutable = 'python',
    [string]$ReportDirectory = '',
    [string]$Image = 'noc-agent-checks',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$rootDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$backendDir = Join-Path $rootDir 'backend'
. (Join-Path $rootDir 'scripts/native-checks.ps1')
$PythonExecutable = Resolve-CheckExecutable -FilePath $PythonExecutable
if (-not $ReportDirectory) { $ReportDirectory = Join-Path $rootDir '.check-results/evals' }
$ReportDirectory = [IO.Path]::GetFullPath($ReportDirectory)
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$previousEnvironment = Set-CheckEnvironment

$locationPushed = $false
try {
    Push-Location $backendDir
    $locationPushed = $true
    if ($Mode -eq 'Docker') {
        Invoke-CheckedCommand -Label 'Docker daemon' -FilePath 'docker' -Arguments @('version', '--format', '{{.Server.Version}}')
        if (-not $SkipBuild) {
            Invoke-CheckedCommand -Label 'Build checks image' -FilePath 'docker' -Arguments @(
                'build', '--target', 'checks', '--tag', $Image, $backendDir
            )
        }
    }
    $steps = @(
        @{ Label = 'Backend lint'; Args = @('-m', 'ruff', 'check', '.') },
        @{ Label = 'Backend unit and integration tests'; Args = @('-m', 'unittest', 'discover', '-s', 'tests', '-v') },
        @{ Label = 'Offline contract evals'; Args = @('-m', 'evals.runner', '--kind', 'contracts') },
        @{ Label = 'Offline workflow evals'; Args = @('-m', 'evals.runner', '--kind', 'benchmark', '--quality-evaluator', 'none', '--results-dir') }
    )
    foreach ($step in $steps) {
        $commandArgs = @($step.Args)
        if ($step.Label -eq 'Offline workflow evals') {
            $commandArgs += if ($Mode -eq 'Docker') { '/reports' } else { $ReportDirectory }
        }
        if ($Mode -eq 'Docker') {
            $program = 'python'
            if ($step.Label -eq 'Backend lint') {
                $program = 'ruff'
                $commandArgs = @('check', '.')
            }
            Invoke-CheckedCommand -Label $step.Label -FilePath 'docker' -Arguments (@(
                'run', '--rm', '--network', 'none',
                '--env', 'DATABASE_URL=sqlite+aiosqlite:///:memory:',
                '--env', 'NOC_AGENT_ENV_FILE=/dev/null',
                '--mount', "type=bind,source=$ReportDirectory,target=/reports",
                $Image, $program
            ) + $commandArgs)
        } else {
            Invoke-CheckedCommand -Label $step.Label -FilePath $PythonExecutable -Arguments $commandArgs
        }
    }
    Write-Host 'Backend checks passed.'
} finally {
    if ($locationPushed) { Pop-Location }
    Restore-CheckEnvironment -Previous $previousEnvironment
}
