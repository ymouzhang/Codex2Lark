from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Protocol, cast

from codex2lark.runtime.types import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)


class ResponsesAPI(Protocol):
    async def create(self, **parameters: Any) -> object: ...


class OpenAIClientPort(Protocol):
    responses: ResponsesAPI


class OpenAIResponsesModel:
    def __init__(
        self,
        client: OpenAIClientPort,
        *,
        input_cost_micros_per_million_tokens: int,
        output_cost_micros_per_million_tokens: int,
    ) -> None:
        if (
            min(
                input_cost_micros_per_million_tokens,
                output_cost_micros_per_million_tokens,
            )
            < 1
        ):
            raise ValueError("model token prices must be positive")
        self._client = client
        self._input_price = input_cost_micros_per_million_tokens
        self._output_price = output_cost_micros_per_million_tokens

    @classmethod
    def from_api_key(
        cls,
        *,
        api_key: str,
        input_cost_micros_per_million_tokens: int,
        output_cost_micros_per_million_tokens: int,
        base_url: str | None = None,
    ) -> OpenAIResponsesModel:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return cls(
            cast(OpenAIClientPort, client),
            input_cost_micros_per_million_tokens=(input_cost_micros_per_million_tokens),
            output_cost_micros_per_million_tokens=(output_cost_micros_per_million_tokens),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        remaining = request.remaining_budget.get("model_tokens", 16_384)
        if remaining < 1:
            raise ValueError("model token budget is exhausted")
        provider_names = {
            self._provider_tool_name(definition.tool_id): definition.tool_id
            for definition in request.tools
        }
        response = await self._client.responses.create(
            model=request.model_profile,
            input=self._input(request.messages),
            tools=[
                {
                    "type": "function",
                    "name": self._provider_tool_name(definition.tool_id),
                    "description": definition.description,
                    "parameters": definition.input_schema,
                    "strict": True,
                }
                for definition in request.tools
            ],
            tool_choice="auto" if request.tools else "none",
            parallel_tool_calls=True,
            max_output_tokens=min(16_384, remaining),
            metadata={"run_id": request.run_id, "node_id": request.node_id},
            store=False,
        )
        calls: list[ToolCall] = []
        for item in getattr(response, "output", ()):
            if getattr(item, "type", None) != "function_call":
                continue
            arguments = json.loads(getattr(item, "arguments", "{}"))
            if not isinstance(arguments, dict):
                raise ValueError("OpenAI function arguments must be an object")
            call_id = item.call_id
            provider_name = item.name
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("OpenAI function call is missing call_id")
            if not isinstance(provider_name, str) or not provider_name:
                raise ValueError("OpenAI function call is missing name")
            tool_id = provider_names.get(provider_name)
            if tool_id is None:
                raise ValueError("OpenAI returned an unknown function name")
            calls.append(
                ToolCall(
                    call_id=call_id,
                    tool_id=tool_id,
                    arguments=arguments,
                )
            )
        usage = getattr(response, "usage", None)
        if usage is None:
            raise ValueError("OpenAI response is missing token usage")
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if min(input_tokens, output_tokens) < 0:
            raise ValueError("OpenAI response token usage cannot be negative")
        weighted_cost = input_tokens * self._input_price + output_tokens * self._output_price
        cost_micros = (weighted_cost + 999_999) // 1_000_000 if weighted_cost else 0
        return ModelResponse(
            content=str(getattr(response, "output_text", "") or ""),
            tool_calls=tuple(calls),
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=cost_micros,
            ),
            provider_response_id=self._optional_text(getattr(response, "id", None)),
        )

    @staticmethod
    def _input(messages: tuple[ModelMessage, ...]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for message in messages:
            if message.role is MessageRole.TOOL:
                if not message.tool_call_id:
                    raise ValueError("tool result requires a call ID")
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content,
                    }
                )
                continue
            if message.content:
                result.append(
                    {
                        "type": "message",
                        "role": message.role.value,
                        "content": message.content,
                    }
                )
            for call in message.tool_calls:
                result.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": OpenAIResponsesModel._provider_tool_name(call.tool_id),
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    }
                )
        return result

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _provider_tool_name(tool_id: str) -> str:
        return f"c2l_{sha256(tool_id.encode('utf-8')).hexdigest()[:32]}"
