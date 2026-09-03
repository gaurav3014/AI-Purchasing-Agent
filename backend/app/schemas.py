from pydantic import BaseModel


class RecommendationReviewRequest(BaseModel):
    recommendation_id: int | None = None  # if omitted, picks the oldest pending recommendation


class ShortfallReviewRequest(BaseModel):
    po_id: str | None = None  # if omitted, picks the oldest pending supplier shortfall


class DemandSpikeReviewRequest(BaseModel):
    forecast_id: int | None = None  # if omitted, picks the oldest pending demand spike


class ApprovalDecisionRequest(BaseModel):
    log_id: int
    approve: bool
    note: str = ""


class DecisionResponse(BaseModel):
    log_id: int
    run_id: str
    scenario: str
    sku: str | None
    recommendation_id: int | None
    triggered_by: str  # "manual" or "cron"
    decision: str
    reasoning: str
    factors_considered: list[str]
    confidence: float
    action_summary: str
    action_result: dict | None
    executed: bool
    tool_call_trace: list[dict]
    requires_human_approval: bool
    approval_reasons: list[str]
    approval_status: str
    approval_note: str
    approved_at: str | None
    validation_result: dict | None
    validation_passed: bool | None
    retry_count: int
