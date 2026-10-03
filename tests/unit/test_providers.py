import httpx
import pytest

from clipforge.llm import providers
from clipforge.llm.providers import GeminiProvider, LLMError, OpenAICompatProvider


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}


def test_gemini_retries_on_503_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
        return httpx.Response(200, json=GEMINI_OK)

    out = GeminiProvider("k", "m", _client(handler)).complete("s", "p")
    assert out == '{"ok": true}' and len(calls) == 3


def test_gives_up_after_max_attempts_on_persistent_503():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, text="surcharge")

    with pytest.raises(LLMError, match="503"):
        GeminiProvider("k", "m", _client(handler)).complete("s", "p")
    assert len(calls) == providers.MAX_ATTEMPTS


def test_no_retry_on_413_request_too_large():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(413, text="too large")

    with pytest.raises(LLMError, match="413"):
        OpenAICompatProvider("groq", "http://x", "k", "m", _client(handler)).complete("s", "p")
    assert len(calls) == 1


def test_groq_request_caps_output_and_lowers_reasoning():
    seen = {}

    def handler(req):
        import json

        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    OpenAICompatProvider("groq", "http://x", "k", "openai/gpt-oss-120b", _client(handler)).complete(
        "s", "p"
    )
    assert seen["max_tokens"] == providers.MAX_OUTPUT_TOKENS
    assert seen["reasoning_effort"] == "low"


def test_retry_after_header_is_honoured(monkeypatch):
    waits = []
    monkeypatch.setattr(providers.time, "sleep", waits.append)
    n = []

    def handler(req):
        n.append(1)
        if len(n) == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, text="tpm")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    OpenAICompatProvider("groq", "http://x", "k", "m", _client(handler)).complete("s", "p")
    assert waits == [7.0]
