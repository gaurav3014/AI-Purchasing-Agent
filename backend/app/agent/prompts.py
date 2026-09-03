"""
DSPy signature for the purchasing agent's decision-making step.

Orchestration (looping over tools to gather evidence, retrying an action
that gets refused, routing to the next step) is LangGraph's job -- see
agent/graph.py. This module only defines the *reasoning* contract used by
the "decide" node inside that graph: given a situation and the evidence
already gathered, produce a structured decision plus (optionally) one
action to hand to the "act" node. Because it's a plain dspy.Signature, the
decide node (a dspy.ChainOfThought over this signature) can later be
compiled/optimized with a DSPy teleprompter against labeled examples,
independent of how the surrounding graph is wired.
"""
from typing import Any, Literal
import dspy


class PurchasingDecision(dspy.Signature):
    """You are an AI purchasing agent assisting a buyer at a retail /
    quick-commerce company. You are given a purchasing situation (a
    triggering event) and the evidence already gathered about it (inventory,
    demand forecast, open purchase orders, supplier terms, budget, storage,
    etc. -- whatever was fetched by an upstream investigation step).

    Reason about ALL relevant constraints together (inventory coverage,
    demand, lead time, MOQ, budget, storage). A recommendation or the
    "obvious" fix is not necessarily correct -- treat it as a hypothesis to
    test against the evidence, not a given. If the evidence is missing or
    conflicting in a way that blocks a confident decision, say so honestly
    via low confidence rather than guessing.

    If an action is warranted, choose AT MOST ONE action tool from
    available_actions and supply the exact arguments it needs. You cannot
    write to the purchase order system directly -- propose_purchase_order
    only *proposes* an order (it is refused outright if it violates MOQ,
    budget, or storage; there is no override) and, if accepted, still waits
    for a human to approve it before anything is actually created. Prefer
    the smallest quantity that resolves the situation. If a previous
    attempt is present in the evidence and it was refused, do not repeat
    the same call -- either propose a smaller/different quantity that
    respects the constraint, or choose escalate_to_human if no compliant
    proposal resolves the situation and it is genuinely urgent.
    """

    situation: str = dspy.InputField(
        desc="The purchasing situation / triggering event to decide on."
    )
    evidence: str = dspy.InputField(
        desc="JSON-serialized trace of tool calls made so far (read-only "
             "investigation results, and any prior action attempt with its "
             "outcome or policy violations)."
    )
    allowed_decisions: str = dspy.InputField(
        desc="Comma-separated list of the only decision types valid for this situation."
    )
    available_actions: str = dspy.InputField(
        desc="Comma-separated list of action tools you may choose from, or 'none'."
    )

    decision: str = dspy.OutputField(
        desc="Exactly one value from allowed_decisions."
    )
    reasoning: str = dspy.OutputField(
        desc="Clear explanation of why, referencing the evidence gathered."
    )
    factors_considered: list[str] = dspy.OutputField(
        desc="Short list of the specific factors that drove the decision, e.g. "
             "'existing inventory + open PO already exceed 30-day demand'."
    )
    confidence: float = dspy.OutputField(
        desc="0.0-1.0, honest confidence in this decision."
    )
    action_summary: str = dspy.OutputField(
        desc="Plain-language summary of any action taken (or 'none')."
    )
    action_tool: Literal[
        "propose_purchase_order", "escalate_to_human", "none",
    ] = dspy.OutputField(desc="one value from available_actions, or 'none' if no action is warranted")
    action_args: dict[str, Any] = dspy.OutputField(
        desc="Keyword arguments for action_tool matching its required parameters "
             "(sku, warehouse_id, supplier_id, qty for propose_purchase_order; reason, context for "
             "escalate_to_human), or {} if action_tool is 'none'."
    )
