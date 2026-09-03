"""
The agent entry point: takes a triggering event and runs it through the
LangGraph orchestrator (agent/graph.py), which loops a read-only
"investigate" step against tools, hands off to a DSPy "decide" step for the
structured decision, and runs an "act" step to execute the chosen action
(retrying the decision once if the action is refused for a policy
violation). See agent/graph.py's module docstring for the full graph shape
and why LangGraph vs. DSPy split the way they do.

If GOOGLE_API_KEY is not configured (or USE_STUB_LLM=true), a deterministic
StubAgent is used instead so the rest of the system (API, validation loop,
eval harness) can be exercised without hitting a real LLM. The stub follows
the same "gather evidence via tools, then decide" shape but with simple
hand-written rules rather than genuine reasoning -- it exists for offline
testing, not as the intended production path.
"""
import uuid
from sqlalchemy.orm import Session
from app.config import settings
from app.agent import stub


class AgentResult:
    def __init__(self):
        self.run_id = str(uuid.uuid4())[:8]
        self.tool_call_trace: list[dict] = []
        self.decision: str | None = None
        self.reasoning: str = ""
        self.factors_considered: list[str] = []
        self.confidence: float = 0.0
        self.action_summary: str = "none"
        self.action_result: dict | None = None  # the write tool's result, if any
        self.error: str | None = None

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "tool_call_trace": self.tool_call_trace,
            "decision": self.decision,
            "reasoning": self.reasoning,
            "factors_considered": self.factors_considered,
            "confidence": self.confidence,
            "action_summary": self.action_summary,
            "action_result": self.action_result,
            "error": self.error,
        }


def run_agent(db: Session, scenario: str, situation_prompt: str, allowed_decisions: list[str],
              extra_context: dict | None = None) -> AgentResult:
    if settings.use_stub_llm or not settings.google_api_key:
        return stub.run_stub_agent(db, scenario, extra_context or {})
    return _run_graph_agent(db, situation_prompt, allowed_decisions)


def _fallback_decision(allowed_decisions: list[str]) -> str:
    """Picks a scenario-appropriate "give up, a human needs to look at this"
    label. Different scenarios spell this differently ("investigate" for
    Scenario 1, "escalate" for Scenario 2) -- hardcoding one would emit a
    decision string outside the calling scenario's own vocabulary."""
    for candidate in ("escalate", "investigate"):
        if candidate in allowed_decisions:
            return candidate
    return allowed_decisions[0] if allowed_decisions else "escalate"


def _run_graph_agent(db: Session, situation_prompt: str, allowed_decisions: list[str]) -> AgentResult:
    from app.agent.graph import run_graph_agent

    result = AgentResult()
    try:
        trace, final_state = run_graph_agent(db, situation_prompt, allowed_decisions)
    except Exception as exc:
        result.error = f"Agent run failed: {exc}"
        result.decision = _fallback_decision(allowed_decisions)
        result.reasoning = f"Agent run raised an error; escalating for safety. Error: {exc}"
        result.confidence = 0.0
        return result

    result.tool_call_trace = trace
    result.decision = final_state.get("decision") or _fallback_decision(allowed_decisions)
    result.reasoning = final_state.get("reasoning", "")
    result.factors_considered = final_state.get("factors_considered", [])
    result.confidence = final_state.get("confidence", 0.0)
    result.action_summary = final_state.get("action_summary", "none")

    for call in trace:
        tool_output = call["result"]
        if tool_output.get("is_write") and tool_output["result"].get("success"):
            result.action_result = tool_output["result"]

    return result
