from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

from demo import llm_client
from demo.main import app


@pytest.fixture(autouse=True)
def _reset_circuit_breaker() -> None:
    # The breaker is a module-level singleton so state persists across
    # requests within a process, by design (that's what makes it useful).
    # Reset it between tests so one test's failures can't leak into another.
    llm_client._breaker.record_success()


@pytest.fixture
def _default_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The app now fails fast at startup without a provider key (see
    # llm_client.load_provider_config). Give tests a default openai key so
    # the `client` fixture's lifespan startup succeeds; tests that care
    # about a specific provider/missing-key scenario override this
    # themselves via the same monkeypatch fixture.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
async def client(_default_provider_env: None) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
