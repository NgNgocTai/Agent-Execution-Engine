import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import settings
from services.skill_validation import inspect_archive
from services.sandbox_executor import execute_sandbox
from test_skill_safety import archive, MANIFEST


@unittest.skipUnless(os.environ.get("SANDBOX_TEST_IMAGE"), "Reviewed local sandbox image is not configured")
class SandboxIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_sandbox_is_nonroot_readonly_and_does_not_receive_host_secrets(self):
        code = '''import json, sys
os = sys.modules['os']
arguments = json.load(sys.stdin)
print(json.dumps({'status':'success','data':{'text':arguments['text'].upper(), 'uid':os.getuid(), 'root_writable':os.access('/',os.W_OK), 'host_secret':os.environ.get('SANDBOX_HOST_TEST_SECRET'), 'docker_socket':os.path.exists('/var/run/docker.sock'), 'network_interfaces':os.listdir('/sys/class/net')}}))
'''
        manifest, files, _ = inspect_archive(archive(code=code))
        with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]), patch.dict(os.environ, {"SANDBOX_HOST_TEST_SECRET": "host-only-sentinel"}):
            result = await execute_sandbox(manifest, files, {"text": "hello"})
        self.assertEqual(result.data, {"text": "HELLO", "uid": 65532, "root_writable": False, "host_secret": None, "docker_socket": False, "network_interfaces": ["lo"]})

    async def test_real_sandbox_deadline_stops_infinite_script(self):
        manifest, files, _ = inspect_archive(archive(code="while True: pass\n", manifest={**MANIFEST, "timeout_seconds": 1}))
        with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]):
            with self.assertRaises(TimeoutError):
                await execute_sandbox(manifest, files, {"text": "hello"})
