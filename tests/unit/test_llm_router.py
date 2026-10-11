import pytest

from clipforge.llm.providers import LLMError
from clipforge.llm.router import LLMRouter, parse_json


class Fake:
    def __init__(self, name, outputs):
        self.name, self.outputs, self.calls = name, list(outputs), 0

    def complete(self, system, prompt):
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_parse_json_with_fences_and_noise():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Voici : {"a": [1, 2]} fin') == {"a": [1, 2]}


def test_fallback_to_next_provider_on_error():
    a = Fake("a", [LLMError("quota")])
    b = Fake("b", ['{"ok": true}'])
    assert LLMRouter([a, b]).generate_json("s", "p") == {"ok": True}
    assert a.calls == 1 and b.calls == 1


def test_retry_once_on_invalid_json_then_fallback():
    a = Fake("a", ["pas du json", "toujours pas"])
    b = Fake("b", ['{"x": 2}'])
    assert LLMRouter([a, b]).generate_json("s", "p") == {"x": 2}
    assert a.calls == 2


def test_all_fail_raises():
    with pytest.raises(LLMError):
        LLMRouter([Fake("a", [LLMError("x")])]).generate_json("s", "p")


def test_no_provider_raises():
    with pytest.raises(LLMError):
        LLMRouter([]).generate_json("s", "p")
