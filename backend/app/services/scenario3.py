"""
Scenario 3 (Demand/Forecast Has Changed) processing logic, mirroring
services/scenario1.py and scenario2.py's shape exactly:

  - app/api/routes.py: a human manually picks a pending demand-spike alert
    and submits it for review (POST /api/scenario3/demand-spike).
  - app/scheduler.py: the cron sweep also covers this queue every cycle.

The trigger is a DemandForecast row where actual sales imply a genuine
spike against an existing open PO that may no longer cover it --
`review_status` flips to "pending" by app/services/scanner.py's
scan_for_demand_spikes, mirroring purchase_recommendations/shortfall_status
for the other two scenarios. Same Proposal Pattern applies: a decision to
increase the order still waits for human approval before
api/orchestrator.py's apply_approval_decision() executes it.
"""
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.models import DecisionLog, DemandForecast
from app.tools import read_tools
from app.api.orchestrator import run_scenario

ALLOWED_DECISIONS = ["plan_sufficient", "increase_order", "escalate"]


def _claim(db: Session, forecast_id: int) -> bool:
    result = db.execute(
        update(DemandForecast)
        .where(DemandForecast.id == forecast_id, DemandForecast.review_status == "pending")
        .values(review_status="in_review")
    )
    db.commit()
    return result.rowcount > 0


def process_demand_spike(db: Session, forecast: DemandForecast, triggered_by: str = "manual") -> DecisionLog | None:
    if not _claim(db, forecast.id):
        return None  # already claimed by a concurrent cron run or another request

    try:
        product = read_tools.get_product(db, forecast.sku)
        if "error" in product:
            forecast.review_status = "pending"  # release the claim -- nothing to review
            db.commit()
            return None

        daily_rate = forecast.actual_qty_to_date / forecast.days_elapsed if forecast.days_elapsed else 0
        situation_prompt = (
            f"SITUATION (Scenario 3 -- Demand/Forecast Has Changed): "
            f"SKU {forecast.sku} ('{product['name']}') at warehouse {forecast.warehouse_id} was forecast at "
            f"{forecast.forecast_qty} units over {forecast.period_days} days, but {forecast.days_elapsed} days in, "
            f"actual sales are already {forecast.actual_qty_to_date} units -- an implied run-rate of "
            f"{daily_rate:.1f}/day, well ahead of the {forecast.forecast_qty / forecast.period_days:.1f}/day originally "
            f"planned. There is an existing open purchase order, but current inventory and incoming quantity may no "
            f"longer be sufficient to cover expected demand at this new pace. Investigate whether the purchasing plan "
            f"needs to change: is the existing order (plus on-hand inventory) still sufficient, does it need to be "
            f"supplemented with an additional order, or should this be escalated to a human buyer?"
        )
        trigger_event = {
            "type": "demand_spike", "sku": forecast.sku, "warehouse_id": forecast.warehouse_id,
            "forecast_id": forecast.id, "forecast_qty": forecast.forecast_qty,
            "actual_qty_to_date": forecast.actual_qty_to_date, "days_elapsed": forecast.days_elapsed,
        }

        log = run_scenario(
            db, scenario="demand_spike",
            situation_prompt=situation_prompt,
            allowed_decisions=ALLOWED_DECISIONS,
            trigger_event=trigger_event,
            warehouse_id=forecast.warehouse_id,
            validator_kwargs={"sku": forecast.sku, "warehouse_id": forecast.warehouse_id, "action_result": None},
            triggered_by=triggered_by,
        )

        forecast.review_status = "reviewed"
        db.commit()
        return log
    except Exception:
        # Release the claim so this spike can be retried on the next cron
        # cycle or a manual click, instead of getting stuck in "in_review".
        forecast.review_status = "pending"
        db.commit()
        raise


def process_all_pending(db: Session) -> list[DecisionLog]:
    """Reviews every pending demand-spike alert in the queue, oldest first.
    One failure doesn't block the rest."""
    pending = db.execute(
        select(DemandForecast).where(DemandForecast.review_status == "pending").order_by(DemandForecast.id.asc())
    ).scalars().all()
    logs = []
    for forecast in pending:
        try:
            log = process_demand_spike(db, forecast, triggered_by="cron")
        except Exception:
            continue
        if log:
            logs.append(log)
    return logs
