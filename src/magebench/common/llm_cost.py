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
    # Locally served OpenAI-compatible endpoint (vLLM). Override the host with
    # MAGEBENCH_LOCAL_BASE_URL; the API key is ignored by vLLM but the client
    # still requires a non-empty string.
    "local": os.environ.get("MAGEBENCH_LOCAL_BASE_URL", "http://127.0.0.1:8000/v1"),
}
_PROVIDER_API_KEY_ENVS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "local": "MAGEBENCH_LOCAL_API_KEY",
}
SUPPORTED_LLM_PROVIDERS = tuple(_PROVIDER_BASE_URLS)
_OPENROUTER_HOSTS = frozenset({"openrouter.ai"})


def _resolve_llm_provider(provider: str | None) -> str:
    """Resolve a provider slug to a supported value."""
    if provider is None:
        return DEFAULT_LLM_PROVIDER
    if provider in _PROVIDER_BASE_URLS:
        return provider
    supported = ", ".join(SUPPORTED_LLM_PROVIDERS)
    raise ValueError(f"Unknown LLM provider: {provider!r}. Supported providers: {supported}.")


def llm_base_url(provider: str | None) -> str:
    """Map a provider slug to its OpenAI-compatible base URL."""
    return _PROVIDER_BASE_URLS[_resolve_llm_provider(provider)]


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


# CACHED INPUT IS NOT FULL PRICE, and this workload is almost entirely cached prefix. Measured on
# run A (job 7860, 200 games): 455.6M prompt tokens against 10.2M genuinely new ones, so 97.8% of
# input is a re-read of the prefix. Billing it at 1.0x overstated the invoice by ~20x.
#   read  0.1x   (Anthropic cache read; OpenAI's automatic prefix cache is also a discount)
#   write 1.25x  (Anthropic cache write premium; OpenAI charges no write premium)
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def _usage_int(usage: object, *names: str) -> int:
    """First present, non-None integer among `names`, walking one level of nesting.

    Providers report cache usage under different names and shapes: OpenAI-compatible responses put
    cached_tokens under prompt_tokens_details, Anthropic's native fields are
    cache_read_input_tokens / cache_creation_input_tokens. A missing field means "not reported",
    which is 0 tokens at the discount and therefore the conservative reading.
    """
    for name in names:
        if "." in name:
            outer, inner = name.split(".", 1)
            holder = getattr(usage, outer, None)
            value = getattr(holder, inner, None) if holder is not None else None
        else:
            value = getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return 0


def call_cost(usage: object, price: tuple[float, float]) -> tuple[float, float, int]:
    """(quote, uncached_bound, cached_tokens) in USD for one call.

    quote           what the provider actually bills: fresh input at 1x, cache reads at 0.1x,
                    cache writes at 1.25x, output at its own rate.
    uncached_bound  every input token at 1x. It is an UPPER BOUND on the quote and the number a
                    spend cap should use, because a cap that trusts the discount and is wrong
                    overspends, while a cap that ignores it merely stops early.
    cached_tokens   what the provider said was served from cache, for the record.
    """
    prompt = _usage_int(usage, "prompt_tokens", "input_tokens")
    completion = _usage_int(usage, "completion_tokens", "output_tokens")
    cached = _usage_int(usage, "prompt_tokens_details.cached_tokens", "cache_read_input_tokens")
    written = _usage_int(usage, "cache_creation_input_tokens", "prompt_tokens_details.cache_creation_tokens")
    fresh = max(0, prompt - cached - written)
    in_rate, out_rate = price
    quote = (fresh + cached * CACHE_READ_MULTIPLIER + written * CACHE_WRITE_MULTIPLIER) * in_rate / 1_000_000
    quote += completion * out_rate / 1_000_000
    bound = (prompt * in_rate + completion * out_rate) / 1_000_000
    return quote, bound, cached


def write_cost_file(
    game_dir: Path,
    username: str,
    cost: float,
    uncached_bound: float | None = None,
    cached_tokens: int | None = None,
) -> None:
    """Write cumulative cost to a JSON file for the observer client to read.

    cost_usd stays the quote (what consumers already read). uncached_bound_usd is the cap's number,
    and cached_tokens records how much of the input the provider actually served from cache -- a
    quote with 0 cached tokens on a long prefix means caching is NOT working, which is a finding and
    not a rounding error.
    """
    cost_file = game_dir / f"{username}_cost.json"
    tmp_file = cost_file.with_suffix(".tmp")
    payload: dict[str, float | int] = {"cost_usd": cost}
    if uncached_bound is not None:
        payload["uncached_bound_usd"] = uncached_bound
    if cached_tokens is not None:
        payload["cached_tokens"] = cached_tokens
    try:
        tmp_file.write_text(json.dumps(payload))
        tmp_file.rename(cost_file)
    except OSError as e:
        logger.error("[llm_cost] Failed to write cost file: %s", e)
