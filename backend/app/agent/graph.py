"""
LangGraph orchestrator for the purchasing agent.

LangGraph owns control flow: looping the "investigate" node against
read-only tools until enough evidence is gathered (or a turn cap is hit),
handing off to the DSPy "decide" node for the structured decision, running
the "act" node to attempt the chosen action -- per the Proposal Pattern
(tools/write_tools.py), this only ever *validates and proposes* a purchase
order, it never writes one -- and, if the proposal is refused for violating
a hard policy (MOQ/budget/storage), routing back to "decide" once with the
refusal folded into the evidence so the model proposes something compliant
instead of blindly retrying. A proposal that passes guardrails still isn't
written to purchase_orders; it waits in decision_log for a human to
approve, at which point api/orchestrator.py executes it as one transaction.

DSPy owns reasoning: the "decide" node is a dspy.ChainOfThought over the
PurchasingDecision signature (agent/prompts.py). It has no awareness of the
graph around it -- it just maps (situation, evidence, allowed_decisions,
available_actions) -> a structured decision. That's what makes it a target
for offline optimization (dspy teleprompters) independent of orchestration.

    investigate <--> tools           (evidence-gathering loop, capped)
         |
         v
       decide  <---------------+
         |                     |
         v                     |
        act  ---(policy violation, retries left)---+
         |
         v (else)
        END
"""
import json
from typing import Annotated, Any, TypedDict

import dspy
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.config import settings
from app.agent.langgraph_tools import build_read_tools
from app.agent.prompts import PurchasingDecision
from app.tools import write_tools

MAX_INVESTIGATE_TURNS = 6
MAX_ACT_ATTEMPTS = 2

INVESTIGATE_INSTRUCTIONS = """You are the investigation step of an AI purchasing agent. Given a
purchasing situation, call read-only tools to gather the evidence a buyer would need before
deciding what to do -- at minimum inventory status, demand forecast, and open purchase orders,
plus whichever of the warehouse (budget/storage) and supplier catalog are relevant to this
situation. Do not guess at numbers you haven't looked up. Once you have enough evidence to
support a well-reasoned decision, stop calling tools (respond with no further tool calls) so the
decision step can take over."""

_WRITE_FNS = {
    "propose_purchase_order": write_tools.propose_purchase_order,
    "escalate_to_human": write_tools.escalate_to_human,
}


class GraphState(TypedDict):
    messages: Annotated[list, add_messages]
    turns: int
    act_attempts: int
    decision: str
    reasoning: str
    factors_considered: list[str]
    confidence: float
    action_summary: str
    action_tool: str
    action_args: dict[str, Any]


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_graph_agent(db, situation_prompt: str, allowed_decisions: list[str]) -> tuple[list[dict], dict]:
    """Runs the graph for one situation. Returns (tool_call_trace, final_state)."""
    trace: list[dict] = []
    read_tools = build_read_tools(db, trace)
    read_tools_by_name = {t.name: t for t in read_tools}
    chat_llm = ChatGoogleGenerativeAI(
        model=settings.google_model, google_api_key=settings.google_api_key, temperature=0,
    ).bind_tools(read_tools)

    decide_lm = dspy.LM(f"gemini/{settings.google_model}", api_key=settings.google_api_key)
    decide_module = dspy.ChainOfThought(PurchasingDecision)

    def investigate(state: GraphState):
        response = chat_llm.invoke(state["messages"])
        return {"messages": [response], "turns": state["turns"] + 1}

    def run_tools(state: GraphState):
        last = state["messages"][-1]
        tool_messages = []
        for call in last.tool_calls:
            tool = read_tools_by_name.get(call["name"])
            content = tool.invoke(call["args"]) if tool else {"error": f"unknown tool {call['name']}"}
            tool_messages.append(ToolMessage(content=json.dumps(content, default=str), tool_call_id=call["id"]))
        return {"messages": tool_messages}

    def route_after_investigate(state: GraphState):
        if state["turns"] >= MAX_INVESTIGATE_TURNS:
            return "decide"
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return "decide"

    def decide(state: GraphState):
        with dspy.context(lm=decide_lm):
            pred = decide_module(
                situation=situation_prompt,
                evidence=json.dumps(trace, default=str),
                allowed_decisions=", ".join(allowed_decisions),
                available_actions=", ".join(list(_WRITE_FNS.keys()) + ["none"]),
            )
        return {
            "decision": pred.decision,
            "reasoning": pred.reasoning,
            "factors_considered": list(pred.factors_considered or []),
            "confidence": _safe_float(pred.confidence),
            "action_summary": pred.action_summary or "none",
            "action_tool": (pred.action_tool or "none").strip(),
            "action_args": dict(pred.action_args or {}),
        }

    def act(state: GraphState):
        tool_name = state.get("action_tool", "none")
        attempts = state.get("act_attempts", 0) + 1
        if tool_name not in _WRITE_FNS:
            return {"act_attempts": attempts}
        args = state.get("action_args") or {}
        try:
            result = _WRITE_FNS[tool_name](db, **args)
        except TypeError as exc:
            result = {"success": False, "error": f"invalid arguments for {tool_name}: {exc}"}
        trace.append({
            "tool": tool_name, "args": args,
            "result": {"ok": True, "is_write": True, "result": result},
        })
        return {"act_attempts": attempts}

    def route_after_act(state: GraphState):
        if not trace or not trace[-1]["result"].get("is_write"):
            return END
        last_result = trace[-1]["result"]["result"]
        refused = last_result.get("success") is False and "policy_violations" in last_result
        if refused and state.get("act_attempts", 0) < MAX_ACT_ATTEMPTS:
            return "decide"
        return END

    graph = StateGraph(GraphState)
    graph.add_node("investigate", investigate)
    graph.add_node("tools", run_tools)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.set_entry_point("investigate")
    graph.add_conditional_edges("investigate", route_after_investigate, {"tools": "tools", "decide": "decide"})
    graph.add_edge("tools", "investigate")
    graph.add_edge("decide", "act")
    graph.add_conditional_edges("act", route_after_act, {"decide": "decide", END: END})
    compiled = graph.compile()

    initial_state: GraphState = {
        "messages": [
            SystemMessage(content=INVESTIGATE_INSTRUCTIONS),
            HumanMessage(content=situation_prompt),
        ],
        "turns": 0,
        "act_attempts": 0,
        "decision": "", "reasoning": "", "factors_considered": [],
        "confidence": 0.0, "action_summary": "none", "action_tool": "none", "action_args": {},
    }
    final_state = compiled.invoke(initial_state, config={"recursion_limit": 50})
    return trace, final_state
