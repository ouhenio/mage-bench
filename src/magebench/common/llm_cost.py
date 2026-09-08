"""Shared LLM cost tracking utilities.

Fetches live model pricing from OpenRouter at startup and provides
helpers for cost estimation and file-based cost reporting.
"""

import json
import os
from pathlib import Path

from magebench.common import http_utils
from magebench.common.log import get_logger

logger = get_logger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
FETCH_TIMEOUT_SECS = 10
DEFAULT_LLM_PROVIDER = "openrouter"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_PROVIDER_BASE_URLS = {
    "openrouter": DEFAULT_BASE_URL,
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

# SELF-HOSTED SEATS. Two of them, because vLLM cannot hold two weight sets in one server
# and a checkpoint-vs-checkpoint game therefore needs two servers -- one per seat. `local_b`
# is `local` in every respect except which host it reads; nothing else in the harness
# distinguishes them, and nothing should.
_SELF_HOSTED_BASE_URL_ENVS = {
    "local": ("MAGEBENCH_LOCAL_BASE_URL", "http://127.0.0.1:8000/v1"),
    "local_b": ("MAGEBENCH_LOCAL_B_BASE_URL", "http://127.0.0.1:8001/v1"),
}
SELF_HOSTED_PROVIDERS = frozenset(_SELF_HOSTED_BASE_URL_ENVS)

_PROVIDER_API_KEY_ENVS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    # The key is ignored by vLLM but the client still requires a non-empty string.
    "local": "MAGEBENCH_LOCAL_API_KEY",
    "local_b": "MAGEBENCH_LOCAL_B_API_KEY",
}
SUPPORTED_LLM_PROVIDERS = tuple(_PROVIDER_BASE_URLS) + tuple(_SELF_HOSTED_BASE_URL_ENVS)


def is_self_hosted(provider: str | None) -> bool:
    """Whether this provider is one of our own vLLM servers.

    ONE PREDICATE, because there were already two copies of the question and the second
    was written as `provider != "local"`. Anything gated on "is this a vLLM endpoint" --
    token-id capture, the decision-identity passthrough, cost accounting -- must ask here
    rather than compare against a literal, or adding a third self-hosted seat silently
    breaks whichever copy nobody remembered.
    """
    return _resolve_llm_provider(provider) in SELF_HOSTED_PROVIDERS


_OPENROUTER_HOSTS = frozenset({"openrouter.ai"})


def _resolve_llm_provider(provider: str | None) -> str:
    """Resolve a provider slug to a supported value."""
    if provider is None:
        return DEFAULT_LLM_PROVIDER
    if provider in _PROVIDER_BASE_URLS or provider in _SELF_HOSTED_BASE_URL_ENVS:
        return provider
    supported = ", ".join(SUPPORTED_LLM_PROVIDERS)
    raise ValueError(f"Unknown LLM provider: {provider!r}. Supported providers: {supported}.")


def llm_base_url(provider: str | None) -> str:
    """Map a provider slug to its OpenAI-compatible base URL.

    THE SELF-HOSTED HOSTS ARE READ AT CALL TIME. They used to be captured into
    _PROVIDER_BASE_URLS at import, so a process that imported this module before setting
    MAGEBENCH_LOCAL_BASE_URL got the default and never knew. With one self-hosted seat that
    was a wrong host; with two it is worse and quieter -- BOTH fall back to their defaults,
    and if only one default is reachable both seats end up on the SAME SERVER, playing a
    checkpoint against itself while every config, log and result looks exactly like the
    intended experiment. That is the failure this whole feature has to be proof against.
    """
    resolved = _resolve_llm_provider(provider)
    env_entry = _SELF_HOSTED_BASE_URL_ENVS.get(resolved)
    if env_entry is not None:
        env_name, fallback = env_entry
        return os.environ.get(env_name, fallback)
    return _PROVIDER_BASE_URLS[resolved]


def required_api_key_env(provider: str | None) -> str:
    """Infer the expected API key env var from the configured provider."""
    return _PROVIDER_API_KEY_ENVS[_resolve_llm_provider(provider)]


def fetch_openrouter_prices() -> dict[str, tuple[float, float]]:
    """Fetch model pricing from OpenRouter.

    Returns {model_id: (input_per_1M_tokens, output_per_1M_tokens)}.
    Returns empty dict on any failure.
    """
    try:
        data = json.loads(
            http_utils.fetch_https_bytes(
                OPENROUTER_MODELS_URL,
                allowed_hosts=_OPENROUTER_HOSTS,
                timeout=FETCH_TIMEOUT_SECS,
            )
        )
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[llm_cost] Failed to fetch OpenRouter prices: %s", e)
        return {}

    prices: dict[str, tuple[float, float]] = {}
    models = data.get("data")
    if models is None:
        logger.warning("[llm_cost] OpenRouter response missing 'data' field")
        return {}
    for model in models:
        model_id = model.get("id")
        pricing = model.get("pricing")
        if not model_id or not pricing:
            continue
        try:
            prompt_per_token = float(pricing.get("prompt") or "0")
            completion_per_token = float(pricing.get("completion") or "0")
            prices[model_id] = (
                prompt_per_token * 1_000_000,
                completion_per_token * 1_000_000,
            )
        except (ValueError, TypeError):
            continue
    return prices


def load_prices() -> dict[str, tuple[float, float]]:
    """Fetch OpenRouter prices at startup. Returns empty dict on failure."""
    prices = fetch_openrouter_prices()
    if prices:
        logger.info("[llm_cost] Loaded pricing for %d models from OpenRouter", len(prices))
    else:
        logger.warning("[llm_cost] Could not fetch OpenRouter prices; cost tracking disabled")
    return prices


def get_model_price(model: str, prices: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    """Get (input, output) price per 1M tokens, or None if unknown."""
    if model in prices:
        return prices[model]
    best_match = ""
    for candidate in prices:
        if model.startswith(candidate) and len(candidate) > len(best_match):
            best_match = candidate
    if best_match:
        return prices[best_match]
    return None


def write_cost_file(game_dir: Path, username: str, cost: float) -> None:
    """Write cumulative cost to a JSON file for the observer client to read."""
    cost_file = game_dir / f"{username}_cost.json"
    tmp_file = cost_file.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps({"cost_usd": cost}))
        tmp_file.rename(cost_file)
    except OSError as e:
        logger.error("[llm_cost] Failed to write cost file: %s", e)
