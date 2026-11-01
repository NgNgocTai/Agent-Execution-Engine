from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.history import build_messages


class HistoryFormatTest(unittest.TestCase):
    def test_native_openai_and_normalized_calls_restore_to_one_contract(self):
        for call in ({"id": "call", "function": {"name": "inventory", "arguments": '{"device_id":"one"}'}},
                     {"call_id": "call", "tool_name": "inventory", "arguments": {"device_id": "one"}}):
            messages = build_messages([{"id": 17, "role": "assistant", "tool_calls": [call]}], "next", "system")
            self.assertEqual(messages[1].tool_calls[0].arguments, {"device_id": "one"})
            self.assertEqual(messages[1].tool_calls[0].tool_name, "inventory")
            self.assertEqual(messages[1].source_id, "17")
