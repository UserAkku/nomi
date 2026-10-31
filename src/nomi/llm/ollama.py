"""Local language-model backend talking to an Ollama server.

Nomi runs one small open-weight model on the maintainer's own machine, so the
backend is built around what such a model needs to be dependable:

* every request is grammar-constrained (`format` = the pydantic JSON schema)
  and decoded greedily with a fixed seed, so answers are structured and
  repeatable;
* a malformed answer gets exactly one corrective retry before giving up, and
  every failure surfaces as `LLMError` with a fix the user can act on;
* validated answers can be cached on disk, keyed by everything that affects
  the output, which makes evaluation runs reproducible and resumable.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from nomi.llm.base import LLMError

T = TypeVar("T", bound=BaseModel)

DEFAULT_HOST = "http://localhost:11434"
KEEP_ALIVE = "15m"
_HEALTH_TIMEOUT_S = 10.0
_MAX_ERROR_CHARS = 400

_THINK_PREFIX = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_CODE_FENCE = re.compile(r"^\s*```[\w-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def normalize_host(host: str) -> str:
    """Accept the forms OLLAMA_HOST is written in (`host:port`, URLs with a slash)."""
    value = (host or DEFAULT_HOST).strip().rstrip("/")
    if not value:
        return DEFAULT_HOST
    if "://" not in value:
        value = f"http://{value}"
    return value


def clean_content(content: str) -> str:
    """Drop a leading <think> block and a wrapping code fence from a model answer."""
    text = _THINK_PREFIX.sub("", content or "", count=1)
    fenced = _CODE_FENCE.match(text)
    return (fenced.group(1) if fenced else text).strip()


def _short(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return _short(response.text) or response.reason_phrase
    if isinstance(data, dict) and data.get("error"):
        return _short(str(data["error"]))
    return _short(response.text) or response.reason_phrase


def _number(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


class OllamaLLM:
    """`nomi.llm.base.LLM` implementation backed by Ollama's /api/chat."""

    def __init__(
        self,
        model: str,
        host: str = DEFAULT_HOST,
        *,
        num_ctx: int = 8192,
        timeout_s: float = 240.0,
        cache_dir: Path | None = None,
        temperature: float = 0.0,
        seed: int = 42,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.host = normalize_host(host)
        self.num_ctx = num_ctx
        self.timeout_s = timeout_s
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.temperature = temperature
        self.seed = seed
        self.stats: dict[str, float] = {
            "calls": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "seconds": 0.0,
        }
        self._client = httpx.Client(
            base_url=self.host,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0)),
            transport=transport,
        )

    @property
    def name(self) -> str:
        return self.model

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ generation

    def generate(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 1024) -> T:
        """Return a validated `schema` instance; one corrective retry, else LLMError."""
        schema_json = schema.model_json_schema()
        key = self._cache_key(system, prompt, schema_json, max_tokens)
        cached = self._cache_read(key, schema)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached

        user = prompt
        problem = ""
        for _attempt in range(2):
            content = self._chat(system, user, schema_json, max_tokens)
            try:
                result = schema.model_validate(json.loads(clean_content(content)))
            except ValueError as exc:  # JSONDecodeError and ValidationError both subclass it
                problem = _short(str(exc))
                user = (
                    f"{prompt}\n\nYour previous answer was invalid: {problem}\n"
                    "Return only JSON that matches the schema."
                )
                continue
            self._cache_write(key, result)
            return result
        raise LLMError(f"{self.model} returned invalid structured output twice: {problem}")

    def _chat(self, system: str, user: str, schema_json: dict[str, Any], max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema_json,
            "options": {
                "temperature": self.temperature,
                "seed": self.seed,
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,
            },
            "keep_alive": KEEP_ALIVE,
        }
        self.stats["calls"] += 1
        response = self._post("/api/chat", payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"Ollama returned a non-JSON response: {_short(response.text)}") from exc
        if not isinstance(data, dict):
            raise LLMError(f"Ollama returned an unexpected response: {_short(response.text)}")
        if data.get("error"):
            raise LLMError(f"Ollama error for {self.model}: {_short(str(data['error']))}")
        self._record_usage(data)
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LLMError(f"Ollama response has no message content: {_short(response.text)}")
        return content

    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = self._client.post(path, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMError(self._unreachable()) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Ollama at {self.host} timed out after {self.timeout_s:g}s running {self.model}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request to {self.host} failed: {_short(str(exc))}") from exc
        if response.is_success:
            return response
        detail = _error_detail(response)
        if response.status_code == 404 or "not found" in detail.lower():
            raise LLMError(
                f"model {self.model} is not available in Ollama at {self.host} ({detail}); "
                f"run `ollama pull {self.model}`"
            )
        raise LLMError(f"Ollama at {self.host} returned HTTP {response.status_code}: {detail}")

    def _record_usage(self, data: dict[str, Any]) -> None:
        self.stats["prompt_tokens"] += int(_number(data.get("prompt_eval_count")))
        self.stats["completion_tokens"] += int(_number(data.get("eval_count")))
        self.stats["seconds"] += _number(data.get("total_duration")) / 1e9

    def _unreachable(self) -> str:
        return f"Ollama is not reachable at {self.host}; start it with `ollama serve`"

    # ----------------------------------------------------------------- cache

    def _cache_key(
        self, system: str, prompt: str, schema_json: dict[str, Any], max_tokens: int
    ) -> str:
        material = [
            self.model,
            system,
            prompt,
            schema_json,
            self.num_ctx,
            max_tokens,
            self.temperature,
            self.seed,
        ]
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / "llm" / key[:2] / f"{key}.json"

    def _cache_read(self, key: str, schema: type[T]) -> T | None:
        path = self._cache_path(key)
        if path is None or not path.is_file():
            return None
        try:
            return schema.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):  # unreadable or stale entry: recompute it
            return None

    def _cache_write(self, key: str, result: BaseModel) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        # A cache that cannot be written must never fail the analysis itself.
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(result.model_dump(mode="json"), fh, sort_keys=True)
                os.replace(tmp, path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise

    # ---------------------------------------------------------------- health

    def health(self) -> tuple[bool, str]:
        """(ok, message): is the server up and is the configured model pulled?"""
        try:
            response = self._client.get("/api/tags", timeout=min(self.timeout_s, _HEALTH_TIMEOUT_S))
        except httpx.HTTPError:
            return False, f"Ollama not reachable at {self.host}; start it with `ollama serve`"
        if not response.is_success:
            return False, (
                f"Ollama at {self.host} returned HTTP {response.status_code}: "
                f"{_error_detail(response)}"
            )
        try:
            models = response.json().get("models") or []
            names = {
                str(entry.get(field))
                for entry in models
                if isinstance(entry, dict)
                for field in ("name", "model")
                if entry.get(field)
            }
        except (ValueError, AttributeError):
            return False, f"Ollama at {self.host} returned an unexpected /api/tags response"
        if self.model in names or f"{self.model}:latest" in names:
            return True, f"Ollama at {self.host} is serving {self.model}"
        return False, f"model {self.model} is not pulled; run `ollama pull {self.model}`"
