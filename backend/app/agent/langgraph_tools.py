"""
Wraps the read-only tool functions (app/tools/read_tools.py) as LangChain
StructuredTools bound to one request's DB session, for use by the
"investigate" node in agent/graph.py. Only read tools are exposed here --
write/action tools are dispatched separately by the "act" node, based on
what the DSPy decide node chooses (see agent/graph.py and agent/prompts.py).

Argument JSON-schemas are reused from app/tools/schema.py's TOOL_SCHEMAS
rather than re-declared, so the tool-calling contract has one source of
truth across the dspy_tools.py (legacy) and langgraph_tools.py paths.
"""
from typing import Any
from pydantic import create_model
from sqlalchemy.orm import Session
from langchain_core.tools import StructuredTool
from app.tools import read_tools
from app.tools.schema import TOOL_SCHEMAS

_READ_FNS = {
    "get_product": read_tools.get_product,
    "get_inventory_status": read_tools.get_inventory_status,
    "get_demand_forecast": read_tools.get_demand_forecast,
    "get_open_pos": read_tools.get_open_pos,
    "get_supplier_catalog": read_tools.get_supplier_catalog,
    "get_warehouse": read_tools.get_warehouse,
    "get_po_status": read_tools.get_po_status,
    "get_alternate_suppliers": read_tools.get_alternate_suppliers,
}
_SCHEMA_BY_NAME = {s["function"]["name"]: s["function"] for s in TOOL_SCHEMAS}

_JSON_TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict}


def _args_schema(name: str, schema: dict):
    properties = schema["parameters"]["properties"]
    required = set(schema["parameters"].get("required", []))
    fields = {}
    for arg_name, prop in properties.items():
        py_type = _JSON_TYPE_MAP.get(prop.get("type"), Any)
        default = ... if arg_name in required else None
        fields[arg_name] = (py_type if arg_name in required else py_type | None, default)
    return create_model(f"{name}_Args", **fields)


def build_read_tools(db: Session, trace: list[dict]) -> list[StructuredTool]:
    """Return StructuredTools bound to `db` for this one agent run. Each
    call appends its args/result to `trace` (the same tool_call_trace shape
    the rest of the app -- API response, frontend trace viewer -- expects)."""
    tools = []
    for name, fn in _READ_FNS.items():
        schema = _SCHEMA_BY_NAME[name]

        def make_wrapper(fn=fn, name=name):
            def wrapper(**kwargs):
                result = fn(db, **kwargs)
                trace.append({
                    "tool": name, "args": kwargs,
                    "result": {"ok": True, "is_write": False, "result": result},
                })
                return result
            return wrapper

        tools.append(StructuredTool.from_function(
            func=make_wrapper(),
            name=name,
            description=schema["description"],
            args_schema=_args_schema(name, schema),
        ))
    return tools
