from __future__ import annotations

from app.config import MODEL_PRICING, settings
from app.models import EstimateResult


def calculate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING[model]
    input_cost = (input_tokens / 1_000_000) * pricing.input_per_million
    output_cost = (output_tokens / 1_000_000) * pricing.output_per_million
    return round(input_cost + output_cost, 6)


def estimate_from_source_tokens(model: str, source_tokens: int) -> EstimateResult:
    estimated_output = int(source_tokens * settings.output_token_factor)
    estimated_cost = calculate_cost_usd(model, source_tokens, estimated_output)
    return EstimateResult(
        model=model,
        source_tokens=source_tokens,
        estimated_output_tokens=estimated_output,
        estimated_cost_usd=estimated_cost,
    )
