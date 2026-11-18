import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe")

DRIVER = r'''
import json, os, sys
kind, args = sys.argv[1], sys.argv[2:]
if kind == "docker" and args[:1] in (["info"], ["version"]):
    stage = "docker-info"
elif kind == "docker" and args[:1] == ["ps"]:
    stage = "docker-ps"
elif kind == "docker" and args[:1] == ["run"] and any("playwright" in arg for arg in args):
    stage = "browser"
elif kind == "docker" and args[:1] == ["build"]:
    stage = "docker-build"
elif kind == "npm":
    stage = "npm-ci" if args[:1] == ["ci"] else "npm-" + args[1]
else:
    if kind == "docker":
        args = args[args.index("python") + 1:] if "python" in args else args[args.index("ruff"):]
    if "ruff" in args:
        stage = "lint"
    elif "unittest" in args:
        stage = "unit"
    elif "evals.adversarial" in args:
        stage = "adversarial"
    elif "evals.runner" in args:
        stage = args[args.index("--kind") + 1]
    elif "scripts/capture_ragas_snapshot.py" in args:
        stage = "snapshot"
    else:
        raise SystemExit(99)
with open(os.environ["CHECK_TEST_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({
        "stage": stage,
        "has_key": bool(os.environ.get("OPENAI_API_KEY")),
        "dotenv": os.environ.get("NOC_AGENT_ENV_FILE"),
        "memory_db": os.environ.get("DATABASE_URL") == "sqlite+aiosqlite:///:memory:",
        "compose": "compose" in sys.argv,
    }) + "\n")
raise SystemExit(17 if stage == os.environ.get("CHECK_TEST_FAIL") else 0)
'''


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell script integration tests")
class CheckScriptsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="check script tests ")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.log = self.path / "calls.jsonl"
        driver = self.path / "driver.py"
        driver.write_text(DRIVER, encoding="utf-8")
        self.fake = {}
        for kind in ("python", "docker", "npm"):
            executable = self.path / f"{kind}.cmd"
            executable.write_text(
                f'@echo off\r\n@"{sys.executable}" "{driver}" {kind} %*\r\n@exit /b %errorlevel%\r\n',
                encoding="utf-8",
            )
            self.fake[kind] = str(executable)

    def _environment(self, fail):
        return {
            **os.environ,
            "PATH": str(self.path) + os.pathsep + os.environ.get("PATH", ""),
            "CHECK_TEST_LOG": str(self.log),
            "CHECK_TEST_FAIL": fail,
            "OPENAI_API_KEY": "script-test-sentinel",
            "DATABASE_URL": "script-test-database",
            "NOC_AGENT_ENV_FILE": "script-test-dotenv",
        }

    def _run(self, script, fail="", extra=(), mode="Local", relative=False):
        self.log.unlink(missing_ok=True)
        args = [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / script)]
        if script in {"scripts/check_all.ps1", "backend/scripts/run_eval_suite.ps1"}:
            args += ["-Mode", mode, "-PythonExecutable", ".\\python.cmd" if relative else self.fake["python"], "-ReportDirectory", str(self.path / "reports")]
        if script == "scripts/check_all.ps1":
            args += ["-NpmExecutable", ".\\npm.cmd" if relative else self.fake["npm"]]
        process = subprocess.run(
            args + list(extra), cwd=self.path, env=self._environment(fail),
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []
        return process, calls

    def test_local_backend_success_is_offline_and_runs_all_checks(self):
        process, calls = self._run("backend/scripts/run_eval_suite.ps1")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual([call["stage"] for call in calls], ["lint", "unit", "contracts", "adversarial", "benchmark"])
        self.assertTrue(all(not call["has_key"] and call["memory_db"] and call["dotenv"] == "NUL" for call in calls))

    def test_each_backend_failure_stops_remaining_checks(self):
        stages = ["lint", "unit", "contracts", "adversarial", "benchmark"]
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                process, calls = self._run("backend/scripts/run_eval_suite.ps1", fail=stage)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual([call["stage"] for call in calls], stages[:index + 1])
                self.assertNotIn("Backend checks passed", process.stdout)

    def test_relative_executables_work_when_the_runner_changes_directory(self):
        process, calls = self._run("scripts/check_all.ps1", relative=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual([call["stage"] for call in calls], [
            "lint", "unit", "contracts", "adversarial", "benchmark", "npm-ci", "npm-lint", "npm-typecheck", "npm-test", "npm-build",
        ])
        self.assertIn("All checks passed", process.stdout)

    def test_docker_mode_builds_checks_image_without_production_compose(self):
        process, calls = self._run("backend/scripts/run_eval_suite.ps1", mode="Docker")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual([call["stage"] for call in calls], ["docker-info", "docker-build", "lint", "unit", "contracts", "adversarial", "benchmark"])
        self.assertTrue(all(not call["compose"] for call in calls))

    def test_docker_build_failure_stops_before_tests(self):
        process, calls = self._run("backend/scripts/run_eval_suite.ps1", fail="docker-build", mode="Docker")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([call["stage"] for call in calls], ["docker-info", "docker-build"])

    def test_root_runner_stops_before_frontend_when_backend_fails(self):
        process, calls = self._run("scripts/check_all.ps1", fail="unit")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([call["stage"] for call in calls], ["lint", "unit"])
        self.assertNotIn("All checks passed", process.stdout)

    def test_each_frontend_failure_stops_remaining_checks(self):
        stages = ["lint", "unit", "contracts", "adversarial", "benchmark", "npm-ci", "npm-lint", "npm-typecheck", "npm-test", "npm-build"]
        for stage in stages[4:]:
            with self.subTest(stage=stage):
                process, calls = self._run("scripts/check_all.ps1", fail=stage)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual([call["stage"] for call in calls], stages[:stages.index(stage) + 1])
                self.assertNotIn("All checks passed", process.stdout)

    def test_batch_entry_point_propagates_failure(self):
        process = subprocess.run(
            ["cmd.exe", "/d", "/c", str(ROOT / "scripts/check_all.bat"), "-Mode", "Local",
             "-PythonExecutable", self.fake["python"], "-NpmExecutable", self.fake["npm"],
             "-ReportDirectory", str(self.path / "reports")],
            cwd=self.path, env=self._environment("npm-typecheck"),
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        self.assertNotEqual(process.returncode, 0)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[-1]["stage"], "npm-typecheck")
        self.assertNotIn("All checks passed", process.stdout)

    def test_failure_restores_environment_and_working_directory(self):
        wrapper = self.path / "wrapper.ps1"
        # Values are sent through environment variables; none are logged.
        wrapper.write_text(r'''
$ErrorActionPreference = 'Stop'
$before = (Get-Location).Path
$key = $env:OPENAI_API_KEY
$database = $env:DATABASE_URL
$dotenv = $env:NOC_AGENT_ENV_FILE
try {
    & $env:CHECK_ROOT_SCRIPT -Mode Local -PythonExecutable $env:CHECK_PYTHON -NpmExecutable $env:CHECK_NPM -ReportDirectory $env:CHECK_REPORTS
    exit 9
} catch {
    if ($env:OPENAI_API_KEY -ne $key -or $env:DATABASE_URL -ne $database -or $env:NOC_AGENT_ENV_FILE -ne $dotenv -or (Get-Location).Path -ne $before) { exit 8 }
    Write-Output 'RESTORED'
}
''', encoding="utf-8")
        environment = {
            **self._environment("unit"),
            "CHECK_ROOT_SCRIPT": str(ROOT / "scripts/check_all.ps1"),
            "CHECK_PYTHON": self.fake["python"], "CHECK_NPM": self.fake["npm"],
            "CHECK_REPORTS": str(self.path / "reports"),
        }
        process = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            cwd=self.path, env=environment, capture_output=True, text=True, errors="replace", timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("RESTORED", process.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(all(not call["has_key"] for call in calls))

    def test_failed_benchmark_does_not_call_ragas_judge(self):
        process, calls = self._run("backend/scripts/run_ragas_eval.ps1", fail="benchmark")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([call["stage"] for call in calls], ["benchmark"])

    def test_ragas_quality_failure_is_nonzero(self):
        process, calls = self._run("backend/scripts/run_ragas_eval.ps1", fail="quality")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([call["stage"] for call in calls], ["benchmark", "quality"])

    def test_snapshot_failure_is_nonzero(self):
        process, calls = self._run("backend/scripts/refresh_real_snapshots.ps1", fail="snapshot", extra=("-ListConversations",))
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([call["stage"] for call in calls], ["snapshot"])

    def test_browser_runner_propagates_failure_and_restores_its_cleanup_path(self):
        process, calls = self._run("scripts/check_browser.ps1", fail="browser", extra=("-ReportDirectory", str(self.path / "browser reports")))
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([call["stage"] for call in calls], ["browser", "docker-ps"])
        self.assertNotIn("Browser checks passed", process.stdout)


if __name__ == "__main__":
    unittest.main()
