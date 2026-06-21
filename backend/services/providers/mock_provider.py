import json
from typing import AsyncGenerator

from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from services.providers.base import LLMProvider
from services.skills.base import ToolSchema


class MockProvider(LLMProvider):
    def __init__(self, scenario: str = "single_tool") -> None:
        self.scenario = scenario
        self.call_count = 0

    async def chat_completion(
        self,
        messages: list[NormalizedMessage],
        tools: list[ToolSchema],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]:
        if self.call_count == 0:
            if self.scenario == "multi_tool":
                yield NormalizedResponse(
                    tool_calls=[
                        NormalizedToolCall(
                            call_id="mock_call_1",
                            tool_name="noc_alarm_enrichment",
                            arguments={"alarm_type": "LINK_DOWN", "window_min": 10},
                        ),
                        NormalizedToolCall(
                            call_id="mock_call_2",
                            tool_name="node_health_remediate",
                            arguments={"host": "10.211.140.16", "ram_threshold": 85, "cpu_threshold": 80},
                        ),
                    ],
                    stop_reason="tool_calls",
                )
            else:
                yield NormalizedResponse(
                    tool_calls=[
                        NormalizedToolCall(
                            call_id="mock_call_1",
                            tool_name="noc_alarm_enrichment",
                            arguments={"alarm_type": "LINK_DOWN", "window_min": 10},
                        ),
                    ],
                    stop_reason="tool_calls",
                )
        else:
            yield NormalizedResponse(
                text_content="Kết quả kiểm tra: node hoạt động bình thường.",
                stop_reason="end_turn",
            )

        self.call_count += 1
