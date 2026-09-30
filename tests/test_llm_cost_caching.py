"""Cache-aware call pricing: the quote, the uncached bound, and what "not reported" means.

Measured motivation (run A, job 7860, 200 games): 455.6M prompt tokens against 10.2M genuinely new
ones, i.e. 97.8% of input is a re-read of the append-only prefix. Billing that at 1.0x overstated
the invoice by roughly 20x, which is the difference between ~$355 and ~$21 for 50 deals at
Sonnet-class prices.
"""

import json
from types import SimpleNamespace

from magebench.common.llm_cost import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    call_cost,
    write_cost_file,
)

PRICE = (3.0, 15.0)  # $/1M in, $/1M out


def openai_usage(prompt, completion, cached=None):
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details)


def anthropic_usage(prompt, completion, read=0, written=0):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=written,
    )


def test_no_cache_reported_is_the_old_full_price():
    quote, bound, cached = call_cost(openai_usage(1_000_000, 10_000), PRICE)
    assert cached == 0
    # nothing reported cached -> nothing discounted. Tolerance, not equality: the quote sums two
    # divisions and the bound divides once, so they differ in the last bit.
    assert abs(quote - bound) < 1e-12
    assert abs(bound - (3.0 + 0.15)) < 1e-9


def test_cached_read_is_a_tenth():
    # 1M prompt of which 900k served from cache
    quote, bound, cached = call_cost(openai_usage(1_000_000, 0, cached=900_000), PRICE)
    assert cached == 900_000
    expected = (100_000 + 900_000 * CACHE_READ_MULTIPLIER) * 3.0 / 1e6
    assert abs(quote - expected) < 1e-9
    assert abs(bound - 3.0) < 1e-9             # the bound ignores the discount, by design
    assert quote < bound / 3


def test_cache_write_carries_its_premium():
    quote, _, _ = call_cost(anthropic_usage(1_000_000, 0, read=0, written=1_000_000), PRICE)
    assert abs(quote - 1_000_000 * CACHE_WRITE_MULTIPLIER * 3.0 / 1e6) < 1e-9


def test_read_and_write_together_leave_the_rest_fresh():
    quote, bound, cached = call_cost(anthropic_usage(1_000, 0, read=600, written=300), PRICE)
    fresh = 1_000 - 600 - 300
    expected = (fresh + 600 * CACHE_READ_MULTIPLIER + 300 * CACHE_WRITE_MULTIPLIER) * 3.0 / 1e6
    assert abs(quote - expected) < 1e-12
    assert cached == 600
    assert quote < bound


def test_cached_larger_than_prompt_never_goes_negative():
    # a provider reporting cached >= prompt must not produce a negative fresh count
    quote, _, _ = call_cost(openai_usage(500, 0, cached=900), PRICE)
    assert quote >= 0


def test_output_is_never_discounted():
    quote, bound, _ = call_cost(openai_usage(0, 100_000, cached=0), PRICE)
    assert abs(quote - 1.5) < 1e-9 and abs(bound - 1.5) < 1e-9


def test_alternative_field_names():
    # Anthropic's native input_tokens/output_tokens spelling
    quote, bound, _ = call_cost(SimpleNamespace(input_tokens=1_000, output_tokens=100), PRICE)
    assert abs(bound - (1_000 * 3.0 + 100 * 15.0) / 1e6) < 1e-12
    assert abs(quote - bound) < 1e-12


def test_cost_file_carries_quote_bound_and_cached(tmp_path):
    write_cost_file(tmp_path, "Eval00", 1.25, 20.0, 900_000)
    payload = json.loads((tmp_path / "Eval00_cost.json").read_text())
    assert payload == {"cost_usd": 1.25, "uncached_bound_usd": 20.0, "cached_tokens": 900_000}


def test_cost_file_stays_backwards_compatible(tmp_path):
    # older callers pass the quote only; cost_usd is what existing consumers read
    write_cost_file(tmp_path, "Eval01", 0.5)
    assert json.loads((tmp_path / "Eval01_cost.json").read_text()) == {"cost_usd": 0.5}
