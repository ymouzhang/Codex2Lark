from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from codex2lark.adapters.openai_responses import OpenAIResponsesModel
from codex2lark.runtime.types import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ToolCall,
    ToolDefinition,
    ToolEffect,
)


class FakeResponses:
    def __init__(self) -> None:
        self.parameters: dict[str, Any] = {}

    async def create(self, **parameters: Any) -> object:
        self.parameters = parameters
        return SimpleNamespace(
            id="resp_1",
            output_text="I will use the document tool.",
            output=(
                SimpleNamespace(
                    type="function_call",
                    call_id="call_2",
                    name=parameters["tools"][0]["name"],
                    arguments='{"title":"Architecture"}',
                ),
            ),
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )


class FakeModels:
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def retrieve(self, model: str) -> object:
        self.requested.append(model)
        return SimpleNamespace(id=model)


async def test_openai_responses_adapter_is_stateless_strict_and_preserves_calls() -> None:
    responses = FakeResponses()
    model = OpenAIResponsesModel(
        SimpleNamespace(responses=responses),
        input_cost_micros_per_million_tokens=1_250_000,
        output_cost_micros_per_million_tokens=10_000_000,
    )
    request = ModelRequest(
        run_id="run-1",
        node_id="/root",
        model_profile="configured-model",
        messages=(
            ModelMessage(MessageRole.SYSTEM, "Trusted policy", trusted=True),
            ModelMessage(
                MessageRole.ASSISTANT,
                "",
                tool_calls=(ToolCall("call_1", "docs.inspect", {"token": "docx_1"}),),
            ),
            ModelMessage(
                MessageRole.TOOL,
                '{"title":"Existing"}',
                name="docs.inspect",
                tool_call_id="call_1",
            ),
        ),
        tools=(
            ToolDefinition(
                "docs.create",
                1,
                "Create one document",
                {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
                ToolEffect.WRITE,
            ),
        ),
        remaining_budget={"tool_calls": 4},
    )

    result = await model.complete(request)

    assert result.provider_response_id == "resp_1"
    assert result.usage.input_tokens == 100
    assert result.usage.cost_micros == 325
    assert result.tool_calls == (ToolCall("call_2", "docs.create", {"title": "Architecture"}),)
    assert responses.parameters["store"] is False
    assert responses.parameters["model"] == "configured-model"
    assert responses.parameters["tools"][0]["strict"] is True
    assert responses.parameters["tools"][0]["name"].startswith("c2l_")
    assert "." not in responses.parameters["tools"][0]["name"]
    assert responses.parameters["input"][1]["name"].startswith("c2l_")
    assert responses.parameters["input"][1]["call_id"] == "call_1"
    assert responses.parameters["input"][2]["type"] == "function_call_output"
    assert "max_output_tokens" not in responses.parameters


async def test_openai_provider_health_uses_metadata_without_creating_response() -> None:
    responses = FakeResponses()
    models = FakeModels()
    model = OpenAIResponsesModel(
        SimpleNamespace(responses=responses, models=models),
        input_cost_micros_per_million_tokens=1,
        output_cost_micros_per_million_tokens=1,
    )

    await model.check_health("configured-model")

    assert models.requested == ["configured-model"]
    assert responses.parameters == {}


async def test_openai_responses_adapter_rejects_missing_billable_usage() -> None:
    responses = FakeResponses()

    async def missing_usage(**parameters: Any) -> object:
        del parameters
        return SimpleNamespace(id="resp_2", output=(), output_text="Done.")

    responses.create = missing_usage  # type: ignore[method-assign]
    model = OpenAIResponsesModel(
        SimpleNamespace(responses=responses),
        input_cost_micros_per_million_tokens=1,
        output_cost_micros_per_million_tokens=1,
    )

    with pytest.raises(ValueError, match="missing token usage"):
        await model.complete(ModelRequest("run", "/root", "model", (), (), {}))
