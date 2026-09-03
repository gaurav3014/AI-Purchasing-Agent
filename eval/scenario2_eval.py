"""
Evaluation harness for Scenario 2 (Supplier Cannot Fulfil the Purchase).

Same shape and philosophy as eval/scenario1_eval.py: a small, readable
script (not a framework) that runs a few scripted cases against the live
agent pipeline (the same `run_scenario` orchestrator the API uses) and
checks, per case:

  - did the agent gather the necessary evidence (inventory, demand, open
    POs, and -- when relevant -- alternate suppliers)?
  - was the decision the expected one?
  - did it respect hard constraints (no policy violations in any proposal)?
  - did post-action validation pass?
  - for the "no action needed" case, did it correctly avoid proposing one?

Case 1 reuses the seeded PO-8001 shortfall as-is. Cases 2 and 3 define their
own throwaway product/warehouse/supplier fixtures inline (rather than
touching seed.py) to isolate two edge cases seed.py's single Scenario 2
narrative doesn't cover: a shortfall that turns out not to matter, and one
where no alternate supplier exists at all.

Run with:  USE_STUB_LLM=true python eval/scenario2_eval.py     (offline, deterministic)
       or: python eval/scenario2_eval.py                        (uses the real Gemini agent)
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


def run_case(db, name, po_id, expected_decisions, expect_action, require_alternate_supplier_check=True,
             expect_validation_pass=True):
    po = db.get(PurchaseOrder, po_id)
    product = read_tools.get_product(db, po.sku)
    shortfall = po.ordered_qty - po.confirmed_qty

    situation_prompt = (
        f"SITUATION (Scenario 2 -- Supplier Cannot Fulfil the Purchase): "
        f"Purchase order {po.po_id} for SKU {po.sku} ('{product['name']}') at warehouse {po.warehouse_id} "
        f"was placed with supplier {po.supplier_id} for {po.ordered_qty} units, but the supplier has now "
        f"confirmed only {po.confirmed_qty} units -- a shortfall of {shortfall} units. Determine what should "
        f"happen next."
    )
    trigger_event = {
        "type": "supplier_shortfall", "sku": po.sku, "warehouse_id": po.warehouse_id, "po_id": po.po_id,
        "supplier_id": po.supplier_id, "ordered_qty": po.ordered_qty, "confirmed_qty": po.confirmed_qty,
    }
    log = run_scenario(
        db, scenario="supplier_shortfall", situation_prompt=situation_prompt,
        allowed_decisions=["accept_shortfall", "additional_po", "escalate"], trigger_event=trigger_event,
        warehouse_id=po.warehouse_id,
        validator_kwargs={"sku": po.sku, "warehouse_id": po.warehouse_id, "action_result": None},
    )

    checks = {}
    called = tools_called(log)
    required = set(REQUIRED_EVIDENCE_TOOLS)
    if require_alternate_supplier_check:
        required.add("get_alternate_suppliers")
    checks["gathered_required_evidence"] = required.issubset(called)
    checks["decision_as_expected"] = log.decision in expected_decisions
    checks["action_expectation_met"] = (log.action_taken != "none") == expect_action
    if expect_validation_pass:
        checks["validation_passed"] = bool(log.validation_passed)
    else:
        # We *expect* the situation to remain genuinely unresolved (e.g. no
        # compliant sourcing option exists), so validation SHOULD fail -- and
        # that failure must correctly trigger human approval rather than
        # being silently ignored.
        checks["validation_correctly_failed_and_escalated"] = (not log.validation_passed) and log.requires_human_approval
    policy_violation_in_action = False
    if log.action_taken != "none":
        action = json.loads(log.action_taken)
        policy_violation_in_action = bool(action.get("policy_violations"))
    checks["no_unjustified_policy_override"] = not policy_violation_in_action

    passed = all(checks.values())
    return {
        "case": name, "po_id": po_id, "sku": po.sku, "shortfall": shortfall,
        "decision": log.decision, "confidence": log.confidence,
        "requires_human_approval": log.requires_human_approval,
        "checks": checks, "passed": passed,
        "reasoning": log.reasoning,
    }


def _make_fixture(db, sku, warehouse_id, unit_cost, budget, on_hand, safety_stock, forecast_qty,
                   po_id, ordered_qty, confirmed_qty, supplier_id, moq, lead_time_days,
                   alt_supplier_id=None, alt_moq=None, alt_lead_time_days=None, alt_unit_cost=None):
    """Inserts a throwaway product/warehouse/supplier(s)/PO for one eval case."""
    db.add(Product(sku=sku, name=sku, category="eval", unit_cost=unit_cost, unit_volume=1.0))
    db.add(Warehouse(warehouse_id=warehouse_id, name=warehouse_id,
                      available_budget=budget, max_storage=1000, current_storage=100))
    db.add(Inventory(sku=sku, warehouse_id=warehouse_id, on_hand_qty=on_hand, safety_stock=safety_stock))
    db.add(DemandForecast(sku=sku, warehouse_id=warehouse_id, period_start=NOW, period_days=30,
                           forecast_qty=forecast_qty, actual_qty_to_date=0, days_elapsed=0))
    db.add(SupplierCatalog(supplier_id=supplier_id, supplier_name=supplier_id, sku=sku,
                            unit_cost=unit_cost, moq=moq, lead_time_days=lead_time_days,
                            reliability_score=0.9, is_primary=True))
    if alt_supplier_id:
        db.add(SupplierCatalog(supplier_id=alt_supplier_id, supplier_name=alt_supplier_id, sku=sku,
                                unit_cost=alt_unit_cost, moq=alt_moq, lead_time_days=alt_lead_time_days,
                                reliability_score=0.85, is_primary=False))
    db.add(PurchaseOrder(po_id=po_id, sku=sku, warehouse_id=warehouse_id, supplier_id=supplier_id,
                          ordered_qty=ordered_qty, confirmed_qty=confirmed_qty, status="open",
                          expected_delivery_date=NOW + timedelta(days=lead_time_days), created_by="system",
                          shortfall_status="pending"))
    db.commit()


def main():
    db_path = os.path.join(os.path.dirname(__file__), "..", "backend", "purchasing_agent.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    os.chdir(os.path.join(os.path.dirname(__file__), "..", "backend"))
    seed()
    db = SessionLocal()

    results = []

    # Case 1: seeded PO-8001 (SKU-008) -- 250-unit shortfall creates a genuine
    # 290-unit gap against demand, and a faster alternate supplier (SUP-12)
    # exists. Expect the agent to propose sourcing the gap from the alternate.
    results.append(run_case(db, "genuine shortfall, alternate supplier available", "PO-8001",
                             expected_decisions={"additional_po"}, expect_action=True))

    # Case 2: shortfall that doesn't actually matter -- on-hand inventory
    # alone already covers demand even with the reduced confirmed qty.
    # Expect accept_shortfall, no action taken.
    _make_fixture(
        db, sku="EVAL-SKU-A", warehouse_id="EVAL-WH-A", unit_cost=2.00, budget=2000.0,
        on_hand=500, safety_stock=50, forecast_qty=300,
        po_id="EVAL-PO-A", ordered_qty=200, confirmed_qty=100,
        supplier_id="EVAL-SUP-A1", moq=50, lead_time_days=10,
    )
    results.append(run_case(db, "shortfall doesn't matter, coverage already sufficient", "EVAL-PO-A",
                             expected_decisions={"accept_shortfall"}, expect_action=False,
                             require_alternate_supplier_check=False))

    # Case 3: genuine gap, but NO alternate supplier exists for this SKU at
    # all -- expect the agent to escalate rather than force a bad option.
    _make_fixture(
        db, sku="EVAL-SKU-B", warehouse_id="EVAL-WH-B", unit_cost=50.00, budget=100.0,
        on_hand=10, safety_stock=5, forecast_qty=200,
        po_id="EVAL-PO-B", ordered_qty=150, confirmed_qty=50,
        supplier_id="EVAL-SUP-B1", moq=20, lead_time_days=15,
    )
    # escalate_to_human is itself a write-tool call (a recorded action, just
    # not a purchase order), and the underlying shortfall is genuinely
    # unresolved -- so we expect an action (the escalation) but NOT
    # validation to pass.
    results.append(run_case(db, "genuine gap, no alternate supplier -> escalate", "EVAL-PO-B",
                             expected_decisions={"escalate"}, expect_action=True,
                             expect_validation_pass=False))

    db.close()

    print("\n=== Scenario 2 Evaluation Report ===\n")
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

    report_path = os.path.join(os.path.dirname(__file__), "scenario2_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Full report written to {report_path}")

    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
