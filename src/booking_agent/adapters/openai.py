"""OpenAI-compatible model adapter. Install booking-agent[openai]."""

from __future__ import annotations

import json
import time
from typing import Any

from openai import APIError, OpenAI

from booking_agent.model import ChatError, ChatResponse, ToolCall


class OpenAIChatModel:
    """Any OpenAI-compatible chat completions API with tool calling."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        reasoning_effort: str | None = "none",
        price_in_per_m: float = 0.0,
        price_out_per_m: float = 0.0,
        timeout_s: float = 30.0,
    ) -> None:
        self._config: dict[str, Any] = dict(
            api_key=api_key,
            base_url=base_url,
            price_in_per_m=price_in_per_m,
            price_out_per_m=price_out_per_m,
            timeout_s=timeout_s,
        )
        self.model_name = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=1)
        self._extra: dict[str, Any] = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        self._price = (price_in_per_m, price_out_per_m)

    def with_model(self, model: str, reasoning_effort: str | None) -> OpenAIChatModel:
        return OpenAIChatModel(model=model, reasoning_effort=reasoning_effort, **self._config)

    def close(self) -> None:
        self._client.close()

    def respond(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        started = time.perf_counter()
        try:
            create: Any = self._client.chat.completions.create
            completion = create(
                model=self.model_name,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                **self._extra,
            )
        except APIError as error:
            raise ChatError(type(error).__name__) from error
        message = completion.choices[0].message
        calls = []
        for call in message.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {"_invalid_json": True}
            except json.JSONDecodeError:
                args = {"_invalid_json": True}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=args))
        tokens_in = completion.usage.prompt_tokens if completion.usage else 0
        tokens_out = completion.usage.completion_tokens if completion.usage else 0
        return ChatResponse(
            text=message.content,
            tool_calls=tuple(calls),
            model=self.model_name,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=round((tokens_in * self._price[0] + tokens_out * self._price[1]) / 1e6, 6),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
