import asyncio
import shlex
import time
from datetime import datetime, timezone

import paramiko

from core.config import settings
from core.policy import approved_snapshot, target_allowed
from services.skills.input_models import NodeHealthInput
from connectors.ssh import SSHConnector, SSHExecutionError, SSHResult
from services.skills.base import BaseSkill, SkillResult, SkillStatus

from services.metrics_script import METRICS_SCRIPT

# Giới hạn an toàn cho log_lines để tránh ai đó truyền số khổng lồ làm tràn context
MAX_LOG_LINES = 1000
DANGEROUS_ACTIONS = {"restart_docker", "restart_service"}


class NodeHealthRemediateSkill(BaseSkill):
    input_model = NodeHealthInput
    @property
    def name(self) -> str:
        return "node_health_remediate"

    @property
    def description(self) -> str:
        return (
            "SSH vào node mạng, đo RAM% và CPU%, rồi tự động xử lý theo ngưỡng: "
            "RAM>ngưỡng X VÀ CPU>ngưỡng Y → restart docker; "
            "RAM>ngưỡng X nhưng CPU<=Y → đọc log container; "
            "RAM<=ngưỡng X nhưng CPU>Y → restart container; "
            "cả hai dưới ngưỡng → không làm gì."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "IP hoặc hostname của node (bắt buộc)",
                },
                "ram_threshold": {
                    "type": "integer",
                    "description": "Ngưỡng RAM% để coi là cao. Mặc định 80.",
                    "default": 80,
                },
                "cpu_threshold": {
                    "type": "integer",
                    "description": "Ngưỡng CPU% để coi là cao. Mặc định 80.",
                    "default": 80,
                },
                "service": {
                    "type": "string",
                    "description": "Tên container docker (cần cho nhánh đọc log & restart service)",
                    "default": "",
                },
                "log_lines": {
                    "type": "integer",
                    "description": f"Số dòng log lấy về khi đọc log container. Mặc định 200, tối đa {MAX_LOG_LINES}.",
                    "default": 200,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "CẢNH BÁO QUAN TRỌNG: LUÔN LUÔN đặt dry_run = True. TUYỆT ĐỐI KHÔNG ĐƯỢC set bằng False khi chưa được người dùng yêu cầu rõ ràng. True = chỉ in lệnh (An toàn), False = thực thi thật (Nguy hiểm).",
                    "default": True,
                },
            },
            "required": ["host"],
        }

    def _ssh_exec(self, host: str, command: str) -> SSHResult:
        if not target_allowed(host):
            raise SSHExecutionError("SSH target denied by policy", code="policy_denied")
        return SSHConnector(settings).execute(host, command)

    def _get_metrics(self, host: str) -> dict:
        import json

        result = self._ssh_exec(host, METRICS_SCRIPT)
        exit_code, output, _ = result
        if exit_code != 0 or getattr(result, "stdout_truncated", False):
            raise SSHExecutionError("SSH metrics are incomplete or the command failed", code="ssh_metrics")
        return json.loads(output)

    def execute_approved_command(self, host: str, command: str) -> dict:
        try:
            result = self._ssh_exec(host, command)
            exit_code, stdout_text, stderr_text = result
            verification = self._verify_command(host, command) if exit_code == 0 else {"status": "not_run", "reason": "Command failed"}
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": host,
                "command": command,
                "executed": True,
                "command_succeeded": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdout_truncated": getattr(result, "stdout_truncated", False),
                "stderr_truncated": getattr(result, "stderr_truncated", False),
                "execution_status": "succeeded" if exit_code == 0 else "failed",
                "verification": verification,
            }
        except SSHExecutionError as e:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": host,
                "command": command,
                "executed": None if e.dispatched else False,
                "command_succeeded": False,
                "exit_code": None,
                "stdout": e.stdout,
                "stderr": str(e),
                "stdout_truncated": e.stdout_truncated,
                "stderr_truncated": e.stderr_truncated,
                "execution_status": "unknown" if e.dispatched else "not_started",
                "error_code": e.code,
            }
        except Exception as e:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": host,
                "command": command,
                "executed": None,
                "command_succeeded": False,
                "exit_code": None,
                "stdout": None,
                "stderr": f"Error executing approved command: {e}",
                "execution_status": "unknown",
            }

    def _verify_command(self, host, command):
        parts = shlex.split(command)
        if parts[:2] == ["docker", "restart"] and len(parts) == 3:
            probe = "docker inspect --format '{{.State.Running}}' " + shlex.quote(parts[2])
            expected = "true"
        elif command == "sudo -n systemctl restart docker":
            probe, expected = "systemctl is-active docker", "active"
        else:
            return {"status": "not_requested"}
        try:
            for attempt in range(3):
                if attempt:
                    time.sleep(0.5)
                result = self._ssh_exec(host, probe)
                code, output, _ = result
                if code == 0 and output.strip().lower() == expected and not getattr(result, "stdout_truncated", False):
                    return {"status": "recovered", "observed_at": datetime.now(timezone.utc).isoformat(), "probe": probe}
            return {"status": "not_recovered", "observed_at": datetime.now(timezone.utc).isoformat(), "probe": probe}
        except Exception:
            return {"status": "inconclusive", "reason": "Post-check unavailable; reconcile service state"}

    async def execute(
        self,
        host: str,
        ram_threshold: int = 80,
        cpu_threshold: int = 80,
        service: str = "",
        log_lines: int = 200,
        dry_run: bool = True,
        **kwargs,
    ) -> SkillResult:
        # Chặn log_lines bất thường (LLM hoặc người dùng truyền số quá lớn)
        if log_lines <= 0:
            log_lines = 200
        log_lines = min(log_lines, MAX_LOG_LINES)

        try:
            metrics = await asyncio.to_thread(self._get_metrics, host)
        except SSHExecutionError as e:
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data={"host": host, "error_code": e.code},
                error=str(e),
            )
        except paramiko.AuthenticationException:
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=f"SSH authentication failed for {host}. Check SSH_USER/SSH_PASSWORD.",
            )
        except paramiko.SSHException as e:
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=f"SSH error connecting to {host}: {e}",
            )
        except Exception as e:
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=f"Failed to get metrics from {host}: {e}",
            )

        ram_high = metrics["mem_pct"] > ram_threshold
        cpu_high = metrics["cpu_pct"] > cpu_threshold

        # service luôn được quote an toàn trước khi ghép vào lệnh shell,
        # tránh command injection nếu service chứa ký tự đặc biệt (;, $(), |, ...)
        safe_service = shlex.quote(service) if service else ""

        if ram_high and cpu_high:
            action = "restart_docker"
            command = "sudo -n systemctl restart docker"
        elif ram_high and not cpu_high:
            if not service:
                return SkillResult(
                    status=SkillStatus.BUSINESS_ERROR,
                    data=None,
                    error="Action 'read_logs' requires 'service' parameter (docker container name). Please ask the user.",
                )
            action = "read_logs"
            command = f"docker logs --timestamps --tail {log_lines} {safe_service}"
        elif not ram_high and cpu_high:
            if not service:
                return SkillResult(
                    status=SkillStatus.BUSINESS_ERROR,
                    data=None,
                    error="Action 'restart_service' requires 'service' parameter (docker container name). Please ask the user.",
                )
            action = "restart_service"
            command = f"docker restart {safe_service}"
        else:
            action = "none"
            command = None

        executed = False
        exit_code = None
        stdout_text = None
        stderr_text = None
        command_succeeded = None  # None = chưa chạy; True/False = đã chạy, dựa theo exit_code
        stdout_truncated = False
        stderr_truncated = False
        execution_error = None

        evaluation = {
            "ram_high": ram_high,
            "cpu_high": cpu_high,
        }
        thresholds = {
            "ram_threshold": ram_threshold,
            "cpu_threshold": cpu_threshold,
        }

        if command and not dry_run and action in DANGEROUS_ACTIONS:
            from services.skill_catalog import builtin_manifest
            manifest = builtin_manifest(NodeHealthRemediateSkill())
            approval_metadata = {
                "skill_version": manifest["version"],
                "skill_hash": manifest["content_hash"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": host,
                "service": service,
                "action": action,
                "command": command,
                "metrics": metrics,
                "thresholds": thresholds,
                "evaluation": evaluation,
                "reason": "Dangerous remediation requires human approval before execution.",
            }
            if not approved_snapshot(approval_metadata):
                return SkillResult(status=SkillStatus.BUSINESS_ERROR, data={"error_code": "POLICY_DENIED"}, error="Remediation is outside the configured allowlist")
            return SkillResult(
                status=SkillStatus.SUCCESS,
                data={
                    **approval_metadata,
                    "dry_run": dry_run,
                    "executed": False,
                    "approval_status": "pending",
                },
                needs_approval=True,
                approval_metadata=approval_metadata,
            )

        if command and not dry_run:
            try:
                result = await asyncio.to_thread(self._ssh_exec, host, command)
                exit_code, stdout_text, stderr_text = result
                stdout_truncated = getattr(result, "stdout_truncated", False)
                stderr_truncated = getattr(result, "stderr_truncated", False)
                executed = True
                # executed=True chỉ nghĩa là SSH chạy được lệnh (không exception Python).
                # command_succeeded phản ánh đúng kết quả của LỆNH TRÊN NODE (exit_code).
                command_succeeded = (exit_code == 0)
                if not command_succeeded:
                    execution_error = "ssh_command_failed"
            except SSHExecutionError as e:
                executed = None if e.dispatched else False
                stdout_text = e.stdout
                stderr_text = str(e)
                stdout_truncated = e.stdout_truncated
                stderr_truncated = e.stderr_truncated
                execution_error = e.code
                command_succeeded = False
            except Exception as e:
                stderr_text = f"Error executing command: {e}"
                execution_error = "ssh_connection"
                command_succeeded = False

        return SkillResult(
            status=SkillStatus.SYSTEM_ERROR if command_succeeded is False else SkillStatus.SUCCESS,
            error=stderr_text if command_succeeded is False else None,
            data={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": host,
                "metrics": metrics,
                "thresholds": thresholds,
                "evaluation": evaluation,
                "action": action,
                "command": command,
                "dry_run": dry_run,
                "executed": executed,
                "command_succeeded": command_succeeded,
                "exit_code": exit_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "error_code": execution_error,
            },
        )
