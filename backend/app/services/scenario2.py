"""
Scenario 2 (Supplier Cannot Fulfil the Purchase) processing logic, mirroring
services/scenario1.py's shape exactly:

  - app/api/routes.py: a human manually picks a pending supplier-shortfall
    PO and submits it for review (POST /api/scenario2/supplier-shortfall).
  - (Not yet wired into app/scheduler.py's cron -- that job currently only
    covers Scenario 1's recommendation queue. Extending it to also sweep
    Scenario 2 shortfalls would be a small follow-up, not a redesign.)

The trigger is a PurchaseOrder whose supplier confirmed less than was
ordered (confirmed_qty < ordered_qty) -- `shortfall_status` flips to
"pending" the moment that gap exists (see seed.py for how the mock data
represents this "supplier just told us" event). Same Proposal Pattern
applies: propose_purchase_order never writes to purchase_orders itself, so
a "additional_po" decision here still waits for human approval before
api/orchestrator.py's apply_approval_decision() actually executes it.

`_claim` is the same atomic UPDATE ... WHERE shortfall_status='pending'
guard as Scenario 1's recommendation claim -- necessary for the same reason
(a real LLM review can take a minute+, so cron and a manual click could
otherwise race for the same PO).
"""
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.models import DecisionLog, PurchaseOrder
from app.tools import read_tools
from app.api.orchestrator import run_scenario

ALLOWED_DECISIONS = ["accept_shortfall", "additional_po", "escalate"]


def _claim(db: Session, po_id: str) -> bool:
    result = db.execute(
        update(PurchaseOrder)
        .where(PurchaseOrder.po_id == po_id, PurchaseOrder.shortfall_status == "pending")
        .values(shortfall_status="in_review")
    )
    db.commit()
    return result.rowcount > 0


def process_shortfall(db: Session, po: PurchaseOrder, triggered_by: str = "manual") -> DecisionLog | None:
    if not _claim(db, po.po_id):
        return None  # already claimed by a concurrent cron run or another request

    try:
        product = read_tools.get_product(db, po.sku)
        if "error" in product:
            po.shortfall_status = "pending"  # release the claim -- nothing to review
            db.commit()
            return None

        shortfall = po.ordered_qty - po.confirmed_qty
        situation_prompt = (
            f"SITUATION (Scenario 2 -- Supplier Cannot Fulfil the Purchase): "
            f"Purchase order {po.po_id} for SKU {po.sku} ('{product['name']}') at warehouse {po.warehouse_id} "
            f"was placed with supplier {po.supplier_id} for {po.ordered_qty} units, but the supplier has now "
            f"confirmed only {po.confirmed_qty} units can be supplied -- a shortfall of {shortfall} units. "
            f"Determine what should happen next: is existing inventory (plus other confirmed incoming supply) "
            f"sufficient despite the shortfall, should the remaining {shortfall} units be sourced from another "
            f"supplier via a new purchase order, or should this be escalated to a human buyer? Consider current "
            f"inventory, demand forecast, other open purchase orders, and alternate suppliers' lead time, MOQ, "
            f"and cost before deciding."
        )
        trigger_event = {
            "type": "supplier_shortfall", "sku": po.sku, "warehouse_id": po.warehouse_id, "po_id": po.po_id,
            "supplier_id": po.supplier_id, "ordered_qty": po.ordered_qty, "confirmed_qty": po.confirmed_qty,
        }

        log = run_scenario(
            db, scenario="supplier_shortfall",
            situation_prompt=situation_prompt,
            allowed_decisions=ALLOWED_DECISIONS,
            trigger_event=trigger_event,
            warehouse_id=po.warehouse_id,
            validator_kwargs={"sku": po.sku, "warehouse_id": po.warehouse_id, "action_result": None},
            triggered_by=triggered_by,
        )

        po.shortfall_status = "reviewed"
        db.commit()
        return log
    except Exception:
        # Release the claim so this shortfall can be retried on the next
        # cron cycle or a manual click, instead of getting stuck in
        # "in_review" forever.
        po.shortfall_status = "pending"
        db.commit()
        raise


def process_all_pending(db: Session) -> list[DecisionLog]:
    """Reviews every pending supplier shortfall in the queue, oldest first.
    One failure doesn't block the rest."""
    pending = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.shortfall_status == "pending").order_by(PurchaseOrder.po_id.asc())
    ).scalars().all()
    logs = []
    for po in pending:
        try:
            log = process_shortfall(db, po, triggered_by="cron")
        except Exception:
            continue
        if log:
            logs.append(log)
    return logs
