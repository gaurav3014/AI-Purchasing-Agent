"""
Runs the purchasing agent on a fixed cron-style schedule instead of waiting
for a human to click "submit" -- a real purchasing system wouldn't sit idle
until someone opens a UI, it would pick up new situations on its own on
some cadence.

Every `settings.scenario1_cron_interval_seconds` (default 60s; set to 0 to
disable), `_run_job` does two things in order:

  1. Scan (app/services/scanner.py): looks at every product's real
     inventory/demand/open-PO data and decides for itself whether a
     Scenario 1, 2, or 3 situation currently exists -- e.g. coverage vs
     demand falling outside a healthy band, a PO whose supplier confirmed
     less than ordered, or actual sales implying a genuine spike against an
     existing PO. This is what makes the pending queues self-populating
     instead of requiring something else to flag them by hand. A healthy
     product is never flagged and never shows up anywhere.

  2. Review: sweeps all three trigger queues (Scenario 1's
     purchase_recommendations, Scenario 2's shortfalled purchase_orders,
     Scenario 3's spiked demand_forecast rows -- now including whatever the
     scan just added) using the exact same per-scenario logic the manual
     API endpoints call (see app/services/scenario1.py, scenario2.py,
     scenario3.py).

The job only ever *proposes*; nothing is written to purchase_orders without
a human approving it (Proposal Pattern, see tools/write_tools.py), so
running this unattended is safe.

`_state["last_run_summary"]` is a per-product breakdown (sku, which case,
decision) of what the last run actually found and did -- not just a count
-- polled by GET /api/scenario1/schedule so the frontend can show a live
"cron is running right now" indicator and what it turned up.
"""
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from app.config import settings
from app.db import SessionLocal

logger = logging.getLogger("scheduler")

_scheduler = BackgroundScheduler()
_JOB_ID = "purchasing_agent_review"

_state = {
    "is_running": False,
    "last_run_started_at": None,
    "last_run_finished_at": None,
    "last_run_reviewed_count": None,
    "last_run_summary": [],
    "last_run_error": None,
}


def _run_job():
    from app.models import Product
    from app.services import scenario1, scenario2, scenario3, scanner

    _state["is_running"] = True
    _state["last_run_started_at"] = datetime.now().isoformat()
    _state["last_run_error"] = None
    db = SessionLocal()
    try:
        scan_result = scanner.scan_all(db)
        if any(scan_result.values()):
            logger.info(f"[cron] scan flagged new cases: {scan_result}")
        logs = (
            scenario1.process_all_pending(db)
            + scenario2.process_all_pending(db)
            + scenario3.process_all_pending(db)
        )
        _state["last_run_reviewed_count"] = len(logs)
        _state["last_run_summary"] = [
            {
                "sku": l.sku,
                "product_name": (db.get(Product, l.sku).name if l.sku and db.get(Product, l.sku) else l.sku),
                "scenario": l.scenario, "decision": l.decision, "requires_human_approval": l.requires_human_approval,
            }
            for l in logs
        ]
        if logs:
            logger.info(f"[cron] reviewed {len(logs)} pending case(s): {_state['last_run_summary']}")
    except Exception as exc:
        _state["last_run_error"] = str(exc)
        logger.exception("[cron] job run failed")
    finally:
        db.close()
        _state["is_running"] = False
        _state["last_run_finished_at"] = datetime.now().isoformat()


def start():
    if settings.scenario1_cron_interval_seconds <= 0:
        logger.info("Purchasing agent cron job disabled (scenario1_cron_interval_seconds <= 0).")
        return
    _scheduler.add_job(
        _run_job, "interval", seconds=settings.scenario1_cron_interval_seconds,
        id=_JOB_ID, replace_existing=True, max_instances=1,
        next_run_time=datetime.now(),  # run once immediately on startup, then every interval after
    )
    if not _scheduler.running:
        _scheduler.start()
    logger.info(f"Purchasing agent cron job started: every {settings.scenario1_cron_interval_seconds}s.")


def stop():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


def get_schedule_status() -> dict:
    job = _scheduler.get_job(_JOB_ID) if _scheduler.running else None
    return {
        "enabled": settings.scenario1_cron_interval_seconds > 0,
        "interval_seconds": settings.scenario1_cron_interval_seconds,
        "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
        **_state,
    }
