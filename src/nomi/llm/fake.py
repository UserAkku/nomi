"""Deterministic stand-in for a real model, used by tests and CI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from nomi.llm.base import LLMError

T = TypeVar("T", bound=BaseModel)

Handler = Callable[[str, str, type[BaseModel]], Any]


class FakeLLM:
    """Answers from a handler function or a queue of canned responses.

    A response may be a dict (validated against the requested schema), a
    model instance, or an exception instance to raise.
    """

    def __init__(
        self,
        handler: Handler | None = None,
        *,
        responses: list[Any] | None = None,
        name: str = "fake-llm",
    ) -> None:
        self._handler = handler
        self._responses = list(responses or [])
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    def generate(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 1024) -> T:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema.__name__})
        if self._handler is not None:
            out = self._handler(system, prompt, schema)
        elif self._responses:
            out = self._responses.pop(0)
        else:
            raise LLMError("FakeLLM has no response configured")
        if isinstance(out, Exception):
            raise out
        if isinstance(out, schema):
            return out
        try:
            return schema.model_validate(out)
        except Exception as exc:  # mirror the real backend's contract
            raise LLMError(f"invalid structured output: {exc}") from exc
