from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import settings, Settings
from services.providers.anthropic_provider import AnthropicProvider


class ProtocolTest(unittest.TestCase):
    def test_protocol_selection_and_native_endpoint(self):
        for url, mode, expected in [
            ("https://api.anthropic.com", "auto", "anthropic"),
            ("", "auto", "anthropic"),
            ("https://router.test/v1", "auto", "openai_compatible"),
            ("https://native.test", "anthropic", "anthropic"),
            ("https://api.anthropic.com", "openai_compatible", "openai_compatible"),
        ]:
            config = Settings(
                _env_file=None, anthropic_base_url=url, anthropic_protocol=mode
            )
            self.assertEqual(config.resolved_anthropic_protocol, expected)
            with (
                patch.object(settings, "anthropic_base_url", url),
                patch.object(settings, "anthropic_protocol", mode),
                patch("openai.AsyncOpenAI") as compatible,
                patch("anthropic.AsyncAnthropic") as native,
            ):
                provider = AnthropicProvider()
                self.assertEqual(
                    provider._use_openai_format, expected == "openai_compatible"
                )
                if expected == "anthropic":
                    native.assert_called_once()
                    self.assertEqual(native.call_args.kwargs["base_url"], url or None)
                    compatible.assert_not_called()
