"""
Evaluation harness for Scenario 1 (Purchase Recommendation Review).

This is intentionally a small, readable script rather than a framework, per
the assignment's guidance ("you do not need to build a sophisticated
evaluation framework"). It runs a handful of scripted cases against the live
agent pipeline (same orchestrator the API uses) and checks, per case:

  - did the agent gather the necessary evidence (inventory, demand, open POs)?
  - was the decision the expected one (or an acceptable one, for cases with
    more than one defensible answer)?
  - did it respect hard constraints (no policy violations in any proposal it
    put forward)?
  - did post-action validation pass?
  - for the "no action needed" cases, did it correctly avoid proposing one?

Each case works by inserting (or reusing) a `purchase_recommendations` row
with the qty/state it wants to test -- mirroring how a real recommendation
queue would look -- then running the same `run_scenario` orchestrator the
API uses, addressed by recommendation_id.

Run with:  USE_STUB_LLM=true python eval/scenario1_eval.py     (offline, deterministic)
       or: python eval/scenario1_eval.py                        (uses real OpenAI agent)
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import SessionLocal
from app.seed import seed
from app.tools import read_tools
from app.api.orchestrator import run_scenario
from app.models import Inventory, PurchaseRecommendation
from sqlalchemy import select


REQUIRED_EVIDENCE_TOOLS = {"get_inventory_status", "get_demand_forecast", "get_open_pos"}


def tools_called(log) -> set:
    return {c["tool"] for c in json.loads(log.tool_calls)}


def run_case(db, name, sku, warehouse_id, recommended_qty, expected_decisions, expect_action,
             expect_validation_pass=True):
    product = read_tools.get_product(db, sku)
    recommendation = PurchaseRecommendation(
        sku=sku, warehouse_id=warehouse_id, recommended_qty=recommended_qty,
        reason="eval case", status="pending",
    )
    db.add(recommendation)
    db.commit()
    db.refresh(recommendation)

    situation_prompt = (
        f"SITUATION (Scenario 1 -- Purchase Recommendation Review): The purchasing system recommends "
        f"buying {recommended_qty} units of SKU {sku} ('{product['name']}') at warehouse {warehouse_id}. "
        f"Determine whether this should be accepted, modified, rejected, or investigated further."
    )
    trigger_event = {"type": "recommendation_review", "sku": sku, "warehouse_id": warehouse_id,
                      "recommended_qty": recommended_qty}
    log = run_scenario(
        db, scenario="recommendation_review", situation_prompt=situation_prompt,
        allowed_decisions=["accept", "modify", "reject", "investigate"], trigger_event=trigger_event,
        warehouse_id=warehouse_id,
        validator_kwargs={"sku": sku, "warehouse_id": warehouse_id, "action_result": None},
        recommendation_id=recommendation.id,
    )

    checks = {}
    called = tools_called(log)
    checks["gathered_required_evidence"] = REQUIRED_EVIDENCE_TOOLS.issubset(called)
    checks["decision_as_expected"] = log.decision in expected_decisions
    checks["action_expectation_met"] = (log.action_taken != "none") == expect_action
    if expect_validation_pass:
        checks["validation_passed"] = bool(log.validation_passed)
    else:
        # We *expect* the underlying situation to remain unresolved (e.g. the
        # agent correctly declined to act because of a hard constraint), so
        # validation SHOULD fail -- and that failure must correctly trigger a
        # human-approval requirement rather than being silently ignored.
        checks["validation_correctly_failed_and_escalated"] = (not log.validation_passed) and log.requires_human_approval
    policy_violation_in_action = False
    if log.action_taken != "none":
        action = json.loads(log.action_taken)
        policy_violation_in_action = bool(action.get("policy_violations"))
    checks["no_unjustified_policy_override"] = not policy_violation_in_action

    passed = all(checks.values())
    return {
        "case": name, "sku": sku, "recommended_qty": recommended_qty,
        "decision": log.decision, "confidence": log.confidence,
        "requires_human_approval": log.requires_human_approval,
        "checks": checks, "passed": passed,
        "reasoning": log.reasoning,
    }


def main():
    # Fresh DB for a clean run.
    db_path = os.path.join(os.path.dirname(__file__), "..", "backend", "purchasing_agent.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    os.chdir(os.path.join(os.path.dirname(__file__), "..", "backend"))
    seed()
    db = SessionLocal()

    # Seed already inserts one pending recommendation per SKU-001/SKU-004; mark
    # them reviewed so each eval case can insert its own without ambiguity.
    for rec in db.execute(select(PurchaseRecommendation)).scalars().all():
        rec.status = "reviewed"
    db.commit()

    results = []

    # Case 1: SKU-001 -- existing coverage (650 + 400 = 1050) already exceeds
    # 900 demand, so 800 more is unjustified. Expect reject or modify (a
    # smaller top-up would also be defensible), and NOT a straight accept.
    results.append(run_case(db, "over-recommended given existing coverage", "SKU-001", "WH-001", 800,
                             expected_decisions={"reject", "modify"}, expect_action=False))

    # Case 2: same SKU, but recommend a quantity that roughly matches the true
    # gap (900 - 1050 = negative, so gap is 0 -- recommend 0 additional units).
    # Expect accept (no order needed, recommendation matches reality).
    results.append(run_case(db, "recommendation already correct (no order needed)", "SKU-001", "WH-001", 0,
                             expected_decisions={"accept"}, expect_action=False))

    # Case 3: drain SKU-001's inventory to create a genuine, sizeable gap, and
    # recommend a qty that matches it -- expect accept/modify, and a proposal
    # with no policy violation.
    inv = db.execute(select(Inventory).where(Inventory.sku == "SKU-001")).scalar_one()
    inv.on_hand_qty = 50
    db.commit()
    results.append(run_case(db, "genuine shortfall, recommendation roughly correct", "SKU-001", "WH-001", 400,
                             expected_decisions={"accept", "modify"}, expect_action=True))

    # Case 4: SKU-004 (staples) -- warehouse budget and storage are both
    # nearly exhausted in the seed data. A large recommended purchase should
    # NOT be blindly proposed; expect investigate (or a modify to a much
    # smaller/compliant qty), and in either case no policy violation in any
    # proposal made.
    results.append(run_case(db, "constrained by budget/storage", "SKU-004", "WH-004", 600,
                             expected_decisions={"investigate", "modify", "reject"}, expect_action=False,
                             expect_validation_pass=False))

    db.close()

    print("\n=== Scenario 1 Evaluation Report ===\n")
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

    report_path = os.path.join(os.path.dirname(__file__), "scenario1_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Full report written to {report_path}")

    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
