"""
Seed the SQLite database with mock data for Scenario 1 (Purchase
Recommendation Review) and Scenario 2 (Supplier Cannot Fulfil the
Purchase). Scenario 3-4 seed data will be added when those scenarios are
built.

Four products, four different warehouses, four genuinely different correct
answers -- so the cron job (app/scheduler.py) reviewing "every pending
recommendation" each cycle is a real multi-product exercise, not just one
row processed in a loop of one:

SKU-001 "Everyday Oat Milk 1L", warehouse WH-001 -- expect REJECT.
  A pending recommendation asks for 800 more units. Inventory + open PO +
  demand are set up so the "correct" answer is genuinely non-obvious: the
  recommendation is too high given existing coverage, so a careful agent
  should reject it (or modify it down), not blindly accept it. Budget and
  storage are comfortable here, so this case isolates the demand/coverage
  reasoning from the constraint-handling reasoning.

SKU-002 "Frozen Berry Mix 500g", warehouse WH-002 -- expect MODIFY.
  A pending recommendation asks for 350 units, but the real gap (demand
  minus on-hand) rounds up to 400 units once MOQ is applied -- close to the
  recommendation, but not exact, so a careful agent should adjust the
  quantity rather than accept 350 or reject outright.

SKU-003 "Eco Coffee Cups 100pk", warehouse WH-003 -- expect ACCEPT.
  A pending recommendation asks for exactly the supplier's MOQ (260 units),
  which also happens to exactly close the real demand gap -- the
  recommendation is already correct as given.

SKU-004 "Bulk Basmati Rice 25kg", warehouse WH-004 -- expect INVESTIGATE/ESCALATE.
  A pending recommendation asks for 600 more units, and the math says a
  meaningful order is genuinely warranted -- but the warehouse's budget and
  storage are both nearly exhausted, forcing the agent to find a compliant
  course of action (a smaller compliant order, or escalation) instead of
  blindly proposing the recommended quantity.

SKU-005 "Kids Multivitamin Gummies", warehouse WH-005 -- expect MODIFY (up).
  Demand-spike case: forecast assumed normal demand (300/30 days), but
  actual sales 10 days into the window are already at 260 -- an implied
  run-rate of 26/day, projecting to ~780 for the full month. The pending
  recommendation (150 units) was sized off the ORIGINAL forecast and is now
  far too low; a careful agent should increase it well past what was
  recommended, not accept the stale number.

SKU-006 "Premium Roast Coffee 250g", warehouse WH-006 -- expect ACCEPT (order placed).
  Low-inventory case: on-hand (15 units) is already below the 40-unit
  safety stock target, with nothing incoming. The recommendation (300
  units) happens to already match the real gap once rounded to MOQ, so the
  correct answer is to accept it and place the order -- unlike SKU-003's
  accept case, this one is driven by a genuine stock shortage, not a
  precise coincidence with no urgency behind it.

SKU-007 "Sparkling Water 12-pack", warehouse WH-007 -- expect REJECT (no update needed).
  Ample-stock case: on-hand (700 units) alone already covers 1.75x the
  30-day forecast (400 units), with nothing incoming. The
  recommendation (500 more units) is entirely unjustified -- inventory does
  not need to be touched at all.

--- Scenario 2 (Supplier Cannot Fulfil the Purchase) ---

SKU-008 "Highland Dark Roast 250g", warehouse WH-008 -- expect ADDITIONAL_PO.
  PO-8001 was placed with the primary supplier (SUP-11, 21-day lead time)
  for 500 units; the supplier has now confirmed only 250 -- a 250-unit
  shortfall (PurchaseOrder.shortfall_status="pending" is what makes this
  Scenario 2's trigger queue, mirroring purchase_recommendations for
  Scenario 1). On-hand (60) + the reduced confirmed qty (250) still falls
  well short of the 600-unit forecast, so the shortfall is a genuine
  problem, not a rounding error. An alternate supplier (SUP-12) exists with
  a much shorter lead time (8 vs 21 days) -- exactly the "should another
  supplier be considered" tradeoff the assignment describes -- so a careful
  agent should propose a new order with SUP-12 to close the gap rather than
  wait on the slow primary supplier or escalate unnecessarily.

--- Proactive scan demo (app/services/scanner.py) ---

SKU-001..008 above are all PRE-flagged (a recommendation or shortfall row
already exists) so their review can be demonstrated immediately without
waiting on a scan. SKU-009 and SKU-010 are deliberately seeded with NO
pending recommendation and NO shortfall flag at all -- they only get
reviewed once the cron's scan step (scanner.scan_all) notices their numbers
are unhealthy and creates the trigger row itself, proving the detection is
real and not just a re-read of hand-seeded data.

SKU-009 "Vanilla Soft Drink 6-pack", warehouse WH-009 -- no recommendation exists yet.
  On-hand (20) vs demand (300) is a 0.07x coverage ratio, far under the
  healthy band -- the scan should create a PurchaseRecommendation for it on
  the next cron cycle, which the agent then reviews like any other.

SKU-010 "Herbal Tea Box 20ct", warehouse WH-010 -- no shortfall flag set yet.
  PO-10001 was confirmed at 150 of 300 ordered (shortfall_status left at its
  default "none", not manually flagged "pending" as PO-8001 was) -- the
  scan should notice confirmed_qty < ordered_qty on its own and flip it to
  "pending" for Scenario 2 to pick up.

--- Healthy control group (should NEVER be flagged) ---

SKU-011/012/013 are plain, ordinary data: coverage comfortably inside the
scan's healthy band (0.9x-1.5x demand), no shortfalled PO. They exist to
prove the scan is actually selective -- if it flagged everything, these
three would show up too. They should never appear in
purchase_recommendations, purchase_orders.shortfall_status, decision_log,
or the "Flagged" list in the UI, no matter how many times the cron runs.

SKU-011 "Classic Granola Bars 12pk", warehouse WH-011 -- on-hand (350) vs
  demand (300) = 1.17x coverage, no open PO needed.
SKU-012 "Almond Milk 1L", warehouse WH-012 -- on-hand (200) + a normal
  (non-shortfalled, confirmed_qty == ordered_qty) open PO (100) vs demand
  (250) = 1.2x coverage.
SKU-013 "Green Tea Bags 100ct", warehouse WH-013 -- on-hand (190) vs demand
  (200) = 0.95x coverage, comfortably inside the band.

--- Scenario 3 (Demand/Forecast Has Changed) ---

SKU-014 "Party Balloons 50pk", warehouse WH-014 -- no spike flag set yet.
  PO-14001 was placed for 200 units (confirmed in full, NOT a shortfall) --
  a normal purchasing plan sized for the ORIGINAL forecast (300/30 days,
  ~10/day). But 8 days in, actual sales are already 200 units (25/day, 2.5x
  the planned pace) -- a genuine spike. On-hand (100) + the existing PO
  (200) = 300 units of coverage, but at the new pace demand projects to
  ~750 -- the existing plan is no longer sufficient. review_status is left
  at its default "none" -- only the daily AI triage (agent/triage.py) should
  notice the accelerated run-rate against the existing PO and flag it.
"""
from datetime import datetime, timedelta
from app.db import Base, engine, SessionLocal
from app.models import (
    Product, Warehouse, Inventory, DemandForecast, SupplierCatalog,
    PurchaseOrder, PurchaseRecommendation,
)

NOW = datetime(2026, 9, 2)


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # ---- Products ----
    db.add_all([
        Product(sku="SKU-001", name="Everyday Oat Milk 1L", category="dairy-alt", unit_cost=1.80, unit_volume=1.0),
        Product(sku="SKU-002", name="Frozen Berry Mix 500g", category="frozen", unit_cost=3.20, unit_volume=1.0),
        Product(sku="SKU-003", name="Eco Coffee Cups 100pk", category="packaging", unit_cost=8.50, unit_volume=1.0),
        Product(sku="SKU-004", name="Bulk Basmati Rice 25kg", category="staples", unit_cost=22.00, unit_volume=1.0),
        Product(sku="SKU-005", name="Kids Multivitamin Gummies", category="health", unit_cost=6.20, unit_volume=1.0),
        Product(sku="SKU-006", name="Premium Roast Coffee 250g", category="beverages", unit_cost=4.50, unit_volume=1.0),
        Product(sku="SKU-007", name="Sparkling Water 12-pack", category="beverages", unit_cost=5.00, unit_volume=1.0),
        Product(sku="SKU-008", name="Highland Dark Roast 250g", category="beverages", unit_cost=4.80, unit_volume=1.0),
        Product(sku="SKU-009", name="Vanilla Soft Drink 6-pack", category="beverages", unit_cost=3.50, unit_volume=1.0),
        Product(sku="SKU-010", name="Herbal Tea Box 20ct", category="beverages", unit_cost=6.00, unit_volume=1.0),
        # Healthy control group -- should never be flagged by the scan.
        Product(sku="SKU-011", name="Classic Granola Bars 12pk", category="snacks", unit_cost=2.90, unit_volume=1.0),
        Product(sku="SKU-012", name="Almond Milk 1L", category="dairy-alt", unit_cost=2.10, unit_volume=1.0),
        Product(sku="SKU-013", name="Green Tea Bags 100ct", category="beverages", unit_cost=4.20, unit_volume=1.0),
        Product(sku="SKU-014", name="Party Balloons 50pk", category="seasonal", unit_cost=3.00, unit_volume=1.0),
    ])

    # ---- Warehouses (budget + storage constraints live here now) ----
    db.add_all([
        Warehouse(warehouse_id="WH-001", name="Warehouse 1 - Dairy/Alt Hub",
                  available_budget=1800.0, max_storage=2000, current_storage=1050),
        Warehouse(warehouse_id="WH-002", name="Warehouse 2 - Frozen Hub",
                  available_budget=2000.0, max_storage=1500, current_storage=600),
        Warehouse(warehouse_id="WH-003", name="Warehouse 3 - Packaging Hub",
                  available_budget=5000.0, max_storage=1000, current_storage=200),
        # staples budget and storage are both nearly exhausted -> drives the constraint case.
        Warehouse(warehouse_id="WH-004", name="Warehouse 4 - Staples Hub",
                  available_budget=350.0, max_storage=1200, current_storage=1080),
        Warehouse(warehouse_id="WH-005", name="Warehouse 5 - Health Hub",
                  available_budget=5000.0, max_storage=2000, current_storage=400),
        Warehouse(warehouse_id="WH-006", name="Warehouse 6 - Beverages Hub A",
                  available_budget=3000.0, max_storage=1200, current_storage=300),
        Warehouse(warehouse_id="WH-007", name="Warehouse 7 - Beverages Hub B",
                  available_budget=2000.0, max_storage=1000, current_storage=300),
        Warehouse(warehouse_id="WH-008", name="Warehouse 8 - Beverages Hub C",
                  available_budget=3000.0, max_storage=1200, current_storage=500),
        Warehouse(warehouse_id="WH-009", name="Warehouse 9 - Beverages Hub D",
                  available_budget=2000.0, max_storage=1000, current_storage=200),
        Warehouse(warehouse_id="WH-010", name="Warehouse 10 - Beverages Hub E",
                  available_budget=3000.0, max_storage=1000, current_storage=300),
        # Healthy control group.
        Warehouse(warehouse_id="WH-011", name="Warehouse 11 - Snacks Hub",
                  available_budget=2000.0, max_storage=1000, current_storage=400),
        Warehouse(warehouse_id="WH-012", name="Warehouse 12 - Dairy/Alt Hub B",
                  available_budget=2000.0, max_storage=1000, current_storage=350),
        Warehouse(warehouse_id="WH-013", name="Warehouse 13 - Beverages Hub F",
                  available_budget=2000.0, max_storage=1000, current_storage=250),
        Warehouse(warehouse_id="WH-014", name="Warehouse 14 - Seasonal Hub",
                  available_budget=3000.0, max_storage=1500, current_storage=300),
    ])

    # ---- Inventory ----
    db.add_all([
        Inventory(sku="SKU-001", warehouse_id="WH-001", on_hand_qty=650, safety_stock=150),
        Inventory(sku="SKU-002", warehouse_id="WH-002", on_hand_qty=80, safety_stock=50),
        Inventory(sku="SKU-003", warehouse_id="WH-003", on_hand_qty=40, safety_stock=20),
        Inventory(sku="SKU-004", warehouse_id="WH-004", on_hand_qty=300, safety_stock=100),
        Inventory(sku="SKU-005", warehouse_id="WH-005", on_hand_qty=120, safety_stock=50),
        # below the 40-unit safety stock target -- a genuine shortage, not just "low".
        Inventory(sku="SKU-006", warehouse_id="WH-006", on_hand_qty=15, safety_stock=40),
        # 700 vs 400 demand is comfortably oversupplied without tripping the
        # validator's own "suspiciously over-covered" (>2x demand) guard.
        Inventory(sku="SKU-007", warehouse_id="WH-007", on_hand_qty=700, safety_stock=100),
        Inventory(sku="SKU-008", warehouse_id="WH-008", on_hand_qty=60, safety_stock=40),
        # Unhealthy on purpose -- nothing pre-flags this, only the scan should notice.
        Inventory(sku="SKU-009", warehouse_id="WH-009", on_hand_qty=20, safety_stock=30),
        Inventory(sku="SKU-010", warehouse_id="WH-010", on_hand_qty=100, safety_stock=50),
        # Healthy control group -- comfortably inside the 0.9x-1.5x band, never flagged.
        Inventory(sku="SKU-011", warehouse_id="WH-011", on_hand_qty=350, safety_stock=50),
        Inventory(sku="SKU-012", warehouse_id="WH-012", on_hand_qty=200, safety_stock=40),
        Inventory(sku="SKU-013", warehouse_id="WH-013", on_hand_qty=190, safety_stock=30),
        # On-hand + existing PO (200) no longer covers the spiked demand (~750).
        Inventory(sku="SKU-014", warehouse_id="WH-014", on_hand_qty=100, safety_stock=50),
    ])

    # ---- Demand forecasts ----
    db.add_all([
        # SKU-001: forecast 900 units over next 30 days, nothing unusual.
        DemandForecast(sku="SKU-001", warehouse_id="WH-001", period_start=NOW, period_days=30,
                        forecast_qty=900, actual_qty_to_date=0, days_elapsed=0),
        # SKU-002: forecast 400/30 days, nothing unusual.
        DemandForecast(sku="SKU-002", warehouse_id="WH-002", period_start=NOW, period_days=30,
                        forecast_qty=400, actual_qty_to_date=0, days_elapsed=0),
        # SKU-003: forecast 300/30 days, nothing unusual.
        DemandForecast(sku="SKU-003", warehouse_id="WH-003", period_start=NOW, period_days=30,
                        forecast_qty=300, actual_qty_to_date=0, days_elapsed=0),
        # SKU-004: forecast 500/30 days, steady.
        DemandForecast(sku="SKU-004", warehouse_id="WH-004", period_start=NOW, period_days=30,
                        forecast_qty=500, actual_qty_to_date=210, days_elapsed=12),
        # SKU-005: forecast 300/30 days ("normal"), but 10 days in actual sales are
        # already at 260 -> massively ahead of pace (spike). Implied run-rate 26/day,
        # projected full-period demand ~780.
        DemandForecast(sku="SKU-005", warehouse_id="WH-005", period_start=NOW - timedelta(days=10),
                        period_days=30, forecast_qty=300, actual_qty_to_date=260, days_elapsed=10),
        # SKU-006: forecast 300/30 days, nothing unusual -- the shortage is purely
        # about how little is on hand, not a demand surprise.
        DemandForecast(sku="SKU-006", warehouse_id="WH-006", period_start=NOW, period_days=30,
                        forecast_qty=300, actual_qty_to_date=0, days_elapsed=0),
        # SKU-007: forecast 400/30 days, nothing unusual.
        DemandForecast(sku="SKU-007", warehouse_id="WH-007", period_start=NOW, period_days=30,
                        forecast_qty=400, actual_qty_to_date=0, days_elapsed=0),
        # SKU-008: forecast 600/30 days -- high enough that the post-shortfall
        # coverage (on-hand + reduced confirmed qty) genuinely falls short.
        DemandForecast(sku="SKU-008", warehouse_id="WH-008", period_start=NOW, period_days=30,
                        forecast_qty=600, actual_qty_to_date=0, days_elapsed=0),
        # SKU-009/010: nothing unusual demand-wise -- their issues are purely
        # about supply (low stock / a shortfall the scan should discover).
        DemandForecast(sku="SKU-009", warehouse_id="WH-009", period_start=NOW, period_days=30,
                        forecast_qty=300, actual_qty_to_date=0, days_elapsed=0),
        DemandForecast(sku="SKU-010", warehouse_id="WH-010", period_start=NOW, period_days=30,
                        forecast_qty=400, actual_qty_to_date=0, days_elapsed=0),
        # Healthy control group.
        DemandForecast(sku="SKU-011", warehouse_id="WH-011", period_start=NOW, period_days=30,
                        forecast_qty=300, actual_qty_to_date=0, days_elapsed=0),
        DemandForecast(sku="SKU-012", warehouse_id="WH-012", period_start=NOW, period_days=30,
                        forecast_qty=250, actual_qty_to_date=0, days_elapsed=0),
        DemandForecast(sku="SKU-013", warehouse_id="WH-013", period_start=NOW, period_days=30,
                        forecast_qty=200, actual_qty_to_date=0, days_elapsed=0),
        # SKU-014: forecast 300/30 days (~10/day), but 8 days in actual sales are
        # already at 200 -> 25/day, 2.5x the planned pace (a genuine spike).
        DemandForecast(sku="SKU-014", warehouse_id="WH-014", period_start=NOW - timedelta(days=8),
                        period_days=30, forecast_qty=300, actual_qty_to_date=200, days_elapsed=8),
    ])

    # ---- Supplier catalog (unified supplier + terms) ----
    db.add_all([
        SupplierCatalog(supplier_id="SUP-01", supplier_name="Meadow Farms Co-op", sku="SKU-001",
                         unit_cost=1.80, moq=200, lead_time_days=10, reliability_score=0.95, is_primary=True),
        SupplierCatalog(supplier_id="SUP-06", supplier_name="Frosty Foods Ltd", sku="SKU-002",
                         unit_cost=3.20, moq=100, lead_time_days=12, reliability_score=0.90, is_primary=True),
        SupplierCatalog(supplier_id="SUP-07", supplier_name="GreenPack Supplies", sku="SKU-003",
                         unit_cost=8.50, moq=260, lead_time_days=9, reliability_score=0.92, is_primary=True),
        SupplierCatalog(supplier_id="SUP-05", supplier_name="Golden Grain Exports", sku="SKU-004",
                         unit_cost=22.00, moq=250, lead_time_days=18, reliability_score=0.93, is_primary=True),
        SupplierCatalog(supplier_id="SUP-08", supplier_name="Sunshine Health Mfg", sku="SKU-005",
                         unit_cost=6.20, moq=150, lead_time_days=14, reliability_score=0.90, is_primary=True),
        SupplierCatalog(supplier_id="SUP-09", supplier_name="Highland Roasters Ltd", sku="SKU-006",
                         unit_cost=4.50, moq=100, lead_time_days=21, reliability_score=0.88, is_primary=True),
        SupplierCatalog(supplier_id="SUP-10", supplier_name="Crystal Springs Beverages", sku="SKU-007",
                         unit_cost=5.00, moq=150, lead_time_days=15, reliability_score=0.91, is_primary=True),
        # SKU-008 has two suppliers: the slow primary (already shortfalled) and a
        # faster, slightly pricier alternate -- the real choice Scenario 2 poses.
        SupplierCatalog(supplier_id="SUP-11", supplier_name="Highland Roasters Co", sku="SKU-008",
                         unit_cost=4.80, moq=100, lead_time_days=21, reliability_score=0.87, is_primary=True),
        SupplierCatalog(supplier_id="SUP-12", supplier_name="QuickBean Traders", sku="SKU-008",
                         unit_cost=5.10, moq=50, lead_time_days=8, reliability_score=0.85, is_primary=False),
        SupplierCatalog(supplier_id="SUP-13", supplier_name="Fizzworks Beverages", sku="SKU-009",
                         unit_cost=3.50, moq=100, lead_time_days=10, reliability_score=0.90, is_primary=True),
        SupplierCatalog(supplier_id="SUP-14", supplier_name="Leaf & Herb Co", sku="SKU-010",
                         unit_cost=6.00, moq=100, lead_time_days=14, reliability_score=0.89, is_primary=True),
        SupplierCatalog(supplier_id="SUP-15", supplier_name="Herbal Direct", sku="SKU-010",
                         unit_cost=6.30, moq=50, lead_time_days=6, reliability_score=0.86, is_primary=False),
        # Healthy control group.
        SupplierCatalog(supplier_id="SUP-16", supplier_name="Golden Oats Snacks", sku="SKU-011",
                         unit_cost=2.90, moq=100, lead_time_days=12, reliability_score=0.92, is_primary=True),
        SupplierCatalog(supplier_id="SUP-17", supplier_name="Pure Almond Co", sku="SKU-012",
                         unit_cost=2.10, moq=100, lead_time_days=10, reliability_score=0.93, is_primary=True),
        SupplierCatalog(supplier_id="SUP-18", supplier_name="Leaf & Herb Co", sku="SKU-013",
                         unit_cost=4.20, moq=100, lead_time_days=11, reliability_score=0.90, is_primary=True),
        SupplierCatalog(supplier_id="SUP-19", supplier_name="Festive Supplies Ltd", sku="SKU-014",
                         unit_cost=3.00, moq=150, lead_time_days=12, reliability_score=0.88, is_primary=True),
    ])

    # ---- Open purchase orders ----
    db.add_all([
        # SKU-001 already has 400 units incoming, arriving well within the window.
        PurchaseOrder(po_id="PO-1001", sku="SKU-001", warehouse_id="WH-001", supplier_id="SUP-01",
                      ordered_qty=400, confirmed_qty=400, status="open",
                      expected_delivery_date=NOW + timedelta(days=10), created_by="system"),
        # SKU-002/003/004/005/006/007: no open PO yet; the recommendation engine wants to place one.

        # PO-8001: the Scenario 2 trigger. Ordered 500, but the supplier has just
        # confirmed only 250 -- shortfall_status="pending" is what puts this in
        # the Scenario 2 queue (GET /api/scenario2/shortfalls).
        PurchaseOrder(po_id="PO-8001", sku="SKU-008", warehouse_id="WH-008", supplier_id="SUP-11",
                      ordered_qty=500, confirmed_qty=250, status="open",
                      expected_delivery_date=NOW + timedelta(days=21), created_by="system",
                      notes="Supplier confirmed only 250 of 500 ordered units.",
                      shortfall_status="pending"),

        # PO-10001: also a shortfall (confirmed 150 of 300), but shortfall_status
        # is deliberately left at its default "none" -- only the scan should
        # notice confirmed_qty < ordered_qty and flip it to "pending".
        PurchaseOrder(po_id="PO-10001", sku="SKU-010", warehouse_id="WH-010", supplier_id="SUP-14",
                      ordered_qty=300, confirmed_qty=150, status="open",
                      expected_delivery_date=NOW + timedelta(days=14), created_by="system",
                      notes="Supplier confirmed only 150 of 300 ordered units."),

        # PO-12001: a normal, fully-confirmed PO for the healthy control group --
        # confirmed_qty == ordered_qty, so this is NOT a shortfall.
        PurchaseOrder(po_id="PO-12001", sku="SKU-012", warehouse_id="WH-012", supplier_id="SUP-17",
                      ordered_qty=100, confirmed_qty=100, status="open",
                      expected_delivery_date=NOW + timedelta(days=10), created_by="system"),

        # PO-14001: a normal, fully-confirmed PO sized for the ORIGINAL forecast --
        # not a shortfall, but no longer enough once demand spiked. review_status
        # on SKU-014's DemandForecast is left "none"; only the scan should notice.
        PurchaseOrder(po_id="PO-14001", sku="SKU-014", warehouse_id="WH-014", supplier_id="SUP-19",
                      ordered_qty=200, confirmed_qty=200, status="open",
                      expected_delivery_date=NOW + timedelta(days=12), created_by="system"),
    ])

    # ---- Pending purchase recommendations (Scenario 1 trigger queue) ----
    db.add_all([
        PurchaseRecommendation(sku="SKU-001", warehouse_id="WH-001", recommended_qty=800,
                                reason="Reorder point triggered by demand-planning system.", status="pending"),
        PurchaseRecommendation(sku="SKU-002", warehouse_id="WH-002", recommended_qty=350,
                                reason="Reorder point triggered by demand-planning system.", status="pending"),
        PurchaseRecommendation(sku="SKU-003", warehouse_id="WH-003", recommended_qty=260,
                                reason="Reorder point triggered by demand-planning system.", status="pending"),
        PurchaseRecommendation(sku="SKU-004", warehouse_id="WH-004", recommended_qty=600,
                                reason="Reorder point triggered by demand-planning system.", status="pending"),
        PurchaseRecommendation(sku="SKU-005", warehouse_id="WH-005", recommended_qty=150,
                                reason="Reorder point triggered by demand-planning system (pre-spike sizing).", status="pending"),
        PurchaseRecommendation(sku="SKU-006", warehouse_id="WH-006", recommended_qty=300,
                                reason="Low stock alert: on-hand has fallen below safety stock.", status="pending"),
        PurchaseRecommendation(sku="SKU-007", warehouse_id="WH-007", recommended_qty=500,
                                reason="Reorder point triggered by demand-planning system.", status="pending"),
    ])

    db.commit()
    db.close()
    print("Seed complete.")


if __name__ == "__main__":
    seed()
