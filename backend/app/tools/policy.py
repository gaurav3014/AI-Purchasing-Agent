"""
Hard guardrails enforced in code, not left to the LLM's judgment.

These are checked before any write tool commits a change. The agent can
still *propose* something that violates a rule (e.g. it might reason its
way to "let's order below MOQ"), but the write tool itself will refuse and
return a structured error the agent must react to. This is what keeps the
system from depending on the LLM to correctly remember and apply every
constraint on every call.

Thresholds below are deliberately simple and centralized here so they're
easy to point to and change during the eval discussion.
"""
from dataclasses import dataclass


# Any single action whose cost exceeds this fraction of *remaining* budget
# requires human approval before executing, even if it technically fits.
BUDGET_APPROVAL_HEADROOM_FRACTION = 0.5

# Any action the agent proposes with confidence below this requires human
# approval regardless of what the action is.
MIN_AUTO_APPROVE_CONFIDENCE = 0.7

# Absolute spend above which an action always requires human approval,
# regardless of remaining budget headroom (a "large dollar amount" guardrail).
LARGE_SPEND_APPROVAL_THRESHOLD = 5000.0


@dataclass
class PolicyViolation:
    code: str
    message: str


def check_moq(qty: int, moq: int) -> PolicyViolation | None:
    if qty <= 0:
        return None
    if qty < moq:
        return PolicyViolation(
            "below_moq",
            f"Requested quantity {qty} is below the supplier's minimum order quantity of {moq}.",
        )
    return None


def check_budget(cost: float, allocated: float, spent: float) -> PolicyViolation | None:
    remaining = allocated - spent
    if cost > remaining:
        return PolicyViolation(
            "over_budget",
            f"Action costs {cost:.2f} but only {remaining:.2f} remains in the budget "
            f"(allocated {allocated:.2f}, already spent {spent:.2f}).",
        )
    return None


def check_storage(added_units: int, capacity: int, used: int) -> PolicyViolation | None:
    remaining = capacity - used
    if added_units > remaining:
        return PolicyViolation(
            "over_storage_capacity",
            f"Action adds {added_units} units but only {remaining} units of storage remain "
            f"(capacity {capacity}, already used {used}).",
        )
    return None


def needs_human_approval(cost: float, remaining_budget: float, confidence: float) -> tuple[bool, list[str]]:
    reasons = []
    if confidence < MIN_AUTO_APPROVE_CONFIDENCE:
        reasons.append(f"agent confidence {confidence:.2f} is below the auto-approve threshold {MIN_AUTO_APPROVE_CONFIDENCE}")
    if cost > LARGE_SPEND_APPROVAL_THRESHOLD:
        reasons.append(f"action cost {cost:.2f} exceeds the large-spend threshold {LARGE_SPEND_APPROVAL_THRESHOLD}")
    if remaining_budget > 0 and cost > remaining_budget * BUDGET_APPROVAL_HEADROOM_FRACTION:
        reasons.append(
            f"action cost {cost:.2f} exceeds {BUDGET_APPROVAL_HEADROOM_FRACTION * 100:.0f}% "
            f"of remaining budget headroom ({remaining_budget:.2f})"
        )
    return (len(reasons) > 0, reasons)
