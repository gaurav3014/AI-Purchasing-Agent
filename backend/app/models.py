"""
SQLAlchemy models for the mock purchasing data.

These stand in for the systems a real buyer would consult: an inventory
system, a demand-planning system, a PO/procurement system, unified supplier
master data, and a warehouse system that tracks both budget and storage.
Kept intentionally simple (SQLite, no migrations) since the assignment calls
for mock data, not production infrastructure.

Currently only the tables Scenario 1 (purchase recommendation review) needs
are populated by seed.py -- the schema is shaped so Scenario 2-4 slot in
without a redesign (e.g. purchase_orders already tracks confirmed_qty
separately from ordered_qty for the Scenario 2 supplier-shortfall case), but
their seed data comes later, one scenario at a time.
"""
from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class Product(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    unit_cost: Mapped[float] = mapped_column(Float)
    unit_volume: Mapped[float] = mapped_column(Float, default=1.0)  # storage units consumed per unit


class Warehouse(Base):
    """A fulfillment node's operational constraints: budget and storage in one place."""
    __tablename__ = "warehouses"

    warehouse_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    available_budget: Mapped[float] = mapped_column(Float)  # remaining spend headroom
    max_storage: Mapped[int] = mapped_column(Integer)
    current_storage: Mapped[int] = mapped_column(Integer)


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String, ForeignKey("products.sku"))
    warehouse_id: Mapped[str] = mapped_column(String, ForeignKey("warehouses.warehouse_id"))
    on_hand_qty: Mapped[int] = mapped_column(Integer)
    safety_stock: Mapped[int] = mapped_column(Integer, default=0)


class DemandForecast(Base):
    __tablename__ = "demand_forecast"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String, ForeignKey("products.sku"))
    warehouse_id: Mapped[str] = mapped_column(String, ForeignKey("warehouses.warehouse_id"))
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_days: Mapped[int] = mapped_column(Integer)  # length of forecast window
    forecast_qty: Mapped[int] = mapped_column(Integer)  # originally expected demand
    actual_qty_to_date: Mapped[int] = mapped_column(Integer)  # actual sales so far in window
    days_elapsed: Mapped[int] = mapped_column(Integer)  # days into the window so far
    # Scenario 3 trigger queue: flips to "pending" when actual sales imply a
    # genuine demand spike against an existing open PO that no longer covers
    # it. Same none/pending/in_review/reviewed pattern as
    # PurchaseOrder.shortfall_status.
    review_status: Mapped[str] = mapped_column(String, default="none")


class SupplierCatalog(Base):
    """Unified supplier master + per-(supplier, sku) terms in one table."""
    __tablename__ = "supplier_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_id: Mapped[str] = mapped_column(String)
    supplier_name: Mapped[str] = mapped_column(String)
    sku: Mapped[str] = mapped_column(String, ForeignKey("products.sku"))
    unit_cost: Mapped[float] = mapped_column(Float)
    moq: Mapped[int] = mapped_column(Integer)
    lead_time_days: Mapped[int] = mapped_column(Integer)
    reliability_score: Mapped[float] = mapped_column(Float)  # 0-1, historical on-time-in-full rate
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    po_id: Mapped[str] = mapped_column(String, primary_key=True)
    sku: Mapped[str] = mapped_column(String, ForeignKey("products.sku"))
    warehouse_id: Mapped[str] = mapped_column(String, ForeignKey("warehouses.warehouse_id"))
    supplier_id: Mapped[str] = mapped_column(String)
    ordered_qty: Mapped[int] = mapped_column(Integer)
    confirmed_qty: Mapped[int] = mapped_column(Integer)  # what supplier has confirmed it can fulfil
    status: Mapped[str] = mapped_column(String)  # open, partially_fulfilled, fulfilled, cancelled
    expected_delivery_date: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String, default="system")  # "system" or "agent"
    notes: Mapped[str] = mapped_column(Text, default="")
    # Scenario 2 trigger queue: flips to "pending" when a supplier confirms
    # less than was ordered (confirmed_qty < ordered_qty). "in_review" /
    # "reviewed" mirror PurchaseRecommendation.status's claim pattern so cron
    # and manual review can't double-process the same shortfall.
    shortfall_status: Mapped[str] = mapped_column(String, default="none")  # none, pending, in_review, reviewed


class PurchaseRecommendation(Base):
    """The trigger queue for Scenario 1: a system-generated recommendation
    awaiting agent (then human) review."""
    __tablename__ = "purchase_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String, ForeignKey("products.sku"))
    warehouse_id: Mapped[str] = mapped_column(String, ForeignKey("warehouses.warehouse_id"))
    recommended_qty: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, reviewed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DecisionLog(Base):
    """
    Full audit trail of every agent run: the triggering event, the evidence it
    gathered, its reasoning, the action it proposed, and the outcome of
    validating that decision. This is both the feedback-loop record and the
    basis for the evaluation harness.

    `action_taken` holds the *proposed* action (per the Proposal Pattern --
    see tools/write_tools.py) until a human approves it, at which point
    `executed` flips to True and `action_taken` is updated with the real
    po_id created by the ACID transaction.
    """
    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String)
    scenario: Mapped[str] = mapped_column(String)
    sku: Mapped[str] = mapped_column(String, nullable=True)
    recommendation_id: Mapped[int] = mapped_column(Integer, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String, default="manual")  # "manual" or "cron"
    trigger_event: Mapped[str] = mapped_column(Text)  # JSON string
    tool_calls: Mapped[str] = mapped_column(Text)  # JSON string, ordered trace
    decision: Mapped[str] = mapped_column(String)  # accept/modify/reject/investigate/etc
    reasoning: Mapped[str] = mapped_column(Text)
    factors_considered: Mapped[str] = mapped_column(Text)  # JSON string
    confidence: Mapped[float] = mapped_column(Float)
    action_taken: Mapped[str] = mapped_column(Text)  # JSON string (proposed or executed action), or "none"
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_status: Mapped[str] = mapped_column(String, default="n/a")  # n/a, pending, approved, rejected
    approval_note: Mapped[str] = mapped_column(Text, default="")
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    validation_result: Mapped[str] = mapped_column(Text, default="")  # JSON string
    validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
