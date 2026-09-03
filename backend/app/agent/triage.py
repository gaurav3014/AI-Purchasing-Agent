"""
AI-driven problem detection, replacing fixed-threshold rules.

Previously, `services/scanner.py` decided "does this product have a
problem?" using hardcoded math (coverage ratio outside 0.9x-1.5x, etc.).
That's fast and free, but it can only ever catch the exact patterns someone
thought to encode -- a genuinely different kind of problem would never be
noticed. `triage_product` replaces that decision with an actual LLM
judgment call per product: the real data is still fetched the same
deterministic way (via read_tools), but *classifying* it as healthy or
flagging it as one of the three known problem types is now the AI's call,
not a threshold comparison.

This runs once per (sku, warehouse) pair per cron cycle for every pair that
has never been flagged before (see services/scanner.py's one-shot dedup) --
one lightweight LLM call, not the full multi-turn investigate/decide/act
loop used for the actual review afterward. If a problem is detected, it's
routed into the exact same trigger queues (purchase_recommendations,
shortfall_status, review_status) that the full agent engine already knows
how to process -- triage only classifies, it never decides what to DO about
a problem.

Like the rest of the system, falls back to a deterministic stub (the old
fixed-threshold math) when USE_STUB_LLM=true or no API key is configured,
so the whole pipeline stays testable offline.
"""
import json
from app.config import settings
from app.tools import read_tools


def triage_product(db, sku: str, warehouse_id: str) -> dict:
    """Returns {"has_problem": bool, "problem_type": str, "reasoning": str, "suggested_qty": int}."""
    if settings.use_stub_llm or not settings.google_api_key:
        return _stub_triage(db, sku, warehouse_id)
    return _llm_triage(db, sku, warehouse_id)


def _llm_triage(db, sku: str, warehouse_id: str) -> dict:
    import dspy
    from app.agent.prompts import ProductTriage

    product = read_tools.get_product(db, sku)
    inventory = read_tools.get_inventory_status(db, sku, warehouse_id)
    forecast = read_tools.get_demand_forecast(db, sku, warehouse_id)
    open_pos = read_tools.get_open_pos(db, sku, warehouse_id)

    lm = dspy.LM(f"gemini/{settings.google_model}", api_key=settings.google_api_key)
    triage_module = dspy.ChainOfThought(ProductTriage)
    with dspy.context(lm=lm):
        pred = triage_module(
            sku=sku,
            product_name=product.get("name", sku),
            inventory=json.dumps(inventory, default=str),
            demand_forecast=json.dumps(forecast, default=str),
            open_purchase_orders=json.dumps(open_pos, default=str),
        )

    return {
        "has_problem": bool(pred.has_problem),
        "problem_type": pred.problem_type if pred.has_problem else "none",
        "reasoning": pred.reasoning,
        "suggested_qty": max(0, int(pred.suggested_qty or 0)),
    }


def _stub_triage(db, sku: str, warehouse_id: str) -> dict:
    """Deterministic fallback -- the same fixed-threshold math the old
    scanner used, kept only so the pipeline is testable offline without
    burning API quota. Not the intended production path."""
    inventory = read_tools.get_inventory_status(db, sku, warehouse_id)
    forecast = read_tools.get_demand_forecast(db, sku, warehouse_id)
    open_pos = read_tools.get_open_pos(db, sku, warehouse_id)

    for po in open_pos["purchase_orders"]:
        if po["status"] == "open" and po["confirmed_qty"] < po["ordered_qty"]:
            return {
                "has_problem": True, "problem_type": "supplier_shortfall",
                "reasoning": f"[stub] PO {po['po_id']} confirmed {po['confirmed_qty']} of {po['ordered_qty']}.",
                "suggested_qty": 0,
            }

    if not forecast["forecasts"]:
        return {"has_problem": False, "problem_type": "none", "reasoning": "[stub] no forecast on file.", "suggested_qty": 0}

    fc = forecast["forecasts"][0]
    demand = fc["projected_total_demand_if_trend_continues"]
    coverage = inventory["total_on_hand"] + open_pos["total_incoming_confirmed"]
    if not demand:
        return {"has_problem": False, "problem_type": "none", "reasoning": "[stub] no demand signal.", "suggested_qty": 0}

    if fc["days_elapsed"] and fc["implied_daily_run_rate"]:
        naive_rate = fc["forecast_qty"] / fc["period_days"]
        if fc["implied_daily_run_rate"] >= naive_rate * 1.3 and open_pos["purchase_orders"] and coverage < demand:
            return {
                "has_problem": True, "problem_type": "demand_spike",
                "reasoning": f"[stub] run-rate {fc['implied_daily_run_rate']}/day vs planned {naive_rate:.1f}/day, "
                             f"coverage {coverage} < projected demand {demand}.",
                "suggested_qty": 0,
            }

    ratio = coverage / demand
    if ratio < 0.9 or ratio > 1.5:
        return {
            "has_problem": True, "problem_type": "recommendation_needed",
            "reasoning": f"[stub] coverage {coverage} vs demand {demand} (ratio {ratio:.2f}) outside 0.9x-1.5x.",
            "suggested_qty": max(0, round(demand + inventory["total_safety_stock"] - coverage)),
        }

    return {"has_problem": False, "problem_type": "none", "reasoning": "[stub] coverage within healthy range.", "suggested_qty": 0}
