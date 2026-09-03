"""
Deterministic (non-LLM) post-decision validation -- the feedback loop.

After the agent decides (and, if it proposed a purchase order, that
proposal passed guardrails), this re-reads the resulting state from the
database and checks it against explicit expectations, independent of
whatever the agent claimed. If validation fails, the caller
(api/orchestrator.py) feeds the failure back to the agent for one retry
before escalating -- see MAX_RETRIES there.

This module intentionally does NOT re-run the LLM. Its whole purpose is to
be a second, independent, rule-based check on the outcome -- if the
validator also used the LLM, a systematic LLM error could pass its own
check.

Note: under the Proposal Pattern (tools/write_tools.py), a proposed order
is not yet written to purchase_orders -- it's still awaiting human
approval. Validation therefore checks projected coverage *as if* the
proposal were approved (on-hand + confirmed incoming + proposed qty),
since that's the plan the agent is actually recommending.
"""
from sqlalchemy.orm import Session
from app.tools import read_tools


def validate_recommendation_review(db: Session, sku: str, warehouse_id: str, action_result: dict | None) -> dict:
    """
    Expectation: projected coverage (on-hand + all confirmed incoming POs +
    any newly-proposed qty) should be reasonably close to projected demand
    -- not wildly over (waste, storage risk) or under (stockout risk) --
    and the proposal (if any) should not have needed to violate a hard
    constraint to get there.
    """
    inventory = read_tools.get_inventory_status(db, sku, warehouse_id)
    forecast = read_tools.get_demand_forecast(db, sku, warehouse_id)
    open_pos = read_tools.get_open_pos(db, sku, warehouse_id)
    warehouse = read_tools.get_warehouse(db, warehouse_id)

    demand = forecast["forecasts"][0]["projected_total_demand_if_trend_continues"] if forecast["forecasts"] else 0
    proposed_qty = action_result.get("qty", 0) if action_result and action_result.get("success") else 0
    coverage = inventory["total_on_hand"] + open_pos["total_incoming_confirmed"] + proposed_qty

    checks = []
    passed = True

    under_covered = coverage < demand * 0.9
    over_covered = coverage > demand * 2.0
    checks.append({
        "check": "coverage_reasonable",
        "passed": not (under_covered or over_covered),
        "detail": f"coverage={coverage}, demand={demand} "
                  f"({'under-covered' if under_covered else 'over-covered' if over_covered else 'reasonable'})",
    })
    if under_covered or over_covered:
        passed = False

    if warehouse.get("available_budget", 0) < 0:
        checks.append({"check": "within_budget", "passed": False, "detail": "warehouse budget is negative"})
        passed = False
    else:
        checks.append({"check": "within_budget", "passed": True, "detail": "spend within budget"})

    if warehouse.get("current_storage", 0) > warehouse.get("max_storage", float("inf")):
        checks.append({"check": "within_storage", "passed": False, "detail": "used storage exceeds capacity"})
        passed = False
    else:
        checks.append({"check": "within_storage", "passed": True, "detail": "storage within capacity"})

    return {"passed": passed, "checks": checks, "coverage": coverage, "demand": demand}


def validate_supplier_shortfall(db: Session, sku: str, warehouse_id: str, action_result: dict | None) -> dict:
    """
    Expectation: after the agent's response to a supplier shortfall, coverage
    should still land in a reasonable band relative to demand -- the same
    math as Scenario 1's validator, since "is projected coverage sane"
    doesn't change meaning just because the trigger was a shortfall rather
    than a fresh recommendation. get_open_pos already reflects the
    supplier's reduced confirmed_qty (it's a real DB row), so a shortfall
    that was never compensated for shows up here as under-covered.
    """
    return validate_recommendation_review(db, sku, warehouse_id, action_result)


def validate_demand_spike(db: Session, sku: str, warehouse_id: str, action_result: dict | None) -> dict:
    """
    Expectation: same coverage-vs-demand math as the other two validators --
    get_demand_forecast already computes the run-rate-projected demand, so a
    spike that was never compensated for shows up here as under-covered
    exactly like an unresolved shortfall would.
    """
    return validate_recommendation_review(db, sku, warehouse_id, action_result)


VALIDATORS = {
    "recommendation_review": validate_recommendation_review,
    "supplier_shortfall": validate_supplier_shortfall,
    "demand_spike": validate_demand_spike,
}


def validate(scenario: str, db: Session, **kwargs) -> dict:
    fn = VALIDATORS.get(scenario)
    if not fn:
        return {"passed": True, "checks": [], "detail": f"no validator registered yet for scenario '{scenario}'"}
    return fn(db, **kwargs)
