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


if __name__ == "__main__":
    unittest.main()
