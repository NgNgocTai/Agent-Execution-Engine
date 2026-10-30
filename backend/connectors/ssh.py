import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko

from core.config import settings as default_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SSHResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __iter__(self):
        # Existing skill implementations/test doubles use the three-value contract.
        yield self.exit_code
        yield self.stdout
        yield self.stderr


class SSHExecutionError(paramiko.SSHException):
    def __init__(
        self, message, *, code, dispatched=False,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
    ):
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_truncated = stdout_truncated
        self.stderr_truncated = stderr_truncated


class _RejectUnknownHostKey(paramiko.RejectPolicy):
    def missing_host_key(self, client, hostname, key):
        raise SSHExecutionError(
            "SSH host key is not trusted. Configure SSH_KNOWN_HOSTS with an independently verified key.",
            code="ssh_host_key",
        )


def _is_timeout(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError):
            return True
        if isinstance(error, paramiko.SSHException) and str(error) in {
            "Authentication timeout.", "Timeout opening channel.",
        }:
            return True
        error = error.__cause__ or error.__context__
    return False


class SSHConnector:
    def __init__(self, settings=default_settings):
        self.settings = settings

    def execute(self, host: str, command: str) -> SSHResult:
        """Run synchronously in a worker thread; drain both streams before exit status.

        The watchdog also covers Paramiko's exec acknowledgement, which does not
        obey Channel.settimeout(). Closing the client cannot revoke a remote
        side effect. OS hostname resolution can outlive the watchdog; the deadline
        is rechecked before dispatch so a late resolver cannot start the command.
        """
        config = self.settings
        client = paramiko.SSHClient()
        expired = threading.Event()
        deadline = time.monotonic() + config.ssh_total_timeout_seconds
        dispatched = False
        stdout = bytearray()
        stderr = bytearray()
        stdout_truncated = False
        stderr_truncated = False

        def close_client():
            try:
                client.close()
            except Exception:
                # Cleanup must not replace the command's result or primary error.
                logger.debug("SSH client cleanup failed", exc_info=True)

        def timeout():
            expired.set()
            close_client()

        def remaining():
            budget = deadline - time.monotonic()
            if expired.is_set() or budget <= 0:
                raise TimeoutError("SSH operation deadline exceeded")
            return budget

        timer = threading.Timer(config.ssh_total_timeout_seconds, timeout)
        timer.daemon = True
        timer.start()
        try:
            if config.ssh_known_hosts:
                client.load_host_keys(str(Path(config.ssh_known_hosts).expanduser()))
            else:
                client.load_system_host_keys()
            if config.ssh_auto_add_host_keys:
                logger.warning("SSH automatic host-key acceptance explicitly enabled for development")
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            else:
                client.set_missing_host_key_policy(_RejectUnknownHostKey())

            key_path = str(Path(config.ssh_key_path).expanduser()) if config.ssh_key_path else None
            if key_path and not Path(key_path).is_file():
                raise FileNotFoundError("SSH_KEY_PATH does not point to a readable private key")
            client.connect(
                hostname=host,
                port=config.ssh_port,
                username=config.ssh_user,
                password=config.ssh_password or None,
                key_filename=key_path,
                timeout=min(config.ssh_connect_timeout_seconds, remaining()),
                banner_timeout=min(config.ssh_auth_timeout_seconds, remaining()),
                auth_timeout=min(config.ssh_auth_timeout_seconds, remaining()),
                channel_timeout=remaining(),
                look_for_keys=False,
                allow_agent=False,
            )
            remaining()
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise paramiko.SSHException("SSH transport is not active")
            channel = transport.open_session(timeout=remaining())
            channel.settimeout(remaining())
            remaining()
            # Even an interrupted exec acknowledgement may have reached the server.
            dispatched = True
            channel.exec_command(command)
            remaining()
            channel.shutdown_write()

            while True:
                remaining()
                received = False
                # Read one bounded chunk from each stream per iteration. A busy
                # stdout must not starve stderr or delay deadline checks.
                if channel.recv_ready():
                    chunk = channel.recv(32768)
                    capacity = max(0, config.ssh_output_max_bytes - len(stdout))
                    stdout.extend(chunk[:capacity])
                    stdout_truncated |= len(chunk) > capacity
                    received = bool(chunk)
                if channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(32768)
                    capacity = max(0, config.ssh_output_max_bytes - len(stderr))
                    stderr.extend(chunk[:capacity])
                    stderr_truncated |= len(chunk) > capacity
                    received |= bool(chunk)

                drained = not channel.recv_ready() and not channel.recv_stderr_ready()
                if drained and channel.exit_status_ready() and (channel.eof_received or channel.closed):
                    exit_code = channel.recv_exit_status()
                    if exit_code < 0:
                        raise paramiko.SSHException("SSH channel closed without an exit status")
                    remaining()
                    return SSHResult(
                        exit_code, stdout.decode("utf-8", errors="replace"),
                        stderr.decode("utf-8", errors="replace"),
                        stdout_truncated, stderr_truncated,
                    )
                if not received:
                    if not transport.is_active() and drained:
                        raise paramiko.SSHException("SSH connection lost before command completion")
                    expired.wait(min(0.01, remaining()))
        except Exception as exc:
            if expired.is_set() or time.monotonic() >= deadline or _is_timeout(exc):
                code = "ssh_timeout"
                message = f"SSH operation timed out (total budget: {config.ssh_total_timeout_seconds:g}s)"
            elif isinstance(exc, SSHExecutionError):
                code, message = exc.code, str(exc)
            elif isinstance(exc, paramiko.BadHostKeyException):
                code = "ssh_host_key"
                message = "SSH host key changed; verify the server key before updating SSH_KNOWN_HOSTS"
            elif isinstance(exc, paramiko.AuthenticationException):
                code = "ssh_authentication"
                message = "SSH authentication failed; check SSH_USER, SSH_PASSWORD or SSH_KEY_PATH"
            elif isinstance(exc, (FileNotFoundError, PermissionError)):
                code = "ssh_configuration"
                message = "Unable to read SSH_KNOWN_HOSTS or SSH_KEY_PATH"
            else:
                code = "ssh_connection"
                message = "SSH connection or command channel failed"
            raise SSHExecutionError(
                message, code=code, dispatched=dispatched,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated,
            ) from exc
        finally:
            timer.cancel()
            close_client()
