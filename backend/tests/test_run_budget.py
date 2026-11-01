from pathlib import Path
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import settings
from core.schemas import NormalizedMessage
from services.run_budget import RunBudget, BudgetExceeded


class BudgetTest(unittest.TestCase):
    def test_token_limit_and_missing_price_block_before_provider_dispatch(self):
        messages, provider = [NormalizedMessage(role="user", content="hello")], SimpleNamespace(model_name="fixture")
        with patch.object(settings, "llm_run_token_limit", 1024):
            with self.assertRaises(BudgetExceeded) as raised: RunBudget().reserve(provider, messages, [])
            self.assertEqual(raised.exception.code, "RUN_TOKEN_BUDGET_EXCEEDED")
        with patch.object(settings, "llm_run_cost_limit", 1.0), patch.object(settings, "llm_model_prices_json", "{}"):
            with self.assertRaises(BudgetExceeded) as raised: RunBudget().reserve(provider, messages, [])
            self.assertEqual(raised.exception.code, "MODEL_PRICE_UNCONFIGURED")

    def test_configured_estimates_and_checkpoint_accounting_are_preserved(self):
        prices = json.dumps({"fixture": {"input_per_million": 0.1, "output_per_million": 0.2}})
        with patch.object(settings, "llm_model_prices_json", prices), patch.object(settings, "llm_max_attempts", 2):
            budget = RunBudget()
            budget.reserve(SimpleNamespace(model_name="fixture"), [NormalizedMessage(role="user", content="hello")], [])
            budget.record({"input": 100, "output": 20}, "answer", "", [])
            snapshot = budget.snapshot()
            self.assertEqual(snapshot["tokens"], 240)
            self.assertAlmostEqual(snapshot["estimated_cost"], .000028)
            self.assertEqual(RunBudget(snapshot).tokens, 240)

    def test_unknown_historical_cost_cannot_be_reset_to_zero_on_resume(self):
        with patch.object(settings, "llm_run_cost_limit", 1.0):
            with self.assertRaises(BudgetExceeded) as raised:
                RunBudget({"tokens": 100, "estimated_cost": None}).reserve(SimpleNamespace(model_name="fixture"), [NormalizedMessage(role="user", content="hello")], [])
            self.assertEqual(raised.exception.code, "RUN_COST_HISTORY_UNAVAILABLE")
