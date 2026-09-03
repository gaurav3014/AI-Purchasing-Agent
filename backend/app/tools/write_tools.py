"""
Write/action tools, built around the Proposal Pattern: the agent can never
write directly to purchase_orders. `propose_purchase_order` re-reads current
state, runs deterministic policy checks (MOQ / budget / storage), and either
refuses with a structured PolicyViolation the agent must react to, or
returns a validated *proposal* -- a plain dict describing what it would do.
Nothing is committed to the database at this point.

`execute_purchase_order` is the only function that actually writes a
purchase order. It is called exactly once, by the human-approval endpoint
(api/orchestrator.py's apply_approval_decision), on an already-validated
proposal -- never by the agent directly. This keeps "decide what to try"
(the LLM) and "commit it for real" (a human, via one ACID transaction)
strictly separate.
"""
import uuid
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models import PurchaseOrder, Warehouse, SupplierCatalog, Product
from app.tools import policy

NOW = datetime(2026, 9, 2)


def _get_supplier_catalog_row(db: Session, sku: str, supplier_id: str):
    from sqlalchemy import select
    return db.execute(
        select(SupplierCatalog).where(SupplierCatalog.sku == sku, SupplierCatalog.supplier_id == supplier_id)
    ).scalar_one_or_none()


def propose_purchase_order(db: Session, sku: str, warehouse_id: str, supplier_id: str, qty: int) -> dict:
    product = db.get(Product, sku)
    warehouse = db.get(Warehouse, warehouse_id)
    terms = _get_supplier_catalog_row(db, sku, supplier_id)
    if not product or not warehouse or not terms:
        return {"success": False, "error": f"unknown sku/warehouse/supplier combination {sku}/{warehouse_id}/{supplier_id}"}

    cost = qty * terms.unit_cost
    storage_needed = int(round(qty * product.unit_volume))

    violations = []
    v = policy.check_moq(qty, terms.moq)
    if v:
        violations.append(v.__dict__)
    v = policy.check_budget(cost, warehouse.available_budget, 0.0)
    if v:
        violations.append(v.__dict__)
    v = policy.check_storage(storage_needed, warehouse.max_storage, warehouse.current_storage)
    if v:
        violations.append(v.__dict__)

    if violations:
        return {
            "success": False,
            "policy_violations": violations,
            "message": "Proposal REFUSED because it violates one or more hard constraints. "
                       "Propose a smaller/different quantity or supplier, or escalate_to_human.",
        }

    return {
        "success": True,
        "sku": sku, "product_name": product.name, "warehouse_id": warehouse_id,
        "supplier_id": supplier_id, "qty": qty,
        "cost": cost, "storage_needed": storage_needed,
        "expected_delivery_date": (NOW + timedelta(days=terms.lead_time_days)).isoformat(),
        "message": "Proposal passed all guardrail checks and is pending human approval before execution.",
    }


def execute_purchase_order(db: Session, proposal: dict) -> dict:
    """Executes an already-validated proposal as one transaction: creates the
    PO, deducts the warehouse's budget, and reserves the storage it needs.
    Called only after a human approves the proposal."""
    terms = _get_supplier_catalog_row(db, proposal["sku"], proposal["supplier_id"])
    warehouse = db.get(Warehouse, proposal["warehouse_id"])
    if not terms or not warehouse:
        return {"success": False, "error": "proposal references a sku/warehouse/supplier that no longer exists"}

    po_id = f"PO-{uuid.uuid4().hex[:8].upper()}"
    po = PurchaseOrder(
        po_id=po_id, sku=proposal["sku"], warehouse_id=proposal["warehouse_id"],
        supplier_id=proposal["supplier_id"], ordered_qty=proposal["qty"], confirmed_qty=proposal["qty"],
        status="open", expected_delivery_date=NOW + timedelta(days=terms.lead_time_days),
        created_by="agent", notes="Created from a human-approved agent proposal.",
    )
    db.add(po)
    warehouse.available_budget -= proposal["cost"]
    warehouse.current_storage += proposal.get("storage_needed", proposal["qty"])
    db.commit()

    return {"success": True, "po_id": po_id, **proposal}


def escalate_to_human(db: Session, reason: str, context: dict) -> dict:
    """
    Not a data mutation -- just a structured signal that the agent could not
    (or should not) resolve this autonomously. The API layer surfaces this
    as a pending human task rather than executing anything.
    """
    return {"success": True, "escalated": True, "reason": reason, "context": context}
