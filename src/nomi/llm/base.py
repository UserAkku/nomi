"""The narrow interface every language-model backend implements.

Nomi only ever asks a model for *structured* output validated against a
pydantic schema, so callers never parse free text.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model was unreachable or could not produce a valid structured answer."""


class LLM(Protocol):
    @property
    def name(self) -> str:
        """Identifier recorded in reports, e.g. 'qwen3:4b-instruct-2507-q4_K_M'."""
        ...

    def generate(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 1024) -> T:
        """Return an instance of `schema` or raise LLMError."""
        ...
