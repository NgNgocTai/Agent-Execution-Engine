import asyncio
import logging
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import paramiko

from connectors.ssh import SSHConnector, SSHExecutionError, SSHResult
from core.config import Settings
from services.skills.node_health_remediate import NodeHealthRemediateSkill
from services.skills.registry import SkillRegistry
from services.skills.base import SkillStatus
from services.tool_executor import ToolExecutor


class LocalSSHServer:
    """Loopback-only SSH protocol fixture. Commands are simulated, never shelled out."""

    def __init__(self, host_key, client_key=None):
        self.host_key = host_key
        self.client_key = client_key
        self.commands = []
        self.mode = "normal"
        self.auth_delay = 0
        self.stop = threading.Event()
        self.connections = []
        self.threads = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _serve(self, connection):
        if self.mode == "banner_hang":
            self.stop.wait(timeout=3)
            connection.close()
            return
        fixture = self
        command_ready = threading.Event()
        transport = paramiko.Transport(connection)
        self.connections.append(transport)
        transport.add_server_key(self.host_key)

        class Server(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                fixture.stop.wait(fixture.auth_delay)
                return paramiko.AUTH_SUCCESSFUL if (username, password) == ("test", "test-only") else paramiko.AUTH_FAILED

            def check_auth_publickey(self, username, key):
                return paramiko.AUTH_SUCCESSFUL if username == "test" and key == fixture.client_key else paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "password,publickey"

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_exec_request(self, channel, command):
                fixture.commands.append(command)
                command_ready.set()
                if command == b"no-ack":
                    fixture.stop.wait(timeout=3)
                return True

        try:
            transport.start_server(server=Server())
            channel = transport.accept(timeout=3)
            if channel is None or not command_ready.wait(timeout=3):
                return
            command = self.commands[-1]
            # Wait for client stdin EOF, proving the exec acknowledgement was
            # received before this fixture sends output and closes the channel.
            channel.settimeout(3)
            channel.recv(1)
            if command == b"large":
                channel.sendall(b"o" * (3 * 1024 * 1024))
                channel.sendall_stderr(b"e" * (3 * 1024 * 1024))
            elif command in {b"hang", b"partial"}:
                if command == b"partial":
                    channel.sendall(b"partial output")
                while transport.is_active() and not self.stop.wait(0.01):
                    pass
                return
            elif command == b"disconnect":
                return
            elif command == b"utf8":
                channel.sendall("kiểm tra\n".encode("utf-8"))
                channel.sendall_stderr("cảnh báo\n".encode("utf-8"))
            else:
                channel.sendall(b"ok")
            channel.send_exit_status(7 if command == b"fail" else 0)
            channel.shutdown_write()
            channel.close()
            # Keep the transport alive until the client has drained the channel.
            # Dropping TCP here races its final SSH window-adjust packets on Linux.
            while transport.is_active() and not self.stop.wait(0.01):
                pass
        except (EOFError, OSError, paramiko.SSHException):
            # Host-key rejection, cancellation and deadline tests close the peer.
            pass
        finally:
            transport.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=1)
        for transport in self.connections:
            transport.close()
        for thread in self.threads:
            thread.join(timeout=1)


class SSHConnectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host_key = paramiko.RSAKey.generate(2048)
        cls.client_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.enterContext(patch.object(logging.getLogger("paramiko.transport"), "level", logging.CRITICAL))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = LocalSSHServer(self.host_key, self.client_key)
        self.addCleanup(self.server.close)
        self.known_hosts = Path(self.directory.name, "known_hosts")
        self._trust(self.host_key)

    def _trust(self, key):
        self.known_hosts.write_text(
            f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n", encoding="ascii",
        )

    def _config(self, **changes):
        options = {
            "ssh_user": "test", "ssh_password": "test-only", "ssh_port": self.server.port,
            "ssh_known_hosts": str(self.known_hosts), "ssh_auto_add_host_keys": False,
            "ssh_key_path": "", "ssh_total_timeout_seconds": 4,
            "ssh_connect_timeout_seconds": 1, "ssh_auth_timeout_seconds": 1,
            "ssh_output_max_bytes": 1024,
        }
        return Settings(_env_file=None, **{**options, **changes})

    def test_verified_host_drains_both_streams_and_preserves_utf8(self):
        result = SSHConnector(self._config()).execute("127.0.0.1", "utf8")
        self.assertEqual(tuple(result), (0, "kiểm tra\n", "cảnh báo\n"))
        self.assertFalse(result.stdout_truncated or result.stderr_truncated)

    def test_large_output_exceeding_ssh_window_is_drained_with_bounded_storage(self):
        result = SSHConnector(self._config()).execute("127.0.0.1", "large")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "o" * 1024)
        self.assertEqual(result.stderr, "e" * 1024)
        self.assertTrue(result.stdout_truncated and result.stderr_truncated)

    def test_unknown_host_is_rejected_before_dispatch_without_updating_known_hosts(self):
        self.known_hosts.write_text("", encoding="ascii")
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config()).execute("127.0.0.1", "unused")
        self.assertEqual(raised.exception.code, "ssh_host_key")
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(self.server.commands, [])
        self.assertEqual(self.known_hosts.read_text(), "")

    def test_changed_host_key_is_rejected_even_with_development_opt_in(self):
        self._trust(self.client_key)
        with self.assertLogs("connectors.ssh", level="WARNING"):
            with self.assertRaises(SSHExecutionError) as raised:
                SSHConnector(self._config(ssh_auto_add_host_keys=True)).execute("127.0.0.1", "unused")
        self.assertEqual(raised.exception.code, "ssh_host_key")
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(self.server.commands, [])

    def test_development_opt_in_accepts_unknown_host_explicitly(self):
        self.known_hosts.write_text("", encoding="ascii")
        with self.assertLogs("connectors.ssh", level="WARNING"):
            result = SSHConnector(self._config(ssh_auto_add_host_keys=True)).execute("127.0.0.1", "ok")
        self.assertEqual(result.stdout, "ok")

    def test_authentication_failure_does_not_dispatch(self):
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config(ssh_password="wrong-test-password")).execute("127.0.0.1", "unused")
        self.assertEqual(raised.exception.code, "ssh_authentication")
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(self.server.commands, [])

    def test_banner_timeout_is_reported_before_dispatch(self):
        self.server.mode = "banner_hang"
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config(ssh_auth_timeout_seconds=0.1)).execute("127.0.0.1", "unused")
        self.assertEqual(raised.exception.code, "ssh_timeout")
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(self.server.commands, [])

    def test_authentication_timeout_is_reported_before_dispatch(self):
        self.server.auth_delay = 0.5
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config(ssh_auth_timeout_seconds=0.1)).execute("127.0.0.1", "unused")
        self.assertEqual(raised.exception.code, "ssh_timeout")
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(self.server.commands, [])

    def test_default_trust_store_is_loaded_and_failed_connection_is_closed(self):
        with patch("connectors.ssh.paramiko.SSHClient") as client_type:
            client = client_type.return_value
            client.connect.side_effect = TimeoutError()
            with self.assertRaises(SSHExecutionError) as raised:
                SSHConnector(self._config(ssh_known_hosts="")).execute("unused-host", "unused")
        self.assertEqual(raised.exception.code, "ssh_timeout")
        client.load_system_host_keys.assert_called_once()
        client.load_host_keys.assert_not_called()
        self.assertFalse(client.connect.call_args.kwargs["look_for_keys"])
        self.assertFalse(client.connect.call_args.kwargs["allow_agent"])
        client.close.assert_called()

    def test_configured_private_key_is_used(self):
        key_path = Path(self.directory.name, "client_key")
        self.client_key.write_private_key_file(str(key_path))
        result = SSHConnector(self._config(ssh_password="", ssh_key_path=str(key_path))).execute("127.0.0.1", "ok")
        self.assertEqual(result.exit_code, 0)

    def test_missing_known_hosts_file_fails_before_connecting(self):
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config(ssh_known_hosts=str(self.known_hosts) + ".missing")).execute("127.0.0.1", "unused")
        self.assertEqual(raised.exception.code, "ssh_configuration")
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(self.server.commands, [])

    def test_command_deadline_preserves_partial_output_and_marks_dispatch_uncertain(self):
        started = time.monotonic()
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config(ssh_total_timeout_seconds=0.5)).execute("127.0.0.1", "partial")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(raised.exception.code, "ssh_timeout")
        self.assertTrue(raised.exception.dispatched)
        self.assertEqual(raised.exception.stdout, "partial output")

    def test_missing_exec_acknowledgement_is_bounded_by_watchdog(self):
        started = time.monotonic()
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config(ssh_total_timeout_seconds=0.5)).execute("127.0.0.1", "no-ack")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(raised.exception.code, "ssh_timeout")
        self.assertTrue(raised.exception.dispatched)

    def test_connection_loss_without_exit_status_is_not_reported_as_known_failure(self):
        with self.assertRaises(SSHExecutionError) as raised:
            SSHConnector(self._config()).execute("127.0.0.1", "disconnect")
        self.assertTrue(raised.exception.dispatched)
        self.assertEqual(raised.exception.code, "ssh_connection")

    def test_nonzero_exit_code_is_retained(self):
        result = SSHConnector(self._config()).execute("127.0.0.1", "fail")
        self.assertEqual(result.exit_code, 7)


class NodeHealthSSHTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Explicitly scoped test deployment; production defaults deny writes.
        from core.config import settings
        self.enterContext(patch.object(settings, "ssh_allowed_hosts", "test-node,NOC-APP-01"))
        self.enterContext(patch.object(settings, "ssh_allowed_services", "test-service,noc-api"))

    async def _assert_nonblocking(self, block_metrics):
        skill = NodeHealthRemediateSkill()
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def blocked(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=2):
                raise TimeoutError("Test operation not released")
            return {"mem_pct": 95, "cpu_pct": 10} if block_metrics else (0, "logs", "")

        metrics = blocked if block_metrics else lambda host: {"mem_pct": 95, "cpu_pct": 10}
        with (
            patch.object(skill, "_get_metrics", side_effect=metrics),
            patch.object(skill, "_ssh_exec", side_effect=(lambda *args: (0, "logs", "")) if block_metrics else blocked) as ssh,
        ):
            task = asyncio.create_task(skill.execute(host="test-node", service="test-service", dry_run=False))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                self.assertFalse(task.done(), "Synchronous SSH blocked the event loop")
            finally:
                release.set()
                result = await task
        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertEqual(ssh.call_count, 1)

    async def test_metrics_io_does_not_block_the_event_loop(self):
        await self._assert_nonblocking(block_metrics=True)

    async def test_log_reading_does_not_block_the_event_loop(self):
        await self._assert_nonblocking(block_metrics=False)

    async def test_outer_tool_timeout_does_not_start_a_later_action(self):
        skill = NodeHealthRemediateSkill()
        registry = SkillRegistry()
        registry.register(skill)
        release = threading.Event()

        def slow_metrics(host):
            release.wait(timeout=2)
            return {"mem_pct": 95, "cpu_pct": 10}

        with (
            patch.object(skill, "_get_metrics", side_effect=slow_metrics),
            patch.object(skill, "_ssh_exec") as execute,
            patch("services.tool_executor.SKILL_TIMEOUT_SECONDS", 0.05),
        ):
            try:
                result = await ToolExecutor(registry).execute(
                    skill.name, {"host": "test-node", "service": "test-service", "dry_run": False},
                )
                self.assertEqual(result.status, SkillStatus.SYSTEM_ERROR)
                self.assertIn("timeout", result.error)
            finally:
                release.set()
            await asyncio.sleep(0.05)
            execute.assert_not_called()

    async def test_incomplete_metrics_are_not_used_for_remediation(self):
        skill = NodeHealthRemediateSkill()
        with patch.object(skill, "_ssh_exec", return_value=SSHResult(0, '{"mem_pct":', "", True)):
            result = await skill.execute(host="test-node", dry_run=False)
        self.assertEqual(result.status, SkillStatus.SYSTEM_ERROR)
        self.assertEqual(result.data["error_code"], "ssh_metrics")

    async def test_failed_log_command_is_reported_as_tool_error(self):
        skill = NodeHealthRemediateSkill()
        with (
            patch.object(skill, "_get_metrics", return_value={"mem_pct": 95, "cpu_pct": 10}),
            patch.object(skill, "_ssh_exec", return_value=SSHResult(1, "", "Log access denied")),
        ):
            result = await skill.execute(host="test-node", service="test-service", dry_run=False)
        self.assertEqual(result.status, SkillStatus.SYSTEM_ERROR)
        self.assertEqual(result.data["exit_code"], 1)
        self.assertIn("Log access denied", result.error)

    async def test_approved_command_distinguishes_predispatch_failure_from_uncertain_dispatch(self):
        skill = NodeHealthRemediateSkill()
        for dispatched in (False, True):
            with self.subTest(dispatched=dispatched):
                error = SSHExecutionError("Test timeout", code="ssh_timeout", dispatched=dispatched)
                with patch.object(skill, "_ssh_exec", side_effect=error):
                    result = await asyncio.to_thread(skill.execute_approved_command, "test-node", "test-command")
                self.assertEqual(result["execution_status"], "unknown" if dispatched else "not_started")
                self.assertEqual(result["error_code"], "ssh_timeout")
                self.assertIs(result["executed"], None if dispatched else False)


if __name__ == "__main__":
    unittest.main()
