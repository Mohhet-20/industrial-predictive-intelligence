"""Unit tests for the LLM provider abstraction.

AnthropicProvider is tested against a MOCKED httpx transport — no network
call is made, and no live Anthropic API call has ever been exercised in
this environment (there is no ANTHROPIC_API_KEY configured anywhere in
this build; see docs/COPILOT.md for the honest scope of what was and
wasn't validated against a live call). These tests instead prove: the
request is shaped correctly, the response is parsed correctly, and every
failure mode (timeout, network error, non-200, malformed body, empty
text) degrades to a ProviderResult(ok=False) rather than raising or
crashing a request.
"""
import httpx
import pytest

from app.copilot.providers import AnthropicProvider, NullProvider, get_provider


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json_body = json_body

    def json(self):
        return self._json_body


class _FakeClient:
    """Stands in for httpx.Client(...) as a context manager. `behavior` is
    a callable taking (url, headers, json) and returning either a
    _FakeResponse or raising an httpx exception, so each test controls
    exactly what "the network" does."""

    def __init__(self, behavior):
        self._behavior = behavior

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        return self._behavior(url, headers, json)


def _patch_client(monkeypatch, behavior):
    monkeypatch.setattr(
        "app.copilot.providers.httpx.Client",
        lambda timeout=None: _FakeClient(behavior),
    )


# ---------------------------------------------------------------------------
# NullProvider
# ---------------------------------------------------------------------------


def test_null_provider_never_raises_and_is_honest():
    provider = NullProvider()
    result = provider.generate(system_prompt="anything", user_message="anything")
    assert result.ok is False
    assert result.provider_name == "none"
    assert "not configured" in result.error.lower() or "ANTHROPIC_API_KEY" in result.error


# ---------------------------------------------------------------------------
# AnthropicProvider — request shape
# ---------------------------------------------------------------------------


def test_anthropic_provider_sends_expected_request_shape(monkeypatch):
    captured = {}

    def behavior(url, headers, json):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, {"content": [{"type": "text", "text": "An answer citing [E1]."}]})

    _patch_client(monkeypatch, behavior)
    provider = AnthropicProvider(api_key="sk-test-fake", model="claude-3-5-haiku-20241022", timeout_seconds=30)
    result = provider.generate(system_prompt="SYSTEM", user_message="What happened?")

    assert result.ok is True
    assert result.text == "An answer citing [E1]."
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-test-fake"
    assert captured["headers"]["anthropic-version"]
    assert captured["json"]["model"] == "claude-3-5-haiku-20241022"
    assert captured["json"]["system"] == "SYSTEM"
    assert captured["json"]["messages"] == [{"role": "user", "content": "What happened?"}]


def test_anthropic_provider_key_never_appears_in_result(monkeypatch):
    def behavior(url, headers, json):
        return _FakeResponse(200, {"content": [{"type": "text", "text": "ok"}]})

    _patch_client(monkeypatch, behavior)
    provider = AnthropicProvider(api_key="sk-super-secret", model="m", timeout_seconds=5)
    result = provider.generate(system_prompt="s", user_message="u")
    assert "sk-super-secret" not in str(result)
    assert "sk-super-secret" not in repr(result)


# ---------------------------------------------------------------------------
# AnthropicProvider — failure modes never raise
# ---------------------------------------------------------------------------


def test_anthropic_provider_handles_timeout(monkeypatch):
    def behavior(url, headers, json):
        raise httpx.TimeoutException("timed out")

    _patch_client(monkeypatch, behavior)
    provider = AnthropicProvider(api_key="k", model="m", timeout_seconds=1)
    result = provider.generate(system_prompt="s", user_message="u")
    assert result.ok is False
    assert "timed out" in result.error.lower()


def test_anthropic_provider_handles_network_error(monkeypatch):
    def behavior(url, headers, json):
        raise httpx.ConnectError("connection refused")

    _patch_client(monkeypatch, behavior)
    provider = AnthropicProvider(api_key="k", model="m", timeout_seconds=1)
    result = provider.generate(system_prompt="s", user_message="u")
    assert result.ok is False
    assert "could not be reached" in result.error.lower()


def test_anthropic_provider_handles_non_200_without_leaking_body(monkeypatch):
    def behavior(url, headers, json):
        return _FakeResponse(401, {"error": {"message": "invalid x-api-key: sk-super-secret"}})

    _patch_client(monkeypatch, behavior)
    provider = AnthropicProvider(api_key="k", model="m", timeout_seconds=1)
    result = provider.generate(system_prompt="s", user_message="u")
    assert result.ok is False
    assert "401" in result.error
    assert "sk-super-secret" not in result.error  # raw provider error body never surfaced


def test_anthropic_provider_handles_malformed_body(monkeypatch):
    def behavior(url, headers, json):
        return _FakeResponse(200, {"unexpected": "shape"})

    _patch_client(monkeypatch, behavior)
    provider = AnthropicProvider(api_key="k", model="m", timeout_seconds=1)
    result = provider.generate(system_prompt="s", user_message="u")
    assert result.ok is False
    assert "empty" in result.error.lower() or "unexpected" in result.error.lower()


def test_anthropic_provider_handles_empty_text(monkeypatch):
    def behavior(url, headers, json):
        return _FakeResponse(200, {"content": [{"type": "text", "text": ""}]})

    _patch_client(monkeypatch, behavior)
    provider = AnthropicProvider(api_key="k", model="m", timeout_seconds=1)
    result = provider.generate(system_prompt="s", user_message="u")
    assert result.ok is False
    assert "empty" in result.error.lower()


# ---------------------------------------------------------------------------
# get_provider factory
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, *, configured, provider="anthropic", api_key=None, model="m", timeout=30):
        self.copilot_configured = configured
        self.copilot_llm_provider = provider
        self.anthropic_api_key = api_key
        self.copilot_llm_model = model
        self.copilot_llm_timeout_seconds = timeout


def test_get_provider_returns_null_when_not_configured():
    provider = get_provider(_FakeSettings(configured=False))
    assert isinstance(provider, NullProvider)


def test_get_provider_returns_anthropic_when_configured():
    provider = get_provider(_FakeSettings(configured=True, api_key="sk-x"))
    assert isinstance(provider, AnthropicProvider)
