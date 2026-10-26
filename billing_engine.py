from typing import Dict, Any

def calculate_ph_electricity_bill(
    accumulated_kwh: float,
    benchmark_rate: float,
    provider_rule: Any
) -> Dict[str, Any]:
    """
    Calculates unbundled Philippine residential electricity bill.
    
    1. Benchmark Deconstruction:
       implied_variable_rate = benchmark_rate - base_distribution_fee
    2. Whole-Consumption Re-Rating (Distribution):
       Retroactively applies distribution rate based on total monthly kWh tier.
    3. ERC Lifeline Discount:
       Applies discount to subtotal if monthly consumption <= 100 kWh.
    4. Marginal Energy Tax (BP 36):
       Applies tax on excess consumption above set thresholds.
    """
    if accumulated_kwh <= 0:
        return {
            "accumulated_kwh": 0.0,
            "benchmark_rate": benchmark_rate,
            "implied_variable_rate": max(0.0, benchmark_rate - provider_rule.tier_1_rate),
            "applied_distribution_rate": provider_rule.tier_1_rate,
            "variable_charge_php": 0.0,
            "distribution_charge_php": 0.0,
            "pre_discount_subtotal_php": 0.0,
            "lifeline_discount_percentage": 0.0,
            "lifeline_discount_php": 0.0,
            "energy_tax_php": 0.0,
            "total_bill_php": 0.0,
            "effective_rate_per_kwh": benchmark_rate
        }

    # Step 1: Deconstruct 200 kWh Benchmark Rate
    base_dist_rate = provider_rule.tier_1_rate
    implied_variable_rate = max(0.0, benchmark_rate - base_dist_rate)

    # Step 2: Whole-Consumption Re-Rating for Distribution Tier
    if accumulated_kwh <= provider_rule.tier_1_max_kwh:           # 1 – 200 kWh
        applied_dist_rate = provider_rule.tier_1_rate
    elif accumulated_kwh <= provider_rule.tier_2_max_kwh:         # 201 – 300 kWh
        applied_dist_rate = provider_rule.tier_2_rate
    elif accumulated_kwh <= provider_rule.tier_3_max_kwh:         # 301 – 400 kWh
        applied_dist_rate = provider_rule.tier_3_rate
    else:                                                          # > 400 kWh
        applied_dist_rate = provider_rule.tier_4_rate

    variable_charge = accumulated_kwh * implied_variable_rate
    distribution_charge = accumulated_kwh * applied_dist_rate
    pre_discount_subtotal = variable_charge + distribution_charge

    # Step 3: ERC Lifeline Discount
    lifeline_discount_pct = 0.0
    if accumulated_kwh <= 50.0:
        lifeline_discount_pct = 1.00      # 100% discount
    elif accumulated_kwh <= 70.0:
        lifeline_discount_pct = 0.35      # 35% discount
    elif accumulated_kwh <= 100.0:
        lifeline_discount_pct = 0.20      # 20% discount

    lifeline_discount_php = pre_discount_subtotal * lifeline_discount_pct
    discounted_subtotal = pre_discount_subtotal - lifeline_discount_php

    # Step 4: Marginal Energy Tax (Batas Pambansa Blg. 36)
    effective_base_rate = discounted_subtotal / accumulated_kwh
    energy_tax_php = 0.0

    if accumulated_kwh > 650.0:
        excess_tier_a = min(accumulated_kwh, 1000.0) - 650.0
        energy_tax_php += excess_tier_a * effective_base_rate * 0.10

    if accumulated_kwh > 1000.0:
        excess_tier_b = min(accumulated_kwh, 1500.0) - 1000.0
        energy_tax_php += excess_tier_b * effective_base_rate * 0.20

    if accumulated_kwh > 1500.0:
        excess_tier_c = accumulated_kwh - 1500.0
        energy_tax_php += excess_tier_c * effective_base_rate * 0.35

    total_bill_php = discounted_subtotal + energy_tax_php
    effective_rate_per_kwh = total_bill_php / accumulated_kwh

    return {
        "accumulated_kwh": round(accumulated_kwh, 4),
        "benchmark_rate": round(benchmark_rate, 4),
        "implied_variable_rate": round(implied_variable_rate, 4),
        "applied_distribution_rate": round(applied_dist_rate, 4),
        "variable_charge_php": round(variable_charge, 2),
        "distribution_charge_php": round(distribution_charge, 2),
        "pre_discount_subtotal_php": round(pre_discount_subtotal, 2),
        "lifeline_discount_percentage": lifeline_discount_pct * 100,
        "lifeline_discount_php": round(lifeline_discount_php, 2),
        "energy_tax_php": round(energy_tax_php, 2),
        "total_bill_php": round(total_bill_php, 2),
        "effective_rate_per_kwh": round(effective_rate_per_kwh, 4)
    }