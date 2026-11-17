"""Swappable model layer. Claude is the default; OpenAI, Ollama and an offline mock
are alternatives. Every provider takes the same ``CodingRequest`` and returns the same
JSON shape, so the rest of the app does not care which one ran.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .retrieval import tokenize

# JSON Schema the model must fill in. Kept strict so every provider can enforce it.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "applications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code_id": {"type": "integer"},
                    "evidence_quote": {"type": "string"},
                    "justification": {"type": "string"},
                    "cited_refs": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["code_id", "evidence_quote", "justification", "cited_refs", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["applications"],
    "additionalProperties": False,
}


@dataclass
class CodingRequest:
    system_prompt: str
    user_prompt: str
    passage: str
    codes: list[dict]                 # codes shown to the model
    examples: list[dict]              # retrieved examples shown to the model
    prompts: list[dict]               # active researcher prompts
    context_refs: list[str] = field(default_factory=list)


@dataclass
class ProviderResult:
    data: dict[str, Any]
    raw: str
    model: str


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    name: str

    def is_configured(self) -> bool: ...
    def model_name(self) -> str: ...
    def code(self, req: CodingRequest) -> ProviderResult: ...


def _parse(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProviderError(f"Model did not return valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("applications"), list):
        raise ProviderError("Model JSON is missing the 'applications' list")
    return data


# --------------------------------------------------------------------------- Claude
class ClaudeProvider:
    """Anthropic Messages API. Default model: Claude Opus 5.5.

    * Structured outputs (``output_config.format``) guarantee schema-valid JSON.
    * Server-side refusal fallbacks are on (``fallbacks="default"``): if a request is
      declined by a safety classifier the API retries it on a fallback model in the
      same call. Set QUALCODE_CLAUDE_FALLBACKS=off to disable.
    * The codebook and researcher prompts sit in a cached system block, so coding a
      long document re-sends them cheaply once the codebook is big enough to cache.
    """

    name = "claude"

    def __init__(self) -> None:
        self._client = None

    def is_configured(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def model_name(self) -> str:
        return os.environ.get("QUALCODE_CLAUDE_MODEL", "claude-opus-5-5")

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def code(self, req: CodingRequest) -> ProviderResult:
        import anthropic

        client = self._get_client()
        kwargs: dict[str, Any] = dict(
            model=self.model_name(),
            max_tokens=16000,
            system=[{"type": "text", "text": req.system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": req.user_prompt}],
            output_config={
                "effort": os.environ.get("QUALCODE_CLAUDE_EFFORT", "medium"),
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        )
        try:
            if os.environ.get("QUALCODE_CLAUDE_FALLBACKS", "on").lower() != "off":
                response = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise ProviderError("Anthropic rejected the API key (check ANTHROPIC_API_KEY)") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Rate limited by the Anthropic API; try again shortly") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("Could not reach the Anthropic API") from e

        if response.stop_reason == "refusal":
            raise ProviderError("The model declined to code this passage")
        if response.stop_reason == "max_tokens":
            raise ProviderError("The model ran out of output tokens on this passage")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return ProviderResult(_parse(text), text, response.model)


# --------------------------------------------------------------------------- OpenAI
class OpenAIProvider:
    """OpenAI Chat Completions with a strict JSON schema. Needs OPENAI_API_KEY and
    QUALCODE_OPENAI_MODEL (pick whichever model your account uses)."""

    name = "openai"

    def is_configured(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("QUALCODE_OPENAI_MODEL"))

    def model_name(self) -> str:
        return os.environ.get("QUALCODE_OPENAI_MODEL", "(set QUALCODE_OPENAI_MODEL)")

    def code(self, req: CodingRequest) -> ProviderResult:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ProviderError("pip install openai to use the OpenAI provider") from e
        if not self.is_configured():
            raise ProviderError("Set OPENAI_API_KEY and QUALCODE_OPENAI_MODEL")
        client = OpenAI()
        try:
            resp = client.chat.completions.create(
                model=self.model_name(),
                messages=[
                    {"role": "system", "content": req.system_prompt},
                    {"role": "user", "content": req.user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "coding", "schema": OUTPUT_SCHEMA, "strict": True},
                },
            )
        except Exception as e:  # the OpenAI SDK has its own error tree; keep this provider simple
            raise ProviderError(f"OpenAI error: {e}") from e
        text = resp.choices[0].message.content or ""
        return ProviderResult(_parse(text), text, resp.model)


# --------------------------------------------------------------------------- Ollama
class OllamaProvider:
    """A local open-weight model (e.g. Llama) served by Ollama. No API key, data never
    leaves the machine. Uses Ollama's structured-output ``format`` field."""

    name = "ollama"

    def host(self) -> str:
        return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

    def is_configured(self) -> bool:
        try:
            with urllib.request.urlopen(self.host() + "/api/tags", timeout=1):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def model_name(self) -> str:
        return os.environ.get("QUALCODE_OLLAMA_MODEL", "llama3.1")

    def code(self, req: CodingRequest) -> ProviderResult:
        body = json.dumps(
            {
                "model": self.model_name(),
                "stream": False,
                "format": OUTPUT_SCHEMA,
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": req.system_prompt},
                    {"role": "user", "content": req.user_prompt},
                ],
            }
        ).encode()
        request = urllib.request.Request(
            self.host() + "/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as r:
                payload = json.loads(r.read())
        except (urllib.error.URLError, OSError) as e:
            raise ProviderError(f"Could not reach Ollama at {self.host()}: {e}") from e
        text = payload.get("message", {}).get("content", "")
        return ProviderResult(_parse(text), text, payload.get("model", self.model_name()))


# --------------------------------------------------------------------------- Mock
class MockProvider:
    """Offline stand-in for demos and tests. It is NOT a model: it applies a code when
    the passage shares enough words with the code's definition and positive examples,
    and skips it when the passage looks like one of the code's negative examples."""

    name = "mock"
    threshold = 3

    def is_configured(self) -> bool:
        return True

    def model_name(self) -> str:
        return "mock-keyword-matcher"

    def code(self, req: CodingRequest) -> ProviderResult:
        passage_tokens = set(tokenize(req.passage))
        sentences = [s for s in _sentences(req.passage) if s.strip()] or [req.passage]
        apps = []
        for code in req.codes:
            code_terms = set(tokenize(f"{code['name']} {code['definition']} {code['include_when']}"))
            pos = [e for e in req.examples if e["code_id"] == code["id"] and e["polarity"] == "positive"]
            neg = [e for e in req.examples if e["code_id"] == code["id"] and e["polarity"] == "negative"]
            ex_terms = set().union(*(set(tokenize(e["quote"])) for e in pos)) if pos else set()
            hits = passage_tokens & (code_terms | ex_terms)
            if len(hits) < self.threshold:
                continue
            if any(_jaccard(passage_tokens, set(tokenize(e["quote"]))) > 0.6 for e in neg):
                continue
            best = max(sentences, key=lambda s: len(set(tokenize(s)) & hits))
            refs = [f"CODE:{code['id']}"]
            if pos:
                best_ex = max(pos, key=lambda e: len(set(tokenize(e["quote"])) & passage_tokens))
                refs.append(f"EX:{best_ex['id']}")
            apps.append(
                {
                    "code_id": code["id"],
                    "evidence_quote": best.strip(),
                    "justification": (
                        f"[mock] Shares the terms {', '.join(sorted(hits)[:6])} with the "
                        f"definition of '{code['name']}' and its examples."
                    ),
                    "cited_refs": refs,
                    "confidence": "high" if len(hits) >= 5 else "medium" if len(hits) >= 3 else "low",
                }
            )
        data = {"applications": apps}
        return ProviderResult(data, json.dumps(data), self.model_name())


def _sentences(text: str) -> list[str]:
    import re

    return re.split(r"(?<=[.!?])\s+", text)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


PROVIDERS: dict[str, type] = {
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "mock": MockProvider,
}
DEFAULT_PROVIDER = "claude"
