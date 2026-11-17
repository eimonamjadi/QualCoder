"""Model-layer tests. The Anthropic client is replaced by a fake: no network, no key."""

import json
from types import SimpleNamespace

import pytest

from qualcode import coding
from qualcode.db import DB
from qualcode.providers import OUTPUT_SCHEMA, ClaudeProvider, CodingRequest, ProviderError, ProviderResult


def _req():
    return CodingRequest("sys", "user", "I paid rent with two jobs.", [], [], [], ["CODE:1"])


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(text, stop_reason="end_turn"):
    resp = SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )
    messages = FakeMessages(resp)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


def test_claude_request_shape(monkeypatch):
    monkeypatch.delenv("QUALCODE_CLAUDE_MODEL", raising=False)
    payload = {"applications": [{"code_id": 1, "evidence_quote": "two jobs", "justification": "j",
                                 "cited_refs": ["CODE:1"], "confidence": "high"}]}
    client, messages = fake_client(json.dumps(payload))
    p = ClaudeProvider()
    p._client = client
    result = p.code(_req())
    assert result.data == payload
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in call and "temperature" not in call


def test_claude_fallbacks_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("QUALCODE_CLAUDE_FALLBACKS", "off")
    client, messages = fake_client(json.dumps({"applications": []}))
    p = ClaudeProvider()
    p._client = client
    p.code(_req())
    assert "betas" not in messages.calls[0] and "fallbacks" not in messages.calls[0]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_claude_bad_stop_reasons_raise(stop):
    client, _ = fake_client("{}", stop_reason=stop)
    p = ClaudeProvider()
    p._client = client
    with pytest.raises(ProviderError):
        p.code(_req())


def test_citation_and_quote_validation_flags_problems(tmp_path):
    db = DB(tmp_path / "v.db")
    code_id = db.insert("codes", name="Money", definition="money worries")
    doc_id = db.insert("documents", name="d", text="I paid rent with two jobs.")
    pid = db.insert("passages", document_id=doc_id, idx=0, text="I paid rent with two jobs.", start_char=0, end_char=26)

    class Liar:
        name = "liar"

        def is_configured(self):
            return True

        def model_name(self):
            return "liar-1"

        def code(self, req):
            data = {"applications": [
                {"code_id": code_id, "evidence_quote": "I was broke", "justification": "x",
                 "cited_refs": [f"CODE:{code_id}", "EX:999"], "confidence": "low"},
                {"code_id": 4242, "evidence_quote": "two jobs", "justification": "x",
                 "cited_refs": [], "confidence": "low"},
            ]}
            return ProviderResult(data, json.dumps(data), "liar-1")

    out = coding.code_passage(db, Liar(), pid)
    assert len(out["applications"]) == 1  # the made-up code_id is dropped
    app = db.one("SELECT * FROM applications WHERE id = ?", (out["applications"][0],))
    assert "evidence quote is not verbatim in the passage" in app["flags"]
    assert any("EX:999" in f for f in app["flags"])


def test_provider_error_is_logged_not_raised(tmp_path):
    db = DB(tmp_path / "e.db")
    doc_id = db.insert("documents", name="d", text="x")
    pid = db.insert("passages", document_id=doc_id, idx=0, text="x", start_char=0, end_char=1)

    class Broken:
        name = "broken"

        def is_configured(self):
            return True

        def model_name(self):
            return "b"

        def code(self, req):
            raise ProviderError("boom")

    out = coding.code_passage(db, Broken(), pid)
    assert out["error"] == "boom"
    assert db.one("SELECT error FROM runs WHERE id = ?", (out["run_id"],))["error"] == "boom"
