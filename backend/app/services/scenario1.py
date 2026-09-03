"""
Scenario 1 (Purchase Recommendation Review) processing logic, shared by two
callers:

  - app/api/routes.py: a human manually picks a pending recommendation and
    submits it for review (POST /api/scenario1/recommendation-review).
  - app/scheduler.py: a fixed cron-style job (see settings.
    scenario1_cron_interval_seconds) automatically picks up every pending
    recommendation and runs it through the same pipeline, unattended --
    mirroring how a real purchasing system wouldn't wait for someone to
    click a button before reviewing a new recommendation.

Either way the result is identical: a DecisionLog row, paused for human
approval per the same rules (api/orchestrator.py). The cron job only
automates *review*, not execution -- nothing is ever written to
purchase_orders without a human approving the proposal (see
tools/write_tools.py's Proposal Pattern).

Because a real LLM review can take a minute or more, the cron job and a
manual click can otherwise race for the same recommendation (the cron picks
it up, and before its "reviewed" commit lands, a manual request reads it as
still pending and reviews it a second time). `_claim` guards against this
with an atomic UPDATE ... WHERE status='pending' -- only one caller can ever
flip a given recommendation out of "pending", so a loser sees rowcount 0 and
backs off instead of duplicating the work.
"""
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.models import DecisionLog, PurchaseRecommendation
from app.tools import read_tools
from app.api.orchestrator import run_scenario


def _claim(db: Session, recommendation_id: int) -> bool:
    result = db.execute(
        update(PurchaseRecommendation)
        .where(PurchaseRecommendation.id == recommendation_id, PurchaseRecommendation.status == "pending")
        .values(status="in_review")
    )
    db.commit()
    return result.rowcount > 0


def process_recommendation(db: Session, recommendation: PurchaseRecommendation, triggered_by: str = "manual") -> DecisionLog | None:
    if not _claim(db, recommendation.id):
        return None  # already claimed by a concurrent cron run or another request

    try:
        product = read_tools.get_product(db, recommendation.sku)
        if "error" in product:
            recommendation.status = "pending"  # release the claim -- nothing to review
            db.commit()
            return None

        situation_prompt = (
            f"SITUATION (Scenario 1 -- Purchase Recommendation Review): "
            f"The purchasing system recommends buying {recommendation.recommended_qty} units of SKU {recommendation.sku} "
            f"('{product['name']}') at warehouse {recommendation.warehouse_id}. Reason given: {recommendation.reason} "
            f"Determine whether this recommendation should be accepted, modified, rejected, or investigated further, "
            f"using current inventory, demand forecast, open purchase orders, supplier lead time and MOQ, "
            f"and the warehouse's available budget and storage capacity."
        )
        trigger_event = {
            "type": "recommendation_review", "sku": recommendation.sku, "warehouse_id": recommendation.warehouse_id,
            "recommended_qty": recommendation.recommended_qty,
        }

        log = run_scenario(
            db, scenario="recommendation_review",
            situation_prompt=situation_prompt,
            allowed_decisions=["accept", "modify", "reject", "investigate"],
            trigger_event=trigger_event,
            warehouse_id=recommendation.warehouse_id,
            validator_kwargs={"sku": recommendation.sku, "warehouse_id": recommendation.warehouse_id, "action_result": None},
            recommendation_id=recommendation.id,
            triggered_by=triggered_by,
        )

        recommendation.status = "reviewed"
        db.commit()
        return log
    except Exception:
        # Release the claim so this recommendation can be retried on the next
        # cron cycle or a manual click, instead of getting stuck in
        # "in_review" forever.
        recommendation.status = "pending"
        db.commit()
        raise


def process_all_pending(db: Session) -> list[DecisionLog]:
    """Used by the scheduled cron job: reviews every pending recommendation
    in the queue, oldest first (any number of distinct products/warehouses --
    it doesn't stop after the first). One failure doesn't block the rest."""
    pending = db.execute(
        select(PurchaseRecommendation).where(PurchaseRecommendation.status == "pending").order_by(PurchaseRecommendation.id.asc())
    ).scalars().all()
    logs = []
    for rec in pending:
        try:
            log = process_recommendation(db, rec, triggered_by="cron")
        except Exception:
            continue
        if log:
            logs.append(log)
    return logs
