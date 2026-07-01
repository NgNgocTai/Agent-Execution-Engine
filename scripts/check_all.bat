@echo off
setlocal

set "ROOT=%~dp0.."
set "BACKEND=%ROOT%\backend"
set "IMAGE=backend-backend"

echo [check_all] Checking Docker daemon...
docker info >nul 2>nul
if errorlevel 1 (
  echo [check_all] ERROR: Docker daemon is not running.
  exit /b 1
)

echo [check_all] Running backend unit tests...
docker run --rm -v "%BACKEND%:/app" -w /app %IMAGE% python -m unittest discover tests
if errorlevel 1 (
  echo [check_all] ERROR: backend unit tests failed.
  exit /b 1
)

echo [check_all] Running deterministic evals...
docker run --rm -v "%BACKEND%:/app" -w /app %IMAGE% python -m evals.runner
if errorlevel 1 (
  echo [check_all] ERROR: evals failed.
  exit /b 1
)

echo [check_all] All checks passed.
