"""LLM provider abstraction (Phase 9 §9).

Core application logic (app/copilot/orchestrator.py) never imports a
provider SDK or constructs a provider-specific request directly — it only
calls `LLMProvider.generate(system_prompt, user_message)`. This keeps the
rest of the platform decoupled from any one vendor, makes "no provider
configured" a first-class, tested code path (not an afterthought), and
means Project 5's core functionality is provably unaffected by whether an
API key is present — see tests/test_copilot_provider.py.

Only one real provider is implemented (Anthropic's Messages API) because
that's the only one with a documented, stable REST contract worth coding
against here — this is not a claim that other providers are unsupported
by design, just that only one adapter was actually built and tested.
Nothing in this module is a claim of local/private inference; both
providers below either call a real hosted API over the network or return
an explicit "not configured" result — there is no on-device model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx

logger = logging.getLogger("app.copilot.providers")

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_API_VERSION = "2023-06-01"


@dataclass
class ProviderResult:
    ok: bool
    text: str = ""
    provider_name: str = "none"
    error: Optional[str] = None


class LLMProvider(Protocol):
    name: str

    def generate(self, *, system_prompt: str, user_message: str) -> ProviderResult:
        ...


class NullProvider:
    """The provider used whenever no real one is configured. Never raises,
    never crashes a request — returns a clear, honest, structured result so
    the API/UI can show "copilot not configured" instead of a 500 or (worse)
    a silently fabricated answer. This is what makes "core Project 5
    functionality continues working without the copilot" true by
    construction rather than by hoping every caller remembers to check."""

    name = "none"

    def generate(self, *, system_prompt: str, user_message: str) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider_name="none",
            error="No LLM provider is configured for this deployment. Set ANTHROPIC_API_KEY to enable the copilot.",
        )


class AnthropicProvider:
    """A real, minimal Anthropic Messages API client. Uses httpx directly
    (no SDK dependency) so the request/response contract is fully visible
    and testable against a mocked transport — see
    tests/test_copilot_provider.py, which verifies the exact request shape
    and a range of response/error handling paths WITHOUT calling the real
    API (no key is configured in this project's own build/test
    environment — see docs/COPILOT.md for the honest scope of what was and
    wasn't validated against a live call)."""

    name = "anthropic"

    def __init__(self, *, api_key: str, model: str, timeout_seconds: int) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    def generate(self, *, system_prompt: str, user_message: str) -> ProviderResult:
        headers = {
            "x-api-key": self._api_key,  # never logged — see module docstring and docs/SECURITY_AND_PRIVACY.md
            "anthropic-version": _ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(_ANTHROPIC_API_URL, headers=headers, json=payload)
        except httpx.TimeoutException:
            logger.warning("Anthropic API call timed out after %ss", self._timeout)
            return ProviderResult(ok=False, provider_name=self.name, error="The AI provider timed out.")
        except httpx.HTTPError as exc:
            logger.warning("Anthropic API call failed: %s", exc.__class__.__name__)
            return ProviderResult(ok=False, provider_name=self.name, error="The AI provider could not be reached.")

        if response.status_code != 200:
            # Never surface the raw provider error body to the client — it
            # can contain account/billing detail unrelated to this request.
            logger.warning("Anthropic API returned status %s", response.status_code)
            return ProviderResult(
                ok=False, provider_name=self.name, error=f"The AI provider returned an error (status {response.status_code})."
            )

        try:
            body = response.json()
            text_blocks = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
            text = "\n".join(t for t in text_blocks if t)
        except Exception:
            return ProviderResult(ok=False, provider_name=self.name, error="The AI provider returned an unexpected response shape.")

        if not text:
            return ProviderResult(ok=False, provider_name=self.name, error="The AI provider returned an empty response.")
        return ProviderResult(ok=True, text=text, provider_name=self.name)


def get_provider(settings) -> LLMProvider:
    """Factory used by the orchestrator. Takes the app's `Settings` object
    (not raw env access) so tests can construct a `Settings` directly with
    whatever provider config they want to exercise."""
    if settings.copilot_configured:
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.copilot_llm_model,
            timeout_seconds=settings.copilot_llm_timeout_seconds,
        )
    return NullProvider()
