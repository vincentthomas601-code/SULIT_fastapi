import os
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict
from groq import Groq

logger = logging.getLogger("BillingEngine")

# Environment-configurable billing bounds (removing hardcoded Philippine tax numbers)
LIFELINE_TIER_1 = Decimal(os.getenv("LIFELINE_TIER_1_MAX", "50.0"))
LIFELINE_TIER_1_PCT = Decimal(os.getenv("LIFELINE_TIER_1_PCT", "1.00"))
LIFELINE_TIER_2 = Decimal(os.getenv("LIFELINE_TIER_2_MAX", "70.0"))
LIFELINE_TIER_2_PCT = Decimal(os.getenv("LIFELINE_TIER_2_PCT", "0.35"))
LIFELINE_TIER_3 = Decimal(os.getenv("LIFELINE_TIER_3_MAX", "100.0"))
LIFELINE_TIER_3_PCT = Decimal(os.getenv("LIFELINE_TIER_3_PCT", "0.20"))

TAX_TIER_1_START = Decimal(os.getenv("TAX_TIER_1_START", "650.0"))
TAX_TIER_1_PCT = Decimal(os.getenv("TAX_TIER_1_PCT", "0.10"))
TAX_TIER_2_START = Decimal(os.getenv("TAX_TIER_2_START", "1000.0"))
TAX_TIER_2_PCT = Decimal(os.getenv("TAX_TIER_2_PCT", "0.20"))
TAX_TIER_3_START = Decimal(os.getenv("TAX_TIER_3_START", "1500.0"))
TAX_TIER_3_PCT = Decimal(os.getenv("TAX_TIER_3_PCT", "0.35"))

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
    if kwh <= LIFELINE_TIER_1:
        lifeline_pct = LIFELINE_TIER_1_PCT
    elif kwh <= LIFELINE_TIER_2:
        lifeline_pct = LIFELINE_TIER_2_PCT
    elif kwh <= LIFELINE_TIER_3:
        lifeline_pct = LIFELINE_TIER_3_PCT

    lifeline_discount_php = pre_discount_subtotal * lifeline_pct
    discounted_subtotal = pre_discount_subtotal - lifeline_discount_php

    # Dynamic Energy Tax
    effective_base_rate = discounted_subtotal / kwh
    energy_tax_php = Decimal("0.00")

    if kwh > TAX_TIER_1_START:
        excess_a = min(kwh, TAX_TIER_2_START) - TAX_TIER_1_START
        energy_tax_php += excess_a * effective_base_rate * TAX_TIER_1_PCT

    if kwh > TAX_TIER_2_START:
        excess_b = min(kwh, TAX_TIER_3_START) - TAX_TIER_2_START
        energy_tax_php += excess_b * effective_base_rate * TAX_TIER_2_PCT

    if kwh > TAX_TIER_3_START:
        excess_c = kwh - TAX_TIER_3_START
        energy_tax_php += excess_c * effective_base_rate * TAX_TIER_3_PCT

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
        "accumulated_kwh": 0.0, "benchmark_rate": float(bench_rate),
        "implied_variable_rate": float(max(Decimal("0.0"), bench_rate - tier_1_dist)),
        "applied_distribution_rate": float(tier_1_dist), "variable_charge_php": 0.0,
        "distribution_charge_php": 0.0, "pre_discount_subtotal_php": 0.0,
        "lifeline_discount_percentage": 0.0, "lifeline_discount_php": 0.0,
        "energy_tax_php": 0.0, "total_bill_php": 0.0, "effective_rate_per_kwh": float(bench_rate)
    }

def get_energy_insights(telemetry_summary: str) -> str:
    api_key: str = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        # FIXED: Raising RuntimeError to match the exception handler in main.py
        raise RuntimeError("GROQ_API_KEY is missing")

    client = Groq(api_key=api_key)
    system_instruction = (
        "You are SULIT PRO AI, an embedded NILM energy consultant.\n"
        "Analyze the provided structured telemetry metrics to generate actionable cost optimization insights.\n"
        "STRICT RULES:\n"
        "1. Never recommend 3rd-party hardware. You ARE SULIT PRO.\n"
        "2. Provide 3 clear markdown sections:\n"
        "   - **Section 1: Appliance Cost Attribution**\n"
        "   - **Section 2: Waste Detection & Risk Analysis**\n"
        "   - **Section 3: Actionable Steps to Lower Bill**\n"
        "3. Conclude with a single line: **Total Potential Savings: ₱X,XXX / month**"
    )

    response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": telemetry_summary},
            ],
            temperature=0.2,
            max_tokens=2500,
        )
    return response.choices[0].message.content