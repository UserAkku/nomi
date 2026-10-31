"""OllamaLLM against a mocked Ollama HTTP API (plus one opt-in real-model test)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import BaseModel

from nomi.extract.claims import extract_claims
from nomi.llm.base import LLMError
from nomi.llm.ollama import OllamaLLM, clean_content, normalize_host
from nomi.models import Artifacts, FileRef, Kind, Submission

HOST = "http://localhost:11434"
MODEL = "qwen3:4b-instruct-2507-q4_K_M"
CHAT = f"{HOST}/api/chat"
TAGS = f"{HOST}/api/tags"


class Answer(BaseModel):
    verdict: str
    score: int


def chat_reply(content: str, **extra: object) -> httpx.Response:
    body = {
        "model": MODEL,
        "message": {"role": "assistant", "content": content},
        "done": True,
        "prompt_eval_count": 100,
        "eval_count": 20,
        "total_duration": 2_500_000_000,
        **extra,
    }
    return httpx.Response(200, json=body)


def sent_json(route: respx.Route, call: int = 0) -> dict:
    return json.loads(route.calls[call].request.content)


@pytest.fixture
def llm() -> OllamaLLM:
    return OllamaLLM(MODEL, HOST)


# ------------------------------------------------------------------ generate


@respx.mock
def test_payload_shape_and_parsed_result(llm: OllamaLLM) -> None:
    route = respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "ok", "score": 3}'))

    result = llm.generate(system="be strict", prompt="judge this", schema=Answer, max_tokens=77)

    assert result == Answer(verdict="ok", score=3)
    payload = sent_json(route)
    assert payload["model"] == MODEL
    assert payload["messages"] == [
        {"role": "system", "content": "be strict"},
        {"role": "user", "content": "judge this"},
    ]
    assert payload["stream"] is False
    assert payload["format"] == Answer.model_json_schema()
    assert payload["options"] == {
        "temperature": 0.0,
        "seed": 42,
        "num_ctx": 8192,
        "num_predict": 77,
    }
    assert payload["keep_alive"] == "15m"
    assert "think" not in payload


@respx.mock
def test_options_follow_constructor_arguments() -> None:
    route = respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "ok", "score": 1}'))
    llm = OllamaLLM(MODEL, HOST, num_ctx=4096, temperature=0.2, seed=7)

    llm.generate(system="s", prompt="p", schema=Answer)

    assert sent_json(route)["options"] == {
        "temperature": 0.2,
        "seed": 7,
        "num_ctx": 4096,
        "num_predict": 1024,
    }


@respx.mock
def test_think_block_is_stripped(llm: OllamaLLM) -> None:
    content = '<think>\nweighing options...\n</think>\n{"verdict": "fine", "score": 2}'
    respx.post(CHAT).mock(return_value=chat_reply(content))

    assert llm.generate(system="s", prompt="p", schema=Answer).verdict == "fine"


@respx.mock
def test_invalid_json_retries_once_with_error_feedback(llm: OllamaLLM) -> None:
    route = respx.post(CHAT).mock(
        side_effect=[chat_reply("not json at all"), chat_reply('{"verdict": "ok", "score": 9}')]
    )

    result = llm.generate(system="s", prompt="original prompt", schema=Answer)

    assert result.score == 9
    assert route.call_count == 2
    retry_user = sent_json(route, 1)["messages"][1]["content"]
    assert retry_user.startswith("original prompt")
    assert "Your previous answer was invalid:" in retry_user
    assert "Return only JSON that matches the schema." in retry_user
    assert llm.stats["calls"] == 2


@respx.mock
def test_schema_violation_twice_raises(llm: OllamaLLM) -> None:
    route = respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "ok"}'))

    with pytest.raises(LLMError, match="invalid structured output"):
        llm.generate(system="s", prompt="p", schema=Answer)
    assert route.call_count == 2
    assert "score" in sent_json(route, 1)["messages"][1]["content"]


@respx.mock
def test_connection_error_is_actionable(llm: OllamaLLM) -> None:
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(LLMError) as exc:
        llm.generate(system="s", prompt="p", schema=Answer)
    assert str(exc.value) == f"Ollama is not reachable at {HOST}; start it with `ollama serve`"


@respx.mock
def test_missing_model_suggests_pull(llm: OllamaLLM) -> None:
    respx.post(CHAT).mock(
        return_value=httpx.Response(404, json={"error": f'model "{MODEL}" not found'})
    )

    with pytest.raises(LLMError, match=r"ollama pull qwen3:4b-instruct-2507-q4_K_M"):
        llm.generate(system="s", prompt="p", schema=Answer)


@respx.mock
def test_server_error_carries_detail(llm: OllamaLLM) -> None:
    respx.post(CHAT).mock(return_value=httpx.Response(500, json={"error": "out of memory"}))

    with pytest.raises(LLMError, match=r"HTTP 500: out of memory"):
        llm.generate(system="s", prompt="p", schema=Answer)


@respx.mock
def test_timeout_is_reported(llm: OllamaLLM) -> None:
    respx.post(CHAT).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(LLMError, match="timed out"):
        llm.generate(system="s", prompt="p", schema=Answer)


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>proxy</html>"),
        httpx.Response(200, json={"error": "model crashed"}),
        httpx.Response(200, json={"done": True}),
        httpx.Response(200, json=["unexpected"]),
    ],
)
def test_malformed_server_responses_raise(llm: OllamaLLM, response: httpx.Response) -> None:
    respx.post(CHAT).mock(return_value=response)

    with pytest.raises(LLMError):
        llm.generate(system="s", prompt="p", schema=Answer)


@respx.mock
def test_stats_accumulate(llm: OllamaLLM) -> None:
    respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "ok", "score": 1}'))

    llm.generate(system="s", prompt="a", schema=Answer)
    llm.generate(system="s", prompt="b", schema=Answer)

    assert llm.stats == {
        "calls": 2,
        "cache_hits": 0,
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "seconds": pytest.approx(5.0),
    }


def test_custom_transport_is_used() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return chat_reply('{"verdict": "ok", "score": 5}')

    llm = OllamaLLM(MODEL, HOST, transport=httpx.MockTransport(handler))

    assert llm.generate(system="s", prompt="p", schema=Answer).score == 5
    assert seen == ["/api/chat"]
    assert llm.name == MODEL


# --------------------------------------------------------------------- cache


@respx.mock
def test_cache_hit_avoids_http(tmp_path: Path) -> None:
    route = respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "cached", "score": 4}'))
    first = OllamaLLM(MODEL, HOST, cache_dir=tmp_path)
    assert first.generate(system="s", prompt="p", schema=Answer).verdict == "cached"
    assert route.call_count == 1

    files = list((tmp_path / "llm").rglob("*.json"))
    assert len(files) == 1
    assert files[0].parent.name == files[0].stem[:2]
    assert json.loads(files[0].read_text()) == {"score": 4, "verdict": "cached"}

    second = OllamaLLM(MODEL, HOST, cache_dir=tmp_path)
    assert second.generate(system="s", prompt="p", schema=Answer) == Answer(
        verdict="cached", score=4
    )
    assert route.call_count == 1
    assert second.stats["cache_hits"] == 1
    assert second.stats["calls"] == 0


@respx.mock
def test_cache_key_depends_on_inputs(tmp_path: Path) -> None:
    route = respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "ok", "score": 1}'))
    llm = OllamaLLM(MODEL, HOST, cache_dir=tmp_path)

    llm.generate(system="s", prompt="p", schema=Answer)
    llm.generate(system="s", prompt="p", schema=Answer, max_tokens=10)
    OllamaLLM(MODEL, HOST, cache_dir=tmp_path, seed=1).generate(
        system="s", prompt="p", schema=Answer
    )
    OllamaLLM("other:1b", HOST, cache_dir=tmp_path).generate(system="s", prompt="p", schema=Answer)

    assert route.call_count == 4


@respx.mock
def test_corrupt_cache_entry_is_recomputed(tmp_path: Path) -> None:
    route = respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "fresh", "score": 2}'))
    llm = OllamaLLM(MODEL, HOST, cache_dir=tmp_path)
    llm.generate(system="s", prompt="p", schema=Answer)
    (entry,) = (tmp_path / "llm").rglob("*.json")
    entry.write_text("{ truncated")

    assert llm.generate(system="s", prompt="p", schema=Answer).verdict == "fresh"
    assert route.call_count == 2
    assert json.loads(entry.read_text())["verdict"] == "fresh"


@respx.mock
def test_failed_answers_are_not_cached(tmp_path: Path) -> None:
    respx.post(CHAT).mock(return_value=chat_reply("nope"))
    llm = OllamaLLM(MODEL, HOST, cache_dir=tmp_path)

    with pytest.raises(LLMError):
        llm.generate(system="s", prompt="p", schema=Answer)
    assert not list(tmp_path.rglob("*.json"))


@respx.mock
def test_unwritable_cache_does_not_fail_generation(tmp_path: Path) -> None:
    respx.post(CHAT).mock(return_value=chat_reply('{"verdict": "ok", "score": 1}'))
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")

    llm = OllamaLLM(MODEL, HOST, cache_dir=blocker)

    assert llm.generate(system="s", prompt="p", schema=Answer).score == 1


# -------------------------------------------------------------------- health


def tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in names]})


@respx.mock
def test_health_ok_when_model_listed(llm: OllamaLLM) -> None:
    respx.get(TAGS).mock(return_value=tags("llama3:8b", MODEL))

    ok, message = llm.health()

    assert ok
    assert MODEL in message


@respx.mock
def test_health_accepts_latest_suffix() -> None:
    respx.get(TAGS).mock(return_value=tags("mistral:latest"))

    assert OllamaLLM("mistral", HOST).health()[0]


@respx.mock
def test_health_reports_missing_model(llm: OllamaLLM) -> None:
    respx.get(TAGS).mock(return_value=tags("llama3:8b"))

    assert llm.health() == (False, f"model {MODEL} is not pulled; run `ollama pull {MODEL}`")


@respx.mock
def test_health_reports_unreachable_server(llm: OllamaLLM) -> None:
    respx.get(TAGS).mock(side_effect=httpx.ConnectError("refused"))

    ok, message = llm.health()

    assert not ok
    assert message.startswith(f"Ollama not reachable at {HOST}")


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(503, text="starting"), httpx.Response(200, text="garbage"), tags()],
)
def test_health_handles_bad_responses(llm: OllamaLLM, response: httpx.Response) -> None:
    respx.get(TAGS).mock(return_value=response)

    assert llm.health()[0] is False


# ------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("localhost:11434", "http://localhost:11434"),
        ("http://gpu-box:11434/", "http://gpu-box:11434"),
        ("  https://ollama.example  ", "https://ollama.example"),
        ("", "http://localhost:11434"),
    ],
)
def test_normalize_host(raw: str, expected: str) -> None:
    assert normalize_host(raw) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('<think></think>{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('{"text": "<think>kept</think>"}', '{"text": "<think>kept</think>"}'),
    ],
)
def test_clean_content(content: str, expected: str) -> None:
    assert clean_content(content) == expected


# --------------------------------------------------------- real local model


@pytest.mark.ollama
def test_real_model_quotes_are_verbatim() -> None:
    llm = OllamaLLM(MODEL, os.environ.get("OLLAMA_HOST", HOST), timeout_s=180)
    ok, message = llm.health()
    if not ok:
        pytest.skip(message)
    submission = Submission(
        kind=Kind.ISSUE,
        repo="acme/widget",
        title="parse_url rejects URLs without a scheme",
        body=(
            "Calling `parse_url('example.com')` raises ValueError instead of assuming http.\n"
            "The check is in src/widget/parser.py at line 11."
        ),
    )
    artifacts = Artifacts(
        symbols=["parse_url", "ValueError"],
        file_refs=[FileRef(path="src/widget/parser.py", raw="src/widget/parser.py")],
    )

    claims = extract_claims(submission, artifacts, llm, max_claims=3)

    assert claims
    assert llm.stats["calls"] >= 1
    for claim in claims:
        assert claim.quote in submission.text
        assert all(entity in submission.text for entity in claim.entities)
