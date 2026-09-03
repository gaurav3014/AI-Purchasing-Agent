import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.db import get_db
from app.models import DecisionLog, PurchaseRecommendation, PurchaseOrder, DemandForecast, Product
from app.tools import read_tools
from app.schemas import (
    RecommendationReviewRequest, ShortfallReviewRequest, DemandSpikeReviewRequest,
    ApprovalDecisionRequest, DecisionResponse,
)
from app.api.orchestrator import apply_approval_decision
from app.services.scenario1 import process_recommendation
from app.services.scenario2 import process_shortfall
from app.services.scenario3 import process_demand_spike

router = APIRouter(prefix="/api")


def _to_response(log: DecisionLog, reasons: list[str] | None = None) -> DecisionResponse:
    return DecisionResponse(
        log_id=log.id,
        run_id=log.run_id,
        scenario=log.scenario,
        sku=log.sku,
        recommendation_id=log.recommendation_id,
        triggered_by=log.triggered_by,
        decision=log.decision,
        reasoning=log.reasoning,
        factors_considered=json.loads(log.factors_considered) if log.factors_considered else [],
        confidence=log.confidence,
        action_summary=(json.loads(log.action_taken).get("po_id", "") if log.action_taken != "none" else "none") or log.decision,
        action_result=json.loads(log.action_taken) if log.action_taken != "none" else None,
        executed=log.executed,
        tool_call_trace=json.loads(log.tool_calls) if log.tool_calls else [],
        requires_human_approval=log.requires_human_approval,
        approval_reasons=reasons if reasons is not None else getattr(log, "_approval_reasons", []),
        approval_status=log.approval_status,
        approval_note=log.approval_note or "",
        approved_at=log.approved_at.isoformat() if log.approved_at else None,
        validation_result=json.loads(log.validation_result) if log.validation_result else None,
        validation_passed=log.validation_passed,
        retry_count=log.retry_count,
    )


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/reference-data")
def reference_data(db: Session = Depends(get_db)):
    """Convenience endpoint for the frontend to populate SKU pickers etc."""
    products = db.query(Product).all()
    return [{"sku": p.sku, "name": p.name, "category": p.category} for p in products]


@router.get("/scenario1/schedule")
def scenario1_schedule(db: Session = Depends(get_db)):
    """When the next cron-style automatic review run will fire (see app/scheduler.py),
    plus how many products exist in total -- so the UI can show "flagged X out of Y",
    making clear the scan is selective, not flagging everything."""
    from app.scheduler import get_schedule_status
    status = get_schedule_status()
    status["total_products"] = db.query(Product).count()
    return status


@router.get("/scenario1/recommendations")
def pending_recommendations(db: Session = Depends(get_db)):
    """Pending purchase_recommendations rows -- the Scenario 1 trigger queue."""
    rows = db.execute(
        select(PurchaseRecommendation).where(PurchaseRecommendation.status == "pending").order_by(PurchaseRecommendation.id.asc())
    ).scalars().all()
    out = []
    for r in rows:
        product = db.get(Product, r.sku)
        out.append({
            "id": r.id, "sku": r.sku, "product_name": product.name if product else r.sku,
            "warehouse_id": r.warehouse_id, "recommended_qty": r.recommended_qty, "reason": r.reason,
        })
    return out


@router.post("/scenario1/recommendation-review", response_model=DecisionResponse)
def scenario1_recommendation_review(req: RecommendationReviewRequest, db: Session = Depends(get_db)):
    """Manual trigger -- review one specific pending recommendation right now.
    Scenario 1 also runs unattended on a fixed schedule; see app/scheduler.py."""
    rec_data = read_tools.get_pending_recommendation(db, recommendation_id=req.recommendation_id)
    if "error" in rec_data:
        raise HTTPException(status_code=404, detail=rec_data["error"])
    recommendation = db.get(PurchaseRecommendation, rec_data["id"])

    log = process_recommendation(db, recommendation)
    if log is None:
        raise HTTPException(
            status_code=409,
            detail="This recommendation is already being reviewed (likely the scheduled cron job got to it "
                   "first) or is no longer pending. Refresh the recommendation list and try again.",
        )

    return _to_response(log)


@router.get("/scenario2/shortfalls")
def pending_shortfalls(db: Session = Depends(get_db)):
    """Pending supplier-shortfall purchase orders -- the Scenario 2 trigger queue."""
    rows = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.shortfall_status == "pending").order_by(PurchaseOrder.po_id.asc())
    ).scalars().all()
    out = []
    for r in rows:
        product = db.get(Product, r.sku)
        out.append({
            "po_id": r.po_id, "sku": r.sku, "product_name": product.name if product else r.sku,
            "warehouse_id": r.warehouse_id, "supplier_id": r.supplier_id,
            "ordered_qty": r.ordered_qty, "confirmed_qty": r.confirmed_qty,
            "shortfall": r.ordered_qty - r.confirmed_qty,
        })
    return out


@router.post("/scenario2/supplier-shortfall", response_model=DecisionResponse)
def scenario2_supplier_shortfall(req: ShortfallReviewRequest, db: Session = Depends(get_db)):
    """Manual trigger -- review one specific pending supplier shortfall right now."""
    shortfall_data = read_tools.get_pending_shortfall(db, po_id=req.po_id)
    if "error" in shortfall_data:
        raise HTTPException(status_code=404, detail=shortfall_data["error"])
    po = db.get(PurchaseOrder, shortfall_data["po_id"])

    log = process_shortfall(db, po)
    if log is None:
        raise HTTPException(
            status_code=409,
            detail="This supplier shortfall is already being reviewed or is no longer pending. "
                   "Refresh the shortfall list and try again.",
        )

    return _to_response(log)


@router.get("/scenario3/demand-spikes")
def pending_demand_spikes(db: Session = Depends(get_db)):
    """Pending demand-spike forecasts -- the Scenario 3 trigger queue."""
    rows = db.execute(
        select(DemandForecast).where(DemandForecast.review_status == "pending").order_by(DemandForecast.id.asc())
    ).scalars().all()
    out = []
    for r in rows:
        product = db.get(Product, r.sku)
        daily_rate = r.actual_qty_to_date / r.days_elapsed if r.days_elapsed else 0
        out.append({
            "forecast_id": r.id, "sku": r.sku, "product_name": product.name if product else r.sku,
            "warehouse_id": r.warehouse_id, "forecast_qty": r.forecast_qty,
            "actual_qty_to_date": r.actual_qty_to_date, "days_elapsed": r.days_elapsed,
            "implied_daily_rate": round(daily_rate, 1),
        })
    return out


@router.post("/scenario3/demand-spike", response_model=DecisionResponse)
def scenario3_demand_spike(req: DemandSpikeReviewRequest, db: Session = Depends(get_db)):
    """Manual trigger -- review one specific pending demand spike right now."""
    spike_data = read_tools.get_pending_demand_spike(db, forecast_id=req.forecast_id)
    if "error" in spike_data:
        raise HTTPException(status_code=404, detail=spike_data["error"])
    forecast = db.get(DemandForecast, spike_data["id"])

    log = process_demand_spike(db, forecast)
    if log is None:
        raise HTTPException(
            status_code=409,
            detail="This demand spike is already being reviewed or is no longer pending. "
                   "Refresh the list and try again.",
        )

    return _to_response(log)


@router.get("/decisions", response_model=list[DecisionResponse])
def list_decisions(db: Session = Depends(get_db)):
    logs = db.query(DecisionLog).order_by(DecisionLog.id.desc()).all()
    return [_to_response(l) for l in logs]


@router.get("/decisions/{log_id}", response_model=DecisionResponse)
def get_decision(log_id: int, db: Session = Depends(get_db)):
    log = db.get(DecisionLog, log_id)
    if not log:
        raise HTTPException(status_code=404, detail="not found")
    return _to_response(log)


@router.post("/approvals/decide", response_model=DecisionResponse)
def decide_approval(req: ApprovalDecisionRequest, db: Session = Depends(get_db)):
    log = db.get(DecisionLog, req.log_id)
    if not log:
        raise HTTPException(status_code=404, detail="not found")
    log = apply_approval_decision(db, log, req.approve, req.note)
    return _to_response(log)
