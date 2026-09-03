"""
Deterministic stand-in for the LLM agent, used when no Google API key is
configured (USE_STUB_LLM=true or key missing). It calls the exact same
tools as the real agent (so the tool trace / evidence-gathering shape is
identical) but makes its decision with simple hard-coded rules instead of
genuine reasoning. This exists purely so the API, validation loop, and eval
harness can be exercised end-to-end offline; it is NOT a substitute for the
real agent's judgment and is intentionally simple.

Currently implements Scenario 1 (recommendation review), Scenario 2
(supplier shortfall), and Scenario 3 (demand/forecast changed). Other
scenarios fall back to `investigate` with an explanatory note until their
stub logic is added.
"""
from sqlalchemy.orm import Session
from app.tools.schema import dispatch_tool_call


def _call(db, result, name, **args):
    out = dispatch_tool_call(db, name, args)
    result.tool_call_trace.append({"tool": name, "args": args, "result": out})
    if out.get("is_write") and out["result"].get("success"):
        result.action_result = out["result"]
    return out["result"]


def run_stub_agent(db: Session, scenario: str, ctx: dict):
    from app.agent.core import AgentResult
    result = AgentResult()

    if scenario == "recommendation_review":
        _stub_recommendation_review(db, result, ctx)
    elif scenario == "supplier_shortfall":
        _stub_supplier_shortfall(db, result, ctx)
    elif scenario == "demand_spike":
        _stub_demand_spike(db, result, ctx)
    else:
        result.decision = "investigate"
        result.reasoning = f"Stub agent has no rule set yet for scenario '{scenario}'; escalating for safety."
        result.confidence = 0.3
        result.action_summary = "none"
    return result


def _stub_recommendation_review(db: Session, result, ctx: dict):
    sku = ctx["sku"]
    warehouse_id = ctx.get("warehouse_id", "WH-001")
    recommended_qty = ctx["recommended_qty"]

    product = _call(db, result, "get_product", sku=sku)
    inventory = _call(db, result, "get_inventory_status", sku=sku, warehouse_id=warehouse_id)
    forecast = _call(db, result, "get_demand_forecast", sku=sku, warehouse_id=warehouse_id)
    open_pos = _call(db, result, "get_open_pos", sku=sku, warehouse_id=warehouse_id)
    supplier_catalog = _call(db, result, "get_supplier_catalog", sku=sku)
    warehouse = _call(db, result, "get_warehouse", warehouse_id=warehouse_id)

    demand_30d = forecast["forecasts"][0]["projected_total_demand_if_trend_continues"] if forecast["forecasts"] else 0
    on_hand = inventory["total_on_hand"]
    incoming = open_pos["total_incoming_confirmed"]
    coverage_without_new_order = on_hand + incoming
    primary = next((s for s in supplier_catalog["suppliers"] if s["is_primary"]), supplier_catalog["suppliers"][0])

    factors = [
        f"On-hand inventory: {on_hand}",
        f"Incoming from open POs: {incoming}",
        f"Projected 30-day demand: {demand_30d}",
        f"Coverage without new order: {coverage_without_new_order}",
        f"Recommended new order: {recommended_qty}",
    ]

    gap = demand_30d - coverage_without_new_order
    if gap <= 0:
        # Already fully covered -- recommendation is unnecessary or should be trimmed.
        result.decision = "reject" if recommended_qty > 0 else "accept"
        result.reasoning = (
            f"Existing inventory ({on_hand}) plus incoming confirmed POs ({incoming}) already covers "
            f"projected {demand_30d}-unit demand over the forecast window with a surplus of {-gap} units. "
            f"The recommended additional order of {recommended_qty} units is not justified by demand."
        )
        result.confidence = 0.85
        result.action_summary = "No purchase order proposed; recommendation rejected as unnecessary given existing coverage."
    else:
        target_qty = max(gap, primary["moq"])
        # round up to nearest MOQ multiple
        if target_qty % primary["moq"] != 0:
            target_qty = ((target_qty // primary["moq"]) + 1) * primary["moq"]
        cost = target_qty * primary["unit_cost"]
        if warehouse.get("available_budget", 0) < cost or warehouse.get("remaining_storage", 0) < target_qty:
            result.decision = "investigate"
            result.reasoning = (
                f"A gap of {gap} units exists, implying an order of about {target_qty} units, but this would "
                f"exceed available budget or storage capacity. Needs buyer input on trade-offs."
            )
            result.confidence = 0.5
            result.action_summary = "none"
        elif target_qty == recommended_qty:
            result.decision = "accept"
            result.reasoning = f"Computed required order ({target_qty}) matches the recommendation."
            result.confidence = 0.8
            result.action_summary = f"Recommendation of {recommended_qty} accepted as-is."
            _call(db, result, "propose_purchase_order", sku=sku, warehouse_id=warehouse_id,
                  supplier_id=primary["supplier_id"], qty=target_qty)
        else:
            _call(db, result, "propose_purchase_order", sku=sku, warehouse_id=warehouse_id,
                  supplier_id=primary["supplier_id"], qty=target_qty)
            result.decision = "modify"
            result.reasoning = (
                f"Demand gap after existing coverage is {gap} units, not {recommended_qty}. "
                f"Adjusted order down/up to {target_qty} units (rounded to MOQ of {primary['moq']})."
            )
            result.confidence = 0.75
            result.action_summary = f"Proposed a purchase order for {target_qty} units instead of the recommended {recommended_qty}."

    result.factors_considered = factors


def _stub_supplier_shortfall(db: Session, result, ctx: dict):
    sku = ctx["sku"]
    warehouse_id = ctx["warehouse_id"]
    po_id = ctx["po_id"]
    ordered_qty = ctx["ordered_qty"]
    confirmed_qty = ctx["confirmed_qty"]
    original_supplier_id = ctx["supplier_id"]
    shortfall = ordered_qty - confirmed_qty

    inventory = _call(db, result, "get_inventory_status", sku=sku, warehouse_id=warehouse_id)
    forecast = _call(db, result, "get_demand_forecast", sku=sku, warehouse_id=warehouse_id)
    open_pos = _call(db, result, "get_open_pos", sku=sku, warehouse_id=warehouse_id)
    alternates = _call(db, result, "get_alternate_suppliers", sku=sku, exclude_supplier_id=original_supplier_id)
    warehouse = _call(db, result, "get_warehouse", warehouse_id=warehouse_id)

    demand_30d = forecast["forecasts"][0]["projected_total_demand_if_trend_continues"] if forecast["forecasts"] else 0
    on_hand = inventory["total_on_hand"]
    # get_open_pos already reflects the supplier's reduced confirmed_qty -- no double counting the shortfall.
    incoming = open_pos["total_incoming_confirmed"]
    coverage = on_hand + incoming
    gap = demand_30d - coverage

    factors = [
        f"PO {po_id}: ordered {ordered_qty}, supplier confirmed only {confirmed_qty} (shortfall of {shortfall})",
        f"On-hand inventory: {on_hand}",
        f"Total incoming confirmed across open POs (post-shortfall): {incoming}",
        f"Projected 30-day demand: {demand_30d}",
    ]

    if gap <= 0:
        result.decision = "accept_shortfall"
        result.reasoning = (
            f"Even after the supplier's shortfall on {po_id} (confirmed {confirmed_qty} of {ordered_qty}), "
            f"on-hand inventory ({on_hand}) plus all confirmed incoming ({incoming}) still covers projected "
            f"{demand_30d}-unit demand. No further sourcing action is needed."
        )
        result.confidence = 0.8
        result.action_summary = "No action taken; existing inventory and incoming supply are sufficient despite the shortfall."
    else:
        candidates = alternates["alternate_suppliers"]
        chosen = min(candidates, key=lambda s: s["lead_time_days"]) if candidates else None
        factors.append(
            f"Alternate suppliers considered: {[s['supplier_id'] for s in candidates]}" if candidates
            else "No alternate suppliers on file for this SKU"
        )

        if chosen is None:
            result.decision = "escalate"
            result.reasoning = (
                f"A gap of {gap} units remains after the shortfall, but no alternate supplier is on file for "
                f"{sku}. Needs buyer input on how to source the remainder."
            )
            result.confidence = 0.4
            result.action_summary = "none"
            _call(db, result, "escalate_to_human",
                  reason=f"Supplier shortfall on {po_id} leaves a {gap}-unit gap with no alternate supplier available.",
                  context={"po_id": po_id, "sku": sku, "gap": gap})
        else:
            target_qty = max(gap, chosen["moq"])
            if target_qty % chosen["moq"] != 0:
                target_qty = ((target_qty // chosen["moq"]) + 1) * chosen["moq"]
            cost = target_qty * chosen["unit_cost"]
            if warehouse.get("available_budget", 0) < cost or warehouse.get("remaining_storage", 0) < target_qty:
                result.decision = "escalate"
                result.reasoning = (
                    f"A gap of {gap} units remains after the shortfall, implying an order of about {target_qty} "
                    f"units from {chosen['supplier_id']}, but this would exceed available budget or storage. "
                    f"Needs buyer input on trade-offs."
                )
                result.confidence = 0.4
                result.action_summary = "none"
                _call(db, result, "escalate_to_human",
                      reason=f"Closing the {gap}-unit gap from {po_id}'s shortfall would violate budget/storage.",
                      context={"po_id": po_id, "sku": sku, "gap": gap, "candidate_supplier": chosen["supplier_id"]})
            else:
                _call(db, result, "propose_purchase_order", sku=sku, warehouse_id=warehouse_id,
                      supplier_id=chosen["supplier_id"], qty=target_qty)
                result.decision = "additional_po"
                result.reasoning = (
                    f"The shortfall on {po_id} leaves a {gap}-unit gap against demand. Proposing a new order for "
                    f"{target_qty} units from {chosen['supplier_id']} ({chosen['lead_time_days']}-day lead time), "
                    f"chosen over the original supplier for faster/available fulfillment."
                )
                result.confidence = 0.75
                result.action_summary = f"Proposed a new purchase order for {target_qty} units from {chosen['supplier_id']} to cover the shortfall."

    result.factors_considered = factors


def _stub_demand_spike(db: Session, result, ctx: dict):
    sku = ctx["sku"]
    warehouse_id = ctx["warehouse_id"]
    forecast_qty = ctx["forecast_qty"]
    actual_qty_to_date = ctx["actual_qty_to_date"]
    days_elapsed = ctx["days_elapsed"]

    inventory = _call(db, result, "get_inventory_status", sku=sku, warehouse_id=warehouse_id)
    forecast = _call(db, result, "get_demand_forecast", sku=sku, warehouse_id=warehouse_id)
    open_pos = _call(db, result, "get_open_pos", sku=sku, warehouse_id=warehouse_id)
    supplier_catalog = _call(db, result, "get_supplier_catalog", sku=sku)
    warehouse = _call(db, result, "get_warehouse", warehouse_id=warehouse_id)

    demand = forecast["forecasts"][0]["projected_total_demand_if_trend_continues"] if forecast["forecasts"] else 0
    daily_rate = actual_qty_to_date / days_elapsed if days_elapsed else 0
    naive_rate = forecast_qty / forecast["forecasts"][0]["period_days"] if forecast["forecasts"] else 0
    on_hand = inventory["total_on_hand"]
    incoming = open_pos["total_incoming_confirmed"]
    coverage = on_hand + incoming
    gap = demand - coverage

    factors = [
        f"Original forecast: {forecast_qty} units (implied {naive_rate:.1f}/day)",
        f"Actual sales {days_elapsed} days in: {actual_qty_to_date} units (implied {daily_rate:.1f}/day)",
        f"Projected demand at current pace: {demand}",
        f"On-hand inventory: {on_hand}",
        f"Incoming from existing open PO(s): {incoming}",
    ]

    if gap <= 0:
        result.decision = "plan_sufficient"
        result.reasoning = (
            f"Even at the accelerated pace ({daily_rate:.1f}/day vs {naive_rate:.1f}/day planned), on-hand "
            f"inventory ({on_hand}) plus the existing open purchase order ({incoming}) still covers projected "
            f"demand ({demand}). The purchasing plan does not need to change."
        )
        result.confidence = 0.8
        result.action_summary = "No action taken; the existing plan already covers the spiked demand."
    else:
        primary = next((s for s in supplier_catalog["suppliers"] if s["is_primary"]), supplier_catalog["suppliers"][0])
        target_qty = max(gap, primary["moq"])
        if target_qty % primary["moq"] != 0:
            target_qty = ((target_qty // primary["moq"]) + 1) * primary["moq"]
        cost = target_qty * primary["unit_cost"]
        if warehouse.get("available_budget", 0) < cost or warehouse.get("remaining_storage", 0) < target_qty:
            result.decision = "escalate"
            result.reasoning = (
                f"Demand has spiked to {daily_rate:.1f}/day (vs {naive_rate:.1f}/day planned), leaving a {gap:.0f}-unit "
                f"gap against the existing plan. Closing it would need about {target_qty} more units, but that would "
                f"exceed available budget or storage. Needs buyer input on trade-offs."
            )
            result.confidence = 0.4
            result.action_summary = "none"
            _call(db, result, "escalate_to_human",
                  reason=f"Demand spike on {sku} leaves a {gap:.0f}-unit gap that would violate budget/storage.",
                  context={"sku": sku, "warehouse_id": warehouse_id, "gap": gap})
        else:
            _call(db, result, "propose_purchase_order", sku=sku, warehouse_id=warehouse_id,
                  supplier_id=primary["supplier_id"], qty=target_qty)
            result.decision = "increase_order"
            result.reasoning = (
                f"Demand has spiked to {daily_rate:.1f}/day (vs {naive_rate:.1f}/day planned), so the existing "
                f"purchase order is no longer sufficient. Proposing a supplementary order for {target_qty} units "
                f"(rounded to MOQ of {primary['moq']}) to close the {gap:.0f}-unit gap."
            )
            result.confidence = 0.75
            result.action_summary = f"Proposed a supplementary purchase order for {target_qty} units to cover the demand spike."

    result.factors_considered = factors
