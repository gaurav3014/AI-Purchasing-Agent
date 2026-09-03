"""
Evaluation harness for Scenario 3 (Demand/Forecast Has Changed).

Same shape and philosophy as eval/scenario1_eval.py and scenario2_eval.py:
a small, readable script (not a framework) that runs a few scripted cases
against the live agent pipeline (the same `run_scenario` orchestrator the
API uses) and checks, per case:

  - did the agent gather the necessary evidence (inventory, demand, open POs)?
  - was the decision the expected one?
  - did it respect hard constraints (no policy violations in any proposal)?
  - did post-action validation pass?
  - for the "no action needed" case, did it correctly avoid proposing one?

Case 1 reuses the seeded SKU-014/PO-14001 spike as-is. Cases 2 and 3 define
their own throwaway product/warehouse/supplier fixtures inline (rather than
touching seed.py) to isolate two edge cases seed.py's single Scenario 3
narrative doesn't cover: a spike the existing plan already absorbs, and one
where closing the gap would blow the budget.

Run with:  USE_STUB_LLM=true python eval/scenario3_eval.py     (offline, deterministic)
       or: python eval/scenario3_eval.py                        (uses the real Gemini agent)
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import SessionLocal
from app.seed import seed
from app.tools import read_tools
from app.api.orchestrator import run_scenario
from app.models import Product, Warehouse, Inventory, DemandForecast, SupplierCatalog, PurchaseOrder

NOW = datetime(2026, 9, 2)
REQUIRED_EVIDENCE_TOOLS = {"get_inventory_status", "get_demand_forecast", "get_open_pos"}


def tools_called(log) -> set:
    return {c["tool"] for c in json.loads(log.tool_calls)}


def run_case(db, name, forecast_id, expected_decisions, expect_action, expect_validation_pass=True):
    forecast = db.get(DemandForecast, forecast_id)
    product = read_tools.get_product(db, forecast.sku)
    daily_rate = forecast.actual_qty_to_date / forecast.days_elapsed if forecast.days_elapsed else 0

    situation_prompt = (
        f"SITUATION (Scenario 3 -- Demand/Forecast Has Changed): SKU {forecast.sku} ('{product['name']}') at "
        f"warehouse {forecast.warehouse_id} was forecast at {forecast.forecast_qty} units over "
        f"{forecast.period_days} days, but {forecast.days_elapsed} days in, actual sales are already "
        f"{forecast.actual_qty_to_date} units ({daily_rate:.1f}/day). There is an existing open purchase order, "
        f"but current inventory and incoming quantity may no longer be sufficient. Determine whether the "
        f"purchasing plan needs to change."
    )
    trigger_event = {
        "type": "demand_spike", "sku": forecast.sku, "warehouse_id": forecast.warehouse_id,
        "forecast_id": forecast.id, "forecast_qty": forecast.forecast_qty,
        "actual_qty_to_date": forecast.actual_qty_to_date, "days_elapsed": forecast.days_elapsed,
    }
    log = run_scenario(
        db, scenario="demand_spike", situation_prompt=situation_prompt,
        allowed_decisions=["plan_sufficient", "increase_order", "escalate"], trigger_event=trigger_event,
        warehouse_id=forecast.warehouse_id,
        validator_kwargs={"sku": forecast.sku, "warehouse_id": forecast.warehouse_id, "action_result": None},
    )

    checks = {}
    called = tools_called(log)
    checks["gathered_required_evidence"] = REQUIRED_EVIDENCE_TOOLS.issubset(called)
    checks["decision_as_expected"] = log.decision in expected_decisions
    checks["action_expectation_met"] = (log.action_taken != "none") == expect_action
    if expect_validation_pass:
        checks["validation_passed"] = bool(log.validation_passed)
    else:
        checks["validation_correctly_failed_and_escalated"] = (not log.validation_passed) and log.requires_human_approval
    policy_violation_in_action = False
    if log.action_taken != "none":
        action = json.loads(log.action_taken)
        policy_violation_in_action = bool(action.get("policy_violations"))
    checks["no_unjustified_policy_override"] = not policy_violation_in_action

    passed = all(checks.values())
    return {
        "case": name, "forecast_id": forecast_id, "sku": forecast.sku,
        "decision": log.decision, "confidence": log.confidence,
        "requires_human_approval": log.requires_human_approval,
        "checks": checks, "passed": passed,
        "reasoning": log.reasoning,
    }


def _make_fixture(db, sku, warehouse_id, unit_cost, budget, on_hand, safety_stock,
                   forecast_qty, actual_qty_to_date, days_elapsed,
                   ordered_qty, confirmed_qty, supplier_id, moq, lead_time_days) -> int:
    """Inserts a throwaway product/warehouse/supplier/PO/forecast for one eval
    case. Returns the new DemandForecast's id."""
    db.add(Product(sku=sku, name=sku, category="eval", unit_cost=unit_cost, unit_volume=1.0))
    db.add(Warehouse(warehouse_id=warehouse_id, name=warehouse_id,
                      available_budget=budget, max_storage=1000, current_storage=100))
    db.add(Inventory(sku=sku, warehouse_id=warehouse_id, on_hand_qty=on_hand, safety_stock=safety_stock))
    db.add(SupplierCatalog(supplier_id=supplier_id, supplier_name=supplier_id, sku=sku,
                            unit_cost=unit_cost, moq=moq, lead_time_days=lead_time_days,
                            reliability_score=0.9, is_primary=True))
    db.add(PurchaseOrder(po_id=f"PO-{sku}", sku=sku, warehouse_id=warehouse_id, supplier_id=supplier_id,
                          ordered_qty=ordered_qty, confirmed_qty=confirmed_qty, status="open",
                          expected_delivery_date=NOW + timedelta(days=lead_time_days), created_by="system"))
    forecast = DemandForecast(sku=sku, warehouse_id=warehouse_id, period_start=NOW - timedelta(days=days_elapsed),
                               period_days=30, forecast_qty=forecast_qty, actual_qty_to_date=actual_qty_to_date,
                               days_elapsed=days_elapsed, review_status="pending")
    db.add(forecast)
    db.commit()
    db.refresh(forecast)
    return forecast.id


def main():
    db_path = os.path.join(os.path.dirname(__file__), "..", "backend", "purchasing_agent.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    os.chdir(os.path.join(os.path.dirname(__file__), "..", "backend"))
    seed()
    db = SessionLocal()

    results = []

    # Case 1: seeded SKU-014/PO-14001 -- forecast 10/day, actual pace 25/day (2.5x)
    # leaves the existing 200-unit PO well short of ~750 projected demand.
    # Expect the agent to propose a supplementary order.
    sku014_forecast = db.query(DemandForecast).filter_by(sku="SKU-014").one()
    sku014_forecast.review_status = "pending"
    db.commit()
    results.append(run_case(db, "genuine spike, existing plan insufficient", sku014_forecast.id,
                             expected_decisions={"increase_order"}, expect_action=True))

    # Case 2: demand accelerated, but the existing PO plus on-hand already
    # cover the new pace. Expect plan_sufficient, no action.
    fid = _make_fixture(
        db, sku="EVAL-SKU-C", warehouse_id="EVAL-WH-C", unit_cost=2.00, budget=2000.0,
        on_hand=210, safety_stock=20, forecast_qty=300, actual_qty_to_date=150, days_elapsed=10,
        ordered_qty=250, confirmed_qty=250, supplier_id="EVAL-SUP-C1", moq=50, lead_time_days=10,
    )
    results.append(run_case(db, "spike absorbed by existing plan", fid,
                             expected_decisions={"plan_sufficient"}, expect_action=False))

    # Case 3: genuine spike-driven gap, but closing it would blow the budget.
    # Expect escalate.
    fid = _make_fixture(
        db, sku="EVAL-SKU-D", warehouse_id="EVAL-WH-D", unit_cost=50.00, budget=100.0,
        on_hand=10, safety_stock=5, forecast_qty=200, actual_qty_to_date=150, days_elapsed=10,
        ordered_qty=100, confirmed_qty=100, supplier_id="EVAL-SUP-D1", moq=20, lead_time_days=15,
    )
    # escalate_to_human is itself a write-tool call (a recorded action, just
    # not a purchase order), and the underlying gap is genuinely unresolved
    # -- so we expect an action (the escalation) but NOT validation to pass.
    results.append(run_case(db, "genuine gap, budget too tight -> escalate", fid,
                             expected_decisions={"escalate"}, expect_action=True,
                             expect_validation_pass=False))

    db.close()

    print("\n=== Scenario 3 Evaluation Report ===\n")
    n_pass = 0
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        n_pass += r["passed"]
        print(f"[{status}] {r['case']}")
        print(f"    decision={r['decision']} confidence={r['confidence']} needs_approval={r['requires_human_approval']}")
        for check, ok in r["checks"].items():
            print(f"    - {check}: {'ok' if ok else 'FAILED'}")
        if not r["passed"]:
            print(f"    reasoning: {r['reasoning']}")
        print()

    print(f"Summary: {n_pass}/{len(results)} cases passed.")

    report_path = os.path.join(os.path.dirname(__file__), "scenario3_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Full report written to {report_path}")

    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
