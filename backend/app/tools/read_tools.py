"""
Read-only tools the agent uses to gather evidence before deciding anything.
Each returns plain dict/JSON-serializable data (what an LLM function-calling
tool result should look like), not ORM objects.
"""
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import (
    Product, Warehouse, Inventory, DemandForecast, SupplierCatalog,
    PurchaseOrder, PurchaseRecommendation,
)


def get_product(db: Session, sku: str) -> dict:
    p = db.get(Product, sku)
    if not p:
        return {"error": f"unknown sku {sku}"}
    return {"sku": p.sku, "name": p.name, "category": p.category, "unit_cost": p.unit_cost, "unit_volume": p.unit_volume}


def get_inventory_status(db: Session, sku: str, warehouse_id: str | None = None) -> dict:
    q = select(Inventory).where(Inventory.sku == sku)
    if warehouse_id:
        q = q.where(Inventory.warehouse_id == warehouse_id)
    rows = db.execute(q).scalars().all()
    return {
        "sku": sku,
        "by_warehouse": [{"warehouse_id": r.warehouse_id, "on_hand_qty": r.on_hand_qty, "safety_stock": r.safety_stock} for r in rows],
        "total_on_hand": sum(r.on_hand_qty for r in rows),
        "total_safety_stock": sum(r.safety_stock for r in rows),
    }


def get_demand_forecast(db: Session, sku: str, warehouse_id: str | None = None) -> dict:
    q = select(DemandForecast).where(DemandForecast.sku == sku)
    if warehouse_id:
        q = q.where(DemandForecast.warehouse_id == warehouse_id)
    rows = db.execute(q).scalars().all()
    out = []
    for r in rows:
        daily_run_rate = (r.actual_qty_to_date / r.days_elapsed) if r.days_elapsed else None
        projected_total = daily_run_rate * r.period_days if daily_run_rate else r.forecast_qty
        out.append({
            "warehouse_id": r.warehouse_id,
            "period_start": r.period_start.isoformat(),
            "period_days": r.period_days,
            "forecast_qty": r.forecast_qty,
            "actual_qty_to_date": r.actual_qty_to_date,
            "days_elapsed": r.days_elapsed,
            "implied_daily_run_rate": round(daily_run_rate, 2) if daily_run_rate else None,
            "projected_total_demand_if_trend_continues": round(projected_total, 1) if daily_run_rate else r.forecast_qty,
        })
    return {"sku": sku, "forecasts": out}


def get_open_pos(db: Session, sku: str, warehouse_id: str | None = None) -> dict:
    q = select(PurchaseOrder).where(PurchaseOrder.sku == sku, PurchaseOrder.status != "cancelled")
    if warehouse_id:
        q = q.where(PurchaseOrder.warehouse_id == warehouse_id)
    rows = db.execute(q).scalars().all()
    return {
        "sku": sku,
        "purchase_orders": [
            {
                "po_id": r.po_id, "warehouse_id": r.warehouse_id, "supplier_id": r.supplier_id,
                "ordered_qty": r.ordered_qty, "confirmed_qty": r.confirmed_qty,
                "status": r.status, "expected_delivery_date": r.expected_delivery_date.isoformat(),
                "created_by": r.created_by, "notes": r.notes,
            }
            for r in rows
        ],
        "total_incoming_confirmed": sum(r.confirmed_qty for r in rows if r.status != "fulfilled"),
    }


def get_po_status(db: Session, po_id: str) -> dict:
    r = db.get(PurchaseOrder, po_id)
    if not r:
        return {"error": f"unknown po_id {po_id}"}
    return {
        "po_id": r.po_id, "sku": r.sku, "warehouse_id": r.warehouse_id, "supplier_id": r.supplier_id,
        "ordered_qty": r.ordered_qty, "confirmed_qty": r.confirmed_qty, "status": r.status,
        "expected_delivery_date": r.expected_delivery_date.isoformat(),
        "created_by": r.created_by, "notes": r.notes,
    }


def get_alternate_suppliers(db: Session, sku: str, exclude_supplier_id: str | None = None) -> dict:
    """Suppliers for a SKU other than the one specified -- e.g. to find an
    alternate when a primary supplier falls short (Scenario 2)."""
    result = get_supplier_catalog(db, sku)
    alts = [s for s in result["suppliers"] if s["supplier_id"] != exclude_supplier_id]
    return {"sku": sku, "alternate_suppliers": alts}


def get_supplier_catalog(db: Session, sku: str, supplier_id: str | None = None) -> dict:
    q = select(SupplierCatalog).where(SupplierCatalog.sku == sku)
    if supplier_id:
        q = q.where(SupplierCatalog.supplier_id == supplier_id)
    rows = db.execute(q).scalars().all()
    return {
        "sku": sku,
        "suppliers": [
            {
                "supplier_id": r.supplier_id, "supplier_name": r.supplier_name,
                "unit_cost": r.unit_cost, "moq": r.moq, "lead_time_days": r.lead_time_days,
                "reliability_score": r.reliability_score, "is_primary": r.is_primary,
            }
            for r in rows
        ],
    }


def get_warehouse(db: Session, warehouse_id: str) -> dict:
    w = db.get(Warehouse, warehouse_id)
    if not w:
        return {"error": f"unknown warehouse_id {warehouse_id}"}
    return {
        "warehouse_id": w.warehouse_id, "name": w.name,
        "available_budget": w.available_budget,
        "max_storage": w.max_storage, "current_storage": w.current_storage,
        "remaining_storage": w.max_storage - w.current_storage,
    }


def get_pending_recommendation(db: Session, recommendation_id: int | None = None, sku: str | None = None) -> dict:
    """Pulls one pending purchase recommendation -- the Scenario 1 trigger queue."""
    q = select(PurchaseRecommendation).where(PurchaseRecommendation.status == "pending")
    if recommendation_id is not None:
        q = q.where(PurchaseRecommendation.id == recommendation_id)
    if sku:
        q = q.where(PurchaseRecommendation.sku == sku)
    q = q.order_by(PurchaseRecommendation.id.asc())
    r = db.execute(q).scalars().first()
    if not r:
        return {"error": "no pending recommendation found"}
    return {
        "id": r.id, "sku": r.sku, "warehouse_id": r.warehouse_id,
        "recommended_qty": r.recommended_qty, "reason": r.reason, "status": r.status,
    }


def get_pending_shortfall(db: Session, po_id: str | None = None) -> dict:
    """Pulls one pending supplier-shortfall PO -- the Scenario 2 trigger queue."""
    q = select(PurchaseOrder).where(PurchaseOrder.shortfall_status == "pending")
    if po_id is not None:
        q = q.where(PurchaseOrder.po_id == po_id)
    q = q.order_by(PurchaseOrder.po_id.asc())
    r = db.execute(q).scalars().first()
    if not r:
        return {"error": "no pending supplier shortfall found"}
    return {
        "po_id": r.po_id, "sku": r.sku, "warehouse_id": r.warehouse_id, "supplier_id": r.supplier_id,
        "ordered_qty": r.ordered_qty, "confirmed_qty": r.confirmed_qty, "shortfall_status": r.shortfall_status,
    }


def get_pending_demand_spike(db: Session, forecast_id: int | None = None) -> dict:
    """Pulls one pending demand-spike alert -- the Scenario 3 trigger queue."""
    q = select(DemandForecast).where(DemandForecast.review_status == "pending")
    if forecast_id is not None:
        q = q.where(DemandForecast.id == forecast_id)
    q = q.order_by(DemandForecast.id.asc())
    r = db.execute(q).scalars().first()
    if not r:
        return {"error": "no pending demand spike found"}
    return {
        "id": r.id, "sku": r.sku, "warehouse_id": r.warehouse_id,
        "forecast_qty": r.forecast_qty, "actual_qty_to_date": r.actual_qty_to_date,
        "days_elapsed": r.days_elapsed, "period_days": r.period_days, "review_status": r.review_status,
    }
