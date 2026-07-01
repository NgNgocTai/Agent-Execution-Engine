import logging
import shlex
from datetime import datetime, timezone

import paramiko

from core.config import settings
from services.skills.base import BaseSkill, SkillResult, SkillStatus

logger = logging.getLogger(__name__)

METRICS_SCRIPT = (
    "python3 -c '"
    "import json,os;"
    "f=open(\"/proc/meminfo\");"
    "lines=f.readlines();f.close();"
    "mem_total=int(lines[0].split()[1]);"
    "mem_avail=int(lines[2].split()[1]);"
    "mem_pct=round((1-mem_avail/mem_total)*100,1);"
    "f=open(\"/proc/loadavg\");"
    "load1=float(f.read().split()[0]);f.close();"
    "st=os.statvfs(\"/\");"
    "disk_pct=round((1-st.f_bavail/st.f_blocks)*100,1);"
    "f=open(\"/proc/stat\");"
    "parts=f.readline().split();f.close();"
    "idle=int(parts[4]);"
    "total=sum(int(p) for p in parts[1:]);"
    "cpu_pct=round((1-idle/total)*100,1);"
    "print(json.dumps({\"mem_pct\":mem_pct,\"cpu_pct\":cpu_pct,\"load1\":load1,\"disk_pct\":disk_pct}))"
    "'"
)

# Giới hạn an toàn cho log_lines để tránh ai đó truyền số khổng lồ làm tràn context
MAX_LOG_LINES = 1000
DANGEROUS_ACTIONS = {"restart_docker", "restart_service"}


class NodeHealthRemediateSkill(BaseSkill):
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

    def _ssh_exec(self, host: str, command: str) -> tuple[int, str, str]:
        """Trả về (exit_code, stdout, stderr) — tách riêng để caller tự quyết định
        cách xử lý/hiển thị, không gộp lẫn vào nhau."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=settings.ssh_port,
                username=settings.ssh_user,
                password=settings.ssh_password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False,
            )
            _, stdout, stderr = client.exec_command(command, timeout=15)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            if err:
                logger.warning("SSH stderr [%s]: %s", host, err)
            return exit_code, out, err
        finally:
            client.close()

    def _get_metrics(self, host: str) -> dict:
        import json

        _, output, _ = self._ssh_exec(host, METRICS_SCRIPT)
        return json.loads(output)

    def execute_approved_command(self, host: str, command: str) -> dict:
        try:
            exit_code, stdout_text, stderr_text = self._ssh_exec(host, command)
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": host,
                "command": command,
                "executed": True,
                "command_succeeded": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
            }
        except Exception as e:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": host,
                "command": command,
                "executed": False,
                "command_succeeded": False,
                "exit_code": None,
                "stdout": None,
                "stderr": f"Error executing approved command: {e}",
            }

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
            metrics = self._get_metrics(host)
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

        evaluation = {
            "ram_high": ram_high,
            "cpu_high": cpu_high,
        }
        thresholds = {
            "ram_threshold": ram_threshold,
            "cpu_threshold": cpu_threshold,
        }

        if command and not dry_run and action in DANGEROUS_ACTIONS:
            approval_metadata = {
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
                exit_code, stdout_text, stderr_text = self._ssh_exec(host, command)
                executed = True
                # executed=True chỉ nghĩa là SSH chạy được lệnh (không exception Python).
                # command_succeeded phản ánh đúng kết quả của LỆNH TRÊN NODE (exit_code).
                command_succeeded = (exit_code == 0)
            except Exception as e:
                stderr_text = f"Error executing command: {e}"
                command_succeeded = False

        return SkillResult(
            status=SkillStatus.SUCCESS,
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
            },
        )
