from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.schemas import NormalizedMessage
from services.providers.openai_provider import OpenAIProvider
from services.providers.deepseek_provider import DeepSeekProvider
from services.providers.anthropic_provider import AnthropicProvider


class UsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_usage_only_final_chunk_is_not_discarded(self):
        async def stream():
            yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))
        for provider_type in (OpenAIProvider, DeepSeekProvider, AnthropicProvider):
            with self.subTest(provider=provider_type.__name__):
                provider = object.__new__(provider_type)
                provider.model_name = "fixture-model"
                provider._use_openai_format = True
                provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=stream()))))
                responses = [item async for item in provider.chat_completion([NormalizedMessage(role="user", content="hello")], [])]
                self.assertEqual([item.token_usage for item in responses if item.token_usage], [{"input": 7, "output": 3}])
