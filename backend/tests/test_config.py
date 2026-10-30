import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from pydantic import ValidationError

from core.config import Settings


class SettingsTest(unittest.TestCase):
    def test_shared_dotenv_accepts_evaluation_only_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory, ".env")
            dotenv.write_text("RAGAS_EVAL_MODEL=test-evaluator\n", encoding="utf-8")
            settings = Settings(_env_file=dotenv)
        self.assertNotIn("ragas_eval_model", settings.model_dump())

    def test_known_fields_still_validate_types(self):
        with self.assertRaises(ValidationError) as raised:
            Settings(_env_file=None, ssh_port="not-a-port")
        self.assertIn("ssh_port", [error["loc"][0] for error in raised.exception.errors()])

    def test_ssh_resource_limits_reject_invalid_values(self):
        for field, value in (
            ("ssh_port", 0), ("ssh_port", 65536),
            ("ssh_connect_timeout_seconds", 0), ("ssh_auth_timeout_seconds", -1),
            ("ssh_total_timeout_seconds", 0), ("ssh_output_max_bytes", 0),
            ("ssh_output_max_bytes", 1048577),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    Settings(_env_file=None, **{field: value})

    def test_unknown_ssh_hosts_are_not_automatically_trusted_by_default(self):
        self.assertIs(Settings.model_fields["ssh_auto_add_host_keys"].default, False)


if __name__ == "__main__":
    unittest.main()
