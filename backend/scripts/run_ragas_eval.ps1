$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Resolve-Path (Join-Path $scriptDir "..")

Push-Location $backendDir
try {
    Write-Host ""
    Write-Host "== End-to-End Workflow Evals: Deterministic Only ==" -ForegroundColor Cyan
    docker compose run --rm `
        -v "${backendDir}:/app" `
        -v noc_agent_eval_deps:/eval_deps `
        -e PYTHONPATH=/eval_deps:/app `
        -e GIT_PYTHON_REFRESH=quiet `
        backend python -m evals.runner --kind benchmark

    Write-Host ""
    Write-Host "== Ragas Quality Evaluation: Frozen Real Snapshots ==" -ForegroundColor Cyan
    docker compose run --rm `
        -v "${backendDir}:/app" `
        -v noc_agent_eval_deps:/eval_deps `
        -e PYTHONPATH=/eval_deps:/app `
        -e GIT_PYTHON_REFRESH=quiet `
        backend python -m evals.runner `
            --kind quality `
            --dataset noc_agent_ragas_quality_cases.json `
            --results-dir /app/evals/results/ragas_quality `
            --quality-evaluator ragas `
            --quality-source real_conversation_snapshot `
            --quality-scope answer_quality
}
finally {
    Pop-Location
}
