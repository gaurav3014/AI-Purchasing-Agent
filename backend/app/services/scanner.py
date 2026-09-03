"""
Runs an AI triage over every (sku, warehouse) pair that has never been
flagged before, and routes whatever it finds into the right trigger queue.
See agent/triage.py for why "does this product have a problem?" is now an
LLM judgment call per product instead of fixed-threshold math -- fixed rules
can only ever catch the exact patterns someone thought to encode; asking an
AI to actually look at each product's real data can catch things a rule
never would.

Still "one shot per situation": once a (sku, warehouse) pair has ever had a
recommendation, shortfall, or spike-review row created for it, it's never
triaged again -- otherwise an unresolved (never approved) situation would
get re-flagged and re-triaged every single cron cycle forever. A human
approving/rejecting the resulting decision is what's expected to actually
resolve the underlying numbers; this module's job is only detection, once
per situation.

Triage classifies; it never decides what to DO about a problem. Whatever it
flags is routed into the exact trigger queues
(purchase_recommendations / purchase_orders.shortfall_status /
demand_forecast.review_status) that the full agent engine
(services/scenario{1,2,3}.py) already knows how to process end-to-end.

Called by app/scheduler.py before each cron sweep, and available to call
manually (e.g. from a script or a future "scan now" endpoint).
"""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Inventory, PurchaseOrder, PurchaseRecommendation, DemandForecast
from app.agent.triage import triage_product


def _already_triaged(db: Session, sku: str, warehouse_id: str) -> bool:
    """True if this (sku, warehouse) pair has ever been flagged for any of
    the three scenarios -- triage only ever gets one shot per pair."""
    if db.execute(
        select(PurchaseRecommendation).where(
            PurchaseRecommendation.sku == sku, PurchaseRecommendation.warehouse_id == warehouse_id,
        )
    ).scalars().first():
        return True
    if db.execute(
        select(DemandForecast).where(
            DemandForecast.sku == sku, DemandForecast.warehouse_id == warehouse_id,
            DemandForecast.review_status != "none",
        )
    ).scalars().first():
        return True
    if db.execute(
        select(PurchaseOrder).where(
            PurchaseOrder.sku == sku, PurchaseOrder.warehouse_id == warehouse_id,
            PurchaseOrder.shortfall_status != "none",
        )
    ).scalars().first():
        return True
    return False


def scan_all(db: Session) -> dict:
    """Triages every never-before-seen (sku, warehouse) pair. Returns what
    was newly flagged, for logging/status reporting."""
    new_recommendations = []
    new_shortfalls = []
    new_demand_spikes = []

    pairs = db.execute(select(Inventory.sku, Inventory.warehouse_id)).all()
    for sku, warehouse_id in pairs:
        if _already_triaged(db, sku, warehouse_id):
            continue

        result = triage_product(db, sku, warehouse_id)
        if not result["has_problem"]:
            continue

        problem_type = result["problem_type"]

        if problem_type == "supplier_shortfall":
            po = db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.sku == sku, PurchaseOrder.warehouse_id == warehouse_id,
                    PurchaseOrder.status == "open", PurchaseOrder.confirmed_qty < PurchaseOrder.ordered_qty,
                    PurchaseOrder.shortfall_status == "none",
                )
            ).scalars().first()
            if po:
                po.shortfall_status = "pending"
                db.commit()
                new_shortfalls.append({"po_id": po.po_id, "sku": sku, "warehouse_id": warehouse_id})
                continue
            # AI classified it as a shortfall but no matching PO actually
            # exists -- don't trust the classification blindly, fall back to
            # treating it as a plain recommendation instead.
            problem_type = "recommendation_needed"

        if problem_type == "demand_spike":
            existing_po = db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.sku == sku, PurchaseOrder.warehouse_id == warehouse_id,
                    PurchaseOrder.status == "open",
                )
            ).scalars().first()
            forecast = db.execute(
                select(DemandForecast).where(
                    DemandForecast.sku == sku, DemandForecast.warehouse_id == warehouse_id,
                )
            ).scalars().first()
            if existing_po and forecast:
                forecast.review_status = "pending"
                db.commit()
                new_demand_spikes.append({"forecast_id": forecast.id, "sku": sku, "warehouse_id": warehouse_id})
                continue
            # No existing plan to revise -- same fallback reasoning as above.
            problem_type = "recommendation_needed"

        if problem_type == "recommendation_needed":
            rec = PurchaseRecommendation(
                sku=sku, warehouse_id=warehouse_id,
                recommended_qty=result.get("suggested_qty", 0),
                reason=f"AI triage: {result['reasoning']}",
                status="pending",
            )
            db.add(rec)
            db.commit()
            new_recommendations.append({"sku": sku, "warehouse_id": warehouse_id, "recommended_qty": rec.recommended_qty})

    return {
        "new_recommendations": new_recommendations,
        "new_shortfalls": new_shortfalls,
        "new_demand_spikes": new_demand_spikes,
    }
