import logging

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
                "dry_run": {
                    "type": "boolean",
                    "description": "CẢNH BÁO QUAN TRỌNG: LUÔN LUÔN đặt dry_run = True. TUYỆT ĐỐI KHÔNG ĐƯỢC set bằng False khi chưa được người dùng yêu cầu rõ ràng. True = chỉ in lệnh (An toàn), False = thực thi thật (Nguy hiểm).",
                    "default": True,
                },
            },
            "required": ["host"],
        }

    def _ssh_exec(self, host: str, command: str) -> tuple[int, str]:
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
            output = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            if err:
                logger.warning("SSH stderr [%s]: %s", host, err)
            return exit_code, output
        finally:
            client.close()

    def _get_metrics(self, host: str) -> dict:
        import json

        _, output = self._ssh_exec(host, METRICS_SCRIPT)
        return json.loads(output)

    async def execute(
        self,
        host: str,
        ram_threshold: int = 80,
        cpu_threshold: int = 80,
        service: str = "",
        dry_run: bool = True,
        **kwargs,
    ) -> SkillResult:
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
            command = f"docker logs --timestamps --tail 200 {service}"
        elif not ram_high and cpu_high:
            if not service:
                return SkillResult(
                    status=SkillStatus.BUSINESS_ERROR,
                    data=None,
                    error="Action 'restart_service' requires 'service' parameter (docker container name). Please ask the user.",
                )
            action = "restart_service"
            command = f"docker restart {service}"
        else:
            action = "none"
            command = None

        executed = False
        execution_output = None

        if command and not dry_run:
            try:
                exit_code, output = self._ssh_exec(host, command)
                executed = True
                execution_output = output
            except Exception as e:
                execution_output = f"Error executing command: {e}"

        evaluation = {
            "ram_high": ram_high,
            "cpu_high": cpu_high,
            "ram_threshold": ram_threshold,
            "cpu_threshold": cpu_threshold,
        }

        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={
                "host": host,
                "metrics": metrics,
                "evaluation": evaluation,
                "action": action,
                "command": command,
                "dry_run": dry_run,
                "executed": executed,
                "execution_output": execution_output,
            },
        )
