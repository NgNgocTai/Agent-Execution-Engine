$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Resolve-Path (Join-Path $scriptDir "..")

Push-Location $backendDir
try {
    Write-Host ""
    Write-Host "== Unit/Integration Tests: backend ==" -ForegroundColor Cyan
    docker compose run --rm `
        -v "${backendDir}:/app" `
        backend python -m unittest discover -s tests

    Write-Host ""
    Write-Host "== Contract/Integration Evals: skill/tool + scripted agent-tool ==" -ForegroundColor Cyan
    docker compose run --rm `
        -v "${backendDir}:/app" `
        backend python -m evals.runner --kind contracts

    Write-Host ""
    Write-Host "== End-to-End Workflow Evals: agent benchmark ==" -ForegroundColor Cyan
    docker compose run --rm `
        -v "${backendDir}:/app" `
        backend python -m evals.runner --kind benchmark
}
finally {
    Pop-Location
}
