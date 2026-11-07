"""Cost metering. Money is stored as integer nano-USD so sums are exact."""
from decimal import Decimal

NANO = 1_000_000_000


def compute_cost_nano(pricing, model, prompt_tokens, completion_tokens):
    """cost = prompt*input_price/1M + completion*output_price/1M, using provider-reported tokens."""
    if model not in pricing:
        return 0
    inp, out = pricing[model]
    usd = (Decimal(prompt_tokens) * inp + Decimal(completion_tokens) * out) / Decimal(1_000_000)
    return int((usd * NANO).to_integral_value())


def nano_to_usd(nano):
    return nano / NANO


def format_usd(nano):
    return f"{nano / NANO:.9f}"
