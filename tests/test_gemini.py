"""GeminiLLM: retries short overloads (503) instead of failing the email, fails fast on
quota and request errors. The Gen AI client is faked; no network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors
from pydantic import BaseModel

from emailctx.config import Settings
from emailctx.llm import GeminiLLM, LLMError


class Ping(BaseModel):
    ok: bool


def api_error(code: int) -> errors.APIError:
    return errors.APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)  # exceptions to raise, then a text to return
        self.calls = 0
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))])


def llm(client, waits):
    return GeminiLLM(Settings(provider="gemini"), client=client, sleep=waits.append)


def test_overload_is_retried_then_succeeds():
    client, waits = FakeClient([api_error(503), api_error(503), '{"ok": true}']), []
    assert llm(client, waits).structured("gate", "s", "p", Ping) == Ping(ok=True)
    assert client.calls == 3
    assert waits == [2.0, 5.0]


def test_overload_that_persists_fails_after_three_retries():
    client, waits = FakeClient([api_error(503)] * 4), []
    with pytest.raises(LLMError, match="503"):
        llm(client, waits).structured("extract", "s", "p", Ping)
    assert client.calls == 4
    assert waits == [2.0, 5.0, 10.0]


@pytest.mark.parametrize("code", [429, 404, 400])
def test_quota_and_request_errors_are_not_retried(code):
    client, waits = FakeClient([api_error(code)]), []
    with pytest.raises(LLMError, match=str(code)):
        llm(client, waits).structured("gate", "s", "p", Ping)
    assert client.calls == 1
    assert waits == []
