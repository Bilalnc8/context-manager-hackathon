"""LLM access behind a small interface so tests can replay recorded responses."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from emailctx.config import Settings, Task

M = TypeVar("M", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not produce a usable structured response."""


class LLMClient(Protocol):
    def structured(self, task: Task, system: str, prompt: str, schema: type[M]) -> M: ...


class AnthropicLLM:
    """Structured-output calls through the Anthropic SDK. Models come from config."""

    _FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        import anthropic

        self._settings = settings
        self._client = client or anthropic.Anthropic()

    def structured(self, task: Task, system: str, prompt: str, schema: type[M]) -> M:
        import anthropic

        s = self._settings
        kwargs: dict[str, Any] = dict(
            model=s.models.for_task(task),
            max_tokens=s.max_tokens.for_task(task),
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": s.effort.for_task(task)},
            output_format=schema,
        )
        fallbacks = s.fallbacks.for_task(task)
        try:
            if fallbacks:
                response = self._client.beta.messages.parse(
                    betas=[self._FALLBACK_BETA], fallbacks=fallbacks, **kwargs
                )
            else:
                response = self._client.messages.parse(**kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"{task}: API error: {e}") from e

        if response.stop_reason == "refusal":
            raise LLMError(f"{task}: model refused")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{task}: hit max_tokens")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"{task}: no parsed output")
        return parsed


class RecordedLLM:
    """Replays recorded JSON responses per task, in order. For tests.

    Each response is validated against the requested schema exactly as a live
    response would be. Running out of responses raises, so tests catch unexpected calls.
    """

    def __init__(self, responses: Mapping[str, Iterable[Any]]) -> None:
        self._queues = {task: deque(items) for task, items in responses.items()}
        self.calls: list[tuple[str, str]] = []  # (task, prompt)

    @classmethod
    def from_file(cls, path: Path) -> RecordedLLM:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def structured(self, task: Task, system: str, prompt: str, schema: type[M]) -> M:
        self.calls.append((task, prompt))
        queue = self._queues.get(task)
        if not queue:
            raise AssertionError(f"unexpected {task} call: no recorded response left")
        return schema.model_validate(queue.popleft())

    def remaining(self) -> dict[str, int]:
        return {task: len(q) for task, q in self._queues.items() if q}
