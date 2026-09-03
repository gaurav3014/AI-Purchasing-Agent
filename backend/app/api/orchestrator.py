"""
Ties together: run the agent -> validate the outcome -> retry once if
validation fails -> decide whether human approval is required -> persist
everything to DecisionLog.

This is deliberately scenario-generic so Scenario 2/3/4 reuse the same
`run_scenario` function -- only the situation prompt, allowed decisions,
and validator differ per scenario.

Proposal Pattern: `run_scenario` never writes a purchase order. If the
agent proposed one and it passed guardrails, that proposal sits in
DecisionLog.action_taken with approval_status="pending" until a human
calls `apply_approval_decision` -- which is the only place
write_tools.execute_purchase_order ever runs.

Every step below is logged via the "agent" logger (situation, evidence
gathered, decision made, validation outcome, approval/execution result) so
the full trace of what the agent thought and did is visible in the server
console/log file, not just queryable from decision_log afterwards.
"""
import json
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from app.agent.core import run_agent, AgentResult
from app.agent import validation
from app.tools import policy, write_tools
from app.models import DecisionLog

logger = logging.getLogger("agent")

MAX_RETRIES = 1


def _estimate_action_cost(action_result: dict | None) -> float:
    if not action_result:
        return 0.0
    return action_result.get("cost", 0.0)


def _remaining_budget(db: Session, warehouse_id: str) -> float:
    from app.tools.read_tools import get_warehouse
    w = get_warehouse(db, warehouse_id)
    return w.get("available_budget", 0.0)


def run_scenario(
    db: Session,
    scenario: str,
    situation_prompt: str,
    allowed_decisions: list[str],
    trigger_event: dict,
    warehouse_id: str | None = None,
    validator_kwargs: dict | None = None,
    recommendation_id: int | None = None,
    triggered_by: str = "manual",
) -> DecisionLog:
    validator_kwargs = validator_kwargs or {}
    sku = trigger_event.get("sku")
    logger.info(f"[{triggered_by}] {scenario} started for sku={sku} warehouse={warehouse_id}: {situation_prompt}")

    def _validate(result: AgentResult) -> dict:
        if not validator_kwargs:
            return {"passed": True, "checks": []}
        # Under the Proposal Pattern nothing is written to the DB at this
        # point, so the validator needs the agent's actual (unexecuted)
        # proposal -- not the placeholder passed in by the caller -- to
        # compute projected coverage correctly.
        kwargs = {**validator_kwargs, "action_result": result.action_result}
        return validation.validate(scenario, db, **kwargs)

    retry_count = 0
    result: AgentResult = run_agent(db, scenario, situation_prompt, allowed_decisions, extra_context=trigger_event)
    logger.info(f"[{triggered_by}] sku={sku}: agent gathered {len(result.tool_call_trace)} pieces of evidence "
                f"-> decision={result.decision} confidence={result.confidence:.2f}")
    logger.info(f"[{triggered_by}] sku={sku}: reasoning: {result.reasoning}")
    val_result = _validate(result)
    logger.info(f"[{triggered_by}] sku={sku}: validation passed={val_result.get('passed')} checks={val_result.get('checks')}")

    while not val_result.get("passed", True) and retry_count < MAX_RETRIES:
        retry_count += 1
        logger.info(f"[{triggered_by}] sku={sku}: validation failed, retrying ({retry_count}/{MAX_RETRIES})")
        followup = (
            f"{situation_prompt}\n\nNOTE: a previous attempt at this decision was validated and FAILED "
            f"the following checks: {json.dumps(val_result.get('checks', []))}. "
            f"Re-investigate current state and correct course. Remember any prior proposal was NOT executed "
            f"(nothing is written until a human approves it), so you are free to propose something different."
        )
        result = run_agent(db, scenario, followup, allowed_decisions, extra_context=trigger_event)
        logger.info(f"[{triggered_by}] sku={sku}: retry -> decision={result.decision} confidence={result.confidence:.2f}")
        logger.info(f"[{triggered_by}] sku={sku}: retry reasoning: {result.reasoning}")
        val_result = _validate(result)
        logger.info(f"[{triggered_by}] sku={sku}: retry validation passed={val_result.get('passed')} checks={val_result.get('checks')}")

    if not val_result.get("passed", True):
        # Retries exhausted and still failing -- do not keep trying
        # autonomously, and force human approval (below). Deliberately does
        # NOT overwrite result.decision: "investigate" is Scenario 1's
        # vocabulary for "unresolved", but other scenarios use different
        # words (Scenario 2 uses "escalate") -- whatever the agent actually
        # decided stays, `requires_human_approval=True` is the real safety
        # mechanism here, not the decision label.
        result.action_summary += " [validation failed after retry; escalated for human review]"

    cost = _estimate_action_cost(result.action_result)
    remaining_budget = _remaining_budget(db, warehouse_id) if warehouse_id else 0.0
    needs_approval, reasons = policy.needs_human_approval(cost, remaining_budget, result.confidence)
    if not val_result.get("passed", True):
        needs_approval = True
        reasons.append("post-action validation failed")
    if result.decision in ("investigate", "escalate") or (result.action_result and result.action_result.get("escalated")):
        needs_approval = True
        reasons.append("agent decision requires human input")
    if result.action_result and result.action_result.get("success") and "cost" in result.action_result:
        # Any validated purchase-order proposal is paused for human review before
        # it is ever executed -- this is unconditional under the Proposal Pattern.
        needs_approval = True
        reasons.append("purchase order proposal requires human approval before execution")

    logger.info(f"[{triggered_by}] sku={sku}: final decision={result.decision} "
                f"requires_human_approval={needs_approval} reasons={reasons}")

    log = DecisionLog(
        run_id=result.run_id,
        scenario=scenario,
        sku=sku,
        recommendation_id=recommendation_id,
        triggered_by=triggered_by,
        trigger_event=json.dumps(trigger_event),
        tool_calls=json.dumps(result.tool_call_trace),
        decision=result.decision or "investigate",
        reasoning=result.reasoning,
        factors_considered=json.dumps(result.factors_considered),
        confidence=result.confidence,
        action_taken=json.dumps(result.action_result) if result.action_result else "none",
        executed=False,
        requires_human_approval=needs_approval,
        approval_status="pending" if needs_approval else "n/a",
        validation_result=json.dumps(val_result),
        validation_passed=val_result.get("passed"),
        retry_count=retry_count,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    log._approval_reasons = reasons  # attached for the response, not persisted
    return log


def apply_approval_decision(db: Session, log: DecisionLog, approve: bool, note: str) -> DecisionLog:
    if log.approval_status != "pending":
        logger.info(f"[approval] log={log.id} sku={log.sku}: ignored, already {log.approval_status}")
        return log

    logger.info(f"[approval] log={log.id} sku={log.sku}: human {'approved' if approve else 'rejected'}. note={note!r}")

    if approve:
        log.approval_status = "approved"
        proposal = json.loads(log.action_taken) if log.action_taken != "none" else None
        if proposal and proposal.get("success") and "cost" in proposal and not log.executed:
            executed = write_tools.execute_purchase_order(db, proposal)
            log.action_taken = json.dumps(executed)
            log.executed = bool(executed.get("success"))
            logger.info(f"[approval] log={log.id} sku={log.sku}: executed -> {executed}")
    else:
        # Nothing was ever written under the Proposal Pattern -- rejecting
        # just discards the proposal, there's nothing to roll back.
        log.approval_status = "rejected"
        logger.info(f"[approval] log={log.id} sku={log.sku}: rejected, no action was ever written (Proposal Pattern)")

    log.approval_note = note
    log.approved_at = datetime.utcnow()
    log.reasoning += f"\n\n[Human review: {'approved' if approve else 'rejected'}. {note}]".rstrip()
    db.commit()
    db.refresh(log)
    return log
