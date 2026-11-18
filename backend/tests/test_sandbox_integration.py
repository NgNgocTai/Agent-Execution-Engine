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
    async def test_markdown_scripts_receive_readonly_args_json_and_typed_outputs(self):
        from test_skill_pipeline import markdown_package
        from services.skill_pipeline import analyze
        runtime = {"scripts": [{"entrypoint": "scripts/one.py", "output_contract": "json", "input_fields": [{"name": "text", "kind": "string"}], "sample_input": {"text": "alarm"}},
                               {"entrypoint": "scripts/two.py", "output_contract": "text"}]}
        payload = markdown_package({"runtime.json": json.dumps(runtime),
            "scripts/one.py": "import json\nwith open('args.json') as f:\n print(json.dumps({'received':json.load(f)['text']}))\n",
            "scripts/two.py": "print('alarm summary ready')\n"})
        manifest, files, digest = inspect_archive(payload)
        with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]):
            _, report = await analyze(manifest, files, digest, runner=execute_sandbox)
        self.assertTrue(report["ready_for_review"])
        self.assertEqual(report["scripts"][0]["result"]["data"], {"received": "alarm"})
        self.assertEqual(report["scripts"][1]["result"]["data"].strip(), "alarm summary ready")

    async def test_real_sandbox_is_nonroot_readonly_and_does_not_receive_host_secrets(self):
        code = '''import json, sys
os = sys.modules['os']
arguments = json.load(sys.stdin)
print(json.dumps({'status':'success','data':{'text':arguments['text'].upper(), 'uid':os.getuid(), 'root_writable':os.access('/',os.W_OK), 'host_secret':os.environ.get('SANDBOX_HOST_TEST_SECRET'), 'docker_socket':os.path.exists('/var/run/docker.sock'), 'network_interfaces':os.listdir('/sys/class/net')}}))
'''
        # Deliberately bypass the AST gate here to test the actual Docker
        # isolation boundary even when a static analyzer misses hostile code.
        manifest, files, _ = inspect_archive(archive())
        files[manifest.entrypoint] = code
        with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]), patch.dict(os.environ, {"SANDBOX_HOST_TEST_SECRET": "host-only-sentinel"}):
            result = await execute_sandbox(manifest, files, {"text": "hello"})
        self.assertEqual(result.data, {"text": "HELLO", "uid": 65532, "root_writable": False, "host_secret": None, "docker_socket": False, "network_interfaces": ["lo"]})

    async def test_real_sandbox_deadline_stops_infinite_script(self):
        manifest, files, _ = inspect_archive(archive(code="while True: pass\n", manifest={**MANIFEST, "timeout_seconds": 1}))
        with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]):
            with self.assertRaises(TimeoutError):
                await execute_sandbox(manifest, files, {"text": "hello"})

    async def test_structured_records_and_output_schema_run_through_sandbox(self):
        from test_skill_pipeline import markdown_package
        from services.skill_pipeline import analyze
        record = {"type": "object", "properties": {"cell": {"type": "string"}, "drop_rate": {"type": "number", "minimum": 0, "maximum": 100}}, "required": ["cell", "drop_rate"]}
        summary = {"type": "object", "properties": {"worst": {"type": "string"}, "count": {"type": "integer"}}, "required": ["worst", "count"]}

        def package(code):
            runtime = {"scripts": [{"entrypoint": "scripts/triage.py", "output_contract": "json",
                "input_fields": [{"name": "records", "kind": "array", "shape": {"type": "array", "items": record, "maxItems": 500}}],
                "sample_input": {"records": [{"cell": "a", "drop_rate": 1.5}, {"cell": "b", "drop_rate": 9}]},
                "output_schema": summary}]}
            return markdown_package({"runtime.json": json.dumps(runtime), "scripts/triage.py": code})

        good = "import json\nrecords = json.load(open('args.json'))['records']\nworst = max(records, key=lambda r: r['drop_rate'])\nprint(json.dumps({'worst': worst['cell'], 'count': len(records)}))\n"
        wrong_shape = "import json\nprint(json.dumps({'worst': 7, 'count': 'two', 'debug': True}))\n"
        with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]):
            manifest, files, digest = inspect_archive(package(good))
            _, report = await analyze(manifest, files, digest, runner=execute_sandbox)
            self.assertTrue(report["ready_for_review"])
            self.assertEqual(report["scripts"][0]["result"]["data"], {"worst": "b", "count": 2})
            self.assertEqual(report["scripts"][0]["output_schema"], summary)
            big = {"records": [{"cell": f"c{i}", "drop_rate": i % 100} for i in range(400)]}
            result = await execute_sandbox(manifest.run_specs()[0], files, big)
            self.assertEqual(result.data, {"worst": "c99", "count": 400})

            manifest, files, digest = inspect_archive(package(wrong_shape))
            _, report = await analyze(manifest, files, digest, runner=execute_sandbox)
            self.assertFalse(report["ready_for_review"])
            self.assertEqual(report["scripts"][0]["status"], "failed")
            with self.assertRaises(ValueError):
                await execute_sandbox(manifest.run_specs()[0], files, {"records": [{"cell": "a", "drop_rate": 1}]})
