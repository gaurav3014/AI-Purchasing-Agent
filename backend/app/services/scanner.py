"""
Proactively scans every product's real data (inventory, demand forecast,
open purchase orders) and decides FOR ITSELF whether a Scenario 1, 2, or 3
situation currently exists -- rather than only ever processing rows someone
already flagged as pending. This is what turns `purchase_recommendations`,
`purchase_orders.shortfall_status`, and `demand_forecast.review_status`
from things a human/seed script has to set by hand into things the system
notices on its own, closer to how a real "purchasing system", a real
"supplier confirms less than ordered" webhook, and a real sales-tracking
feed would behave.

All three scans are deliberately "one shot per sku+warehouse": once a
recommendation, shortfall, or spike alert has ever been created for a given
(sku, warehouse) pair, the scan won't create another one even if the
underlying numbers are still unhealthy on the next cron cycle -- otherwise
an unresolved (never approved) situation would get re-flagged and re-run by
the agent every single cycle forever. A human approving/rejecting the
resulting decision is what's expected to actually resolve the underlying
numbers; the scan's job is only detection, once per situation.

Called by app/scheduler.py before each cron sweep, and available to call
manually (e.g. from a script or a future "scan now" endpoint).
"""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Product, Inventory, DemandForecast, PurchaseOrder, PurchaseRecommendation
from app.tools import read_tools

# Coverage (on-hand + confirmed incoming) outside this band relative to
# projected demand is considered worth a human/agent look -- too low risks a
# stockout, too high risks dead stock / wasted storage & budget.
HEALTHY_COVERAGE_MIN_RATIO = 0.9
HEALTHY_COVERAGE_MAX_RATIO = 1.5

# A run-rate at least this many times the naively-expected rate
# (forecast_qty / period_days) counts as a genuine demand spike, not normal
# day-to-day variance.
SPIKE_RATIO_THRESHOLD = 1.3


def scan_for_recommendation_candidates(db: Session) -> list[PurchaseRecommendation]:
    """Scenario 1 detection: for every (sku, warehouse) with a demand
    forecast, compute projected coverage vs demand. If it falls outside the
    healthy band AND no recommendation has ever been created for that pair
    before, create one with a naively-computed suggested quantity -- playing
    the role of the upstream "purchasing system" the assignment describes."""
    created = []
    for inv in db.execute(select(Inventory)).scalars().all():
        sku, warehouse_id = inv.sku, inv.warehouse_id

        already_seen = db.execute(
            select(PurchaseRecommendation).where(
                PurchaseRecommendation.sku == sku, PurchaseRecommendation.warehouse_id == warehouse_id,
            )
        ).scalars().first()
        if already_seen:
            continue

        # If under-coverage is already explained by an active supplier
        # shortfall, that's a Scenario 2 case, not a fresh Scenario 1
        # recommendation -- don't double-flag the same root cause.
        active_shortfall = db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.sku == sku, PurchaseOrder.warehouse_id == warehouse_id,
                PurchaseOrder.status == "open", PurchaseOrder.confirmed_qty < PurchaseOrder.ordered_qty,
            )
        ).scalars().first()
        if active_shortfall:
            continue

        # Likewise, if a demand-spike alert already exists (or is being
        # created this same cycle) for this pair, that's Scenario 3's job to
        # revise the existing plan -- not a fresh Scenario 1 recommendation.
        active_spike = db.execute(
            select(DemandForecast).where(
                DemandForecast.sku == sku, DemandForecast.warehouse_id == warehouse_id,
                DemandForecast.review_status != "none",
            )
        ).scalars().first()
        if active_spike:
            continue

        forecast = read_tools.get_demand_forecast(db, sku, warehouse_id)
        if not forecast["forecasts"]:
            continue
        demand = forecast["forecasts"][0]["projected_total_demand_if_trend_continues"]
        if not demand:
            continue

        open_pos = read_tools.get_open_pos(db, sku, warehouse_id)
        coverage = inv.on_hand_qty + open_pos["total_incoming_confirmed"]
        ratio = coverage / demand

        if HEALTHY_COVERAGE_MIN_RATIO <= ratio <= HEALTHY_COVERAGE_MAX_RATIO:
            continue  # healthy -- nothing to flag

        target_coverage = demand + inv.safety_stock
        recommended_qty = max(0, round(target_coverage - coverage))
        reason = (
            f"Automated scan: coverage ({coverage}) vs projected demand ({demand}) plus safety stock "
            f"({inv.safety_stock}) is {'under' if ratio < 1 else 'over'} the healthy range "
            f"({HEALTHY_COVERAGE_MIN_RATIO}x-{HEALTHY_COVERAGE_MAX_RATIO}x) -- flagged for review."
        )
        rec = PurchaseRecommendation(
            sku=sku, warehouse_id=warehouse_id, recommended_qty=recommended_qty,
            reason=reason, status="pending",
        )
        db.add(rec)
        created.append(rec)

    db.commit()
    return created


def scan_for_shortfalls(db: Session) -> list[PurchaseOrder]:
    """Scenario 2 detection: any open purchase order where the supplier
    confirmed less than was ordered IS a shortfall -- no separate flag is
    needed to know that, it's directly observable from the two quantities.
    `shortfall_status` only tracks review progress, so a PO is only ever
    flagged once (shortfall_status starts at "none")."""
    flagged = []
    candidates = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.status == "open", PurchaseOrder.shortfall_status == "none")
    ).scalars().all()
    for po in candidates:
        if po.confirmed_qty < po.ordered_qty:
            po.shortfall_status = "pending"
            flagged.append(po)

    db.commit()
    return flagged


def scan_for_demand_spikes(db: Session) -> list[DemandForecast]:
    """Scenario 3 detection: a product whose actual sales imply a run-rate
    at least SPIKE_RATIO_THRESHOLD times the naively-expected rate IS a
    demand spike -- directly observable from the forecast's own actuals.
    Only flagged if there's an existing open PO for the pair (per the
    assignment's own framing: "There is an existing purchase order, but...
    may no longer be sufficient") AND that PO plus on-hand no longer covers
    the spiked-up projection; a spike with no PO in play, or one the
    existing plan still comfortably covers, isn't a Scenario 3 case."""
    flagged = []
    candidates = db.execute(
        select(DemandForecast).where(DemandForecast.review_status == "none", DemandForecast.days_elapsed > 0)
    ).scalars().all()
    for forecast in candidates:
        naive_daily_rate = forecast.forecast_qty / forecast.period_days
        actual_daily_rate = forecast.actual_qty_to_date / forecast.days_elapsed
        if naive_daily_rate <= 0 or actual_daily_rate < naive_daily_rate * SPIKE_RATIO_THRESHOLD:
            continue  # normal variance, not a genuine spike

        existing_po = db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.sku == forecast.sku, PurchaseOrder.warehouse_id == forecast.warehouse_id,
                PurchaseOrder.status == "open",
            )
        ).scalars().first()
        if not existing_po:
            continue  # no existing plan to revise -- Scenario 1's scan handles this instead

        inv = db.execute(
            select(Inventory).where(Inventory.sku == forecast.sku, Inventory.warehouse_id == forecast.warehouse_id)
        ).scalars().first()
        on_hand = inv.on_hand_qty if inv else 0
        open_pos = read_tools.get_open_pos(db, forecast.sku, forecast.warehouse_id)
        coverage = on_hand + open_pos["total_incoming_confirmed"]
        projected_demand = actual_daily_rate * forecast.period_days

        if coverage >= projected_demand:
            continue  # existing plan still covers the spike -- nothing to revise

        forecast.review_status = "pending"
        flagged.append(forecast)

    db.commit()
    return flagged


def scan_all(db: Session) -> dict:
    """Runs all three detectors, demand spikes first so the recommendation
    scan can skip pairs Scenario 3 is already handling. Returns counts for
    logging/status reporting."""
    spikes = scan_for_demand_spikes(db)
    recs = scan_for_recommendation_candidates(db)
    shortfalls = scan_for_shortfalls(db)
    return {
        "new_recommendations": [{"sku": r.sku, "warehouse_id": r.warehouse_id, "recommended_qty": r.recommended_qty} for r in recs],
        "new_shortfalls": [{"po_id": p.po_id, "sku": p.sku, "warehouse_id": p.warehouse_id} for p in shortfalls],
        "new_demand_spikes": [{"forecast_id": f.id, "sku": f.sku, "warehouse_id": f.warehouse_id} for f in spikes],
    }
