import os
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict
from groq import Groq

logger = logging.getLogger("BillingEngine")


def _to_decimal(val: float) -> Decimal:
    return Decimal(str(val))


def calculate_ph_electricity_bill(
    net_cycle_kwh: float,
    benchmark_rate: float,
    provider_rule: Any
) -> Dict[str, Any]:
    kwh = max(Decimal("0.0"), _to_decimal(net_cycle_kwh))
    bench_rate = _to_decimal(benchmark_rate)
    tier_1_dist = _to_decimal(provider_rule.tier_1_rate)

    if kwh <= Decimal("0.0"):
        return _build_zero_response(bench_rate, tier_1_dist)

    implied_variable_rate = max(Decimal("0.0"), bench_rate - tier_1_dist)

    if kwh <= _to_decimal(provider_rule.tier_1_max_kwh):
        applied_dist_rate = _to_decimal(provider_rule.tier_1_rate)
    elif kwh <= _to_decimal(provider_rule.tier_2_max_kwh):
        applied_dist_rate = _to_decimal(provider_rule.tier_2_rate)
    elif kwh <= _to_decimal(provider_rule.tier_3_max_kwh):
        applied_dist_rate = _to_decimal(provider_rule.tier_3_rate)
    else:
        applied_dist_rate = _to_decimal(provider_rule.tier_4_rate)

    variable_charge = kwh * implied_variable_rate
    distribution_charge = kwh * applied_dist_rate
    pre_discount_subtotal = variable_charge + distribution_charge

    # Dynamic Lifeline Discount Matrix
    lifeline_pct = Decimal("0.00")
    if kwh <= Decimal("20.0"):
        lifeline_pct = Decimal("1.00")
    elif kwh <= Decimal("50.0"):
        lifeline_pct = Decimal("0.50")
    elif kwh <= Decimal("70.0"):
        lifeline_pct = Decimal("0.20")
    elif kwh <= Decimal("100.0"):
        lifeline_pct = Decimal("0.05")

    lifeline_discount_php = pre_discount_subtotal * lifeline_pct
    discounted_subtotal = pre_discount_subtotal - lifeline_discount_php

    # Dynamic Energy Tax
    effective_base_rate = discounted_subtotal / kwh
    energy_tax_php = Decimal("0.00")

    if kwh > Decimal("650.0"):
        excess = min(kwh, Decimal("1000.0")) - Decimal("650.0")
        energy_tax_php += excess * effective_base_rate * Decimal("0.10")
    if kwh > Decimal("1000.0"):
        excess = min(kwh, Decimal("1500.0")) - Decimal("1000.0")
        energy_tax_php += excess * effective_base_rate * Decimal("0.20")
    if kwh > Decimal("1500.0"):
        excess = kwh - Decimal("1500.0")
        energy_tax_php += excess * effective_base_rate * Decimal("0.35")

    total_bill_php = discounted_subtotal + energy_tax_php
    effective_rate = total_bill_php / kwh

    def _r2(d: Decimal) -> float:
        return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _r4(d: Decimal) -> float:
        return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

    return {
        "accumulated_kwh": _r4(kwh),
        "benchmark_rate": _r4(bench_rate),
        "implied_variable_rate": _r4(implied_variable_rate),
        "applied_distribution_rate": _r4(applied_dist_rate),
        "variable_charge_php": _r2(variable_charge),
        "distribution_charge_php": _r2(distribution_charge),
        "pre_discount_subtotal_php": _r2(pre_discount_subtotal),
        "lifeline_discount_percentage": _r2(lifeline_pct * Decimal("100")),
        "lifeline_discount_php": _r2(lifeline_discount_php),
        "energy_tax_php": _r2(energy_tax_php),
        "total_bill_php": _r2(total_bill_php),
        "effective_rate_per_kwh": _r4(effective_rate)
    }


def _build_zero_response(bench_rate: Decimal, tier_1_dist: Decimal) -> Dict[str, Any]:
    return {
        "accumulated_kwh": 0.0,
        "benchmark_rate": float(bench_rate),
        "implied_variable_rate": float(max(Decimal("0.0"), bench_rate - tier_1_dist)),
        "applied_distribution_rate": float(tier_1_dist),
        "variable_charge_php": 0.0,
        "distribution_charge_php": 0.0,
        "pre_discount_subtotal_php": 0.0,
        "lifeline_discount_percentage": 0.0,
        "lifeline_discount_php": 0.0,
        "energy_tax_php": 0.0,
        "total_bill_php": 0.0,
        "effective_rate_per_kwh": float(bench_rate)
    }


def get_energy_insights(telemetry_summary: str) -> str:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing from environment variables.")

    client = Groq(api_key=api_key)

    system_instruction = (
        "You are SULIT AI, a Philippine residential energy efficiency analyst. "
        "Provide clear, concise, actionable recommendations in Philippine Pesos (₱) based ONLY on the provided usage data. "
        "Do not invent or estimate total potential savings unless calculable directly from the provided data."
    )

    # Groq flagship production model (GPT-OSS 120B) with GPT-OSS 20B fallback
    primary_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    models_to_try = list(dict.fromkeys([primary_model, "openai/gpt-oss-120b", "openai/gpt-oss-20b"]))

    last_error = None
    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": telemetry_summary},
                ],
                temperature=0.2,
                max_tokens=1500,
            )
            content = (response.choices[0].message.content or "").strip()
            if content:
                return content
            raise RuntimeError(f"Groq model '{model_name}' returned an empty response.")
        except Exception as err:
            logger.warning(f"Groq model '{model_name}' failed: {err}")
            last_error = err
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError("No Groq models were available to generate energy insights.")