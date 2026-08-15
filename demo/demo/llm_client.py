"""Calls to the LLM provider, through the chaosllm proxy.

The proxy is a pure passthrough: it forwards whatever the caller sends and
holds no key of its own. That makes this app the caller, so it needs its
own provider key, and it needs to build the right endpoint, auth header,
and request/response shape for whichever provider that key belongs to.
LLM_PROVIDER=openai|anthropic (default openai) selects it; see
load_provider_config() for the env vars involved.

Two code paths behind the RESILIENT flag (DESIGN.md 4.6):
  - naive: no explicit timeout override, no retries, no fallback. A
    blackholed proxy call hangs the naive path exactly like it would in a
    real, unguarded production app.
  - resilient: an explicit httpx timeout, tenacity retries with jitter on
    retryable failures, and a circuit breaker so a sustained outage stops
    hammering a dead upstream instead of retrying it into the ground.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from demo.circuit_breaker import CircuitBreaker, CircuitOpenError

RESILIENT_TIMEOUT_S = 5.0
RESILIENT_MAX_ATTEMPTS = 3
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 512

_breaker = CircuitBreaker(failure_threshold=3, reset_after_s=5.0)


class LLMUnavailableError(Exception):
    pass


class LLMConfigError(Exception):
    """Raised at startup: LLM_PROVIDER is unsupported or its key is missing."""


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    path: str
    model: str
    api_key: str

    def request_headers(self) -> dict[str, str]:
        if self.provider == "anthropic":
            return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}
        return {"authorization": f"Bearer {self.api_key}"}

    def request_body(self, question: str, context: str) -> dict[str, Any]:
        system = f"Answer using only this context:\n{context}"
        if self.provider == "anthropic":
            return {
                "model": self.model,
                "max_tokens": ANTHROPIC_MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": question}],
            }
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }

    def extract_text(self, body: dict[str, Any]) -> str:
        text: Any = None
        if self.provider == "anthropic":
            content = body.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                text = content[0].get("text")
        else:
            choices = body.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    text = message.get("content")
        if not isinstance(text, str) or not text:
            raise LLMUnavailableError("malformed LLM response: no text content")
        return text


# path/model-env/default-model/key-env per supported provider.
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "path": "/openai/v1/chat/completions",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-5.4-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "path": "/anthropic/v1/messages",
        "model_env": "ANTHROPIC_MODEL",
        "default_model": "claude-haiku-4-5",
        "key_env": "ANTHROPIC_API_KEY",
    },
}


def load_provider_config() -> ProviderConfig:
    """Read LLM_PROVIDER and the matching model/key env vars.

    Raises LLMConfigError if the provider is unsupported or its key is
    missing, so the app fails fast at startup with a clear message instead
    of surfacing a confusing 401 from the real provider on the first /ask.
    """
    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
    defaults = _PROVIDER_DEFAULTS.get(provider)
    if defaults is None:
        supported = ", ".join(sorted(_PROVIDER_DEFAULTS))
        raise LLMConfigError(f"LLM_PROVIDER={provider!r} is not supported; use one of: {supported}")

    key_env = defaults["key_env"]
    api_key = os.environ.get(key_env, "").strip()
    if not api_key:
        raise LLMConfigError(
            f"LLM_PROVIDER={provider} but {key_env} is not set. "
            f"Set {key_env} in your environment (see .env.example)."
        )

    model = os.environ.get(defaults["model_env"], defaults["default_model"])
    return ProviderConfig(provider=provider, path=defaults["path"], model=model, api_key=api_key)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    # Malformed/truncated bodies (the truncate and malformed_json faults)
    # surface as LLMUnavailableError too: worth one retry, same as a bad status.
    return isinstance(exc, LLMUnavailableError)


async def naive_ask_llm(
    client: httpx.AsyncClient, config: ProviderConfig, question: str, context: str
) -> str:
    """No timeout override, no retries, no fallback: mirrors an unguarded prod app."""
    response = await client.post(
        config.path,
        json=config.request_body(question, context),
        headers=config.request_headers(),
        timeout=None,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise LLMUnavailableError(f"malformed LLM response: {exc}") from exc
    return config.extract_text(body)


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(RESILIENT_MAX_ATTEMPTS),
    wait=wait_random_exponential(multiplier=0.1, max=1.0),
    reraise=True,
)
async def _resilient_call(
    client: httpx.AsyncClient, config: ProviderConfig, question: str, context: str
) -> str:
    response = await client.post(
        config.path,
        json=config.request_body(question, context),
        headers=config.request_headers(),
        timeout=RESILIENT_TIMEOUT_S,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise LLMUnavailableError(f"malformed LLM response: {exc}") from exc
    return config.extract_text(body)


async def resilient_ask_llm(
    client: httpx.AsyncClient, config: ProviderConfig, question: str, context: str
) -> str:
    """Timeout + retry-with-jitter + circuit breaker.

    Raises LLMUnavailableError on final failure so the caller can fall back
    to an extractive (retrieval-only) answer instead of failing the request.
    """
    try:
        _breaker.before_call()
        text = await _resilient_call(client, config, question, context)
    except CircuitOpenError as exc:
        raise LLMUnavailableError("circuit open") from exc
    except (httpx.HTTPError, LLMUnavailableError) as exc:
        _breaker.record_failure()
        raise LLMUnavailableError(str(exc)) from exc
    else:
        _breaker.record_success()
        return text
