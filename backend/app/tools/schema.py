"""
Tool JSON-schemas (argument descriptions) + a dispatcher that maps a tool
call name to the actual Python function in read_tools.py / write_tools.py.
This is the single source of truth for tool argument shapes -- both the
stub agent (via dispatch_tool_call) and the LangGraph tool wrappers
(agent/langgraph_tools.py) build on top of it.
"""
from app.tools import read_tools, write_tools

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_product", "description": "Get basic product info (name, category, unit cost, unit volume) for a SKU.",
        "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "get_inventory_status", "description": "Get current on-hand inventory and safety stock for a SKU, optionally at a specific warehouse.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "warehouse_id": {"type": "string", "description": "optional"},
        }, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "get_demand_forecast",
        "description": "Get demand forecast(s) for a SKU, including forecast quantity, actual sales to date within "
                        "the current period, and an implied run-rate if actuals are tracking ahead of or behind plan.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "warehouse_id": {"type": "string", "description": "optional"},
        }, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "get_open_pos", "description": "Get all non-cancelled purchase orders for a SKU, with ordered/confirmed quantities, status, and expected delivery.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "warehouse_id": {"type": "string", "description": "optional"},
        }, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "get_supplier_catalog",
        "description": "Get lead time, minimum order quantity (MOQ), unit cost, and reliability for supplier(s) of a SKU.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "supplier_id": {"type": "string", "description": "optional, to look up one supplier"},
        }, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "get_warehouse",
        "description": "Get available budget, max storage, and current storage used for a warehouse.",
        "parameters": {"type": "object", "properties": {
            "warehouse_id": {"type": "string"},
        }, "required": ["warehouse_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_po_status",
        "description": "Get the full current status of one specific purchase order by ID (ordered/confirmed qty, supplier, dates).",
        "parameters": {"type": "object", "properties": {
            "po_id": {"type": "string"},
        }, "required": ["po_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_alternate_suppliers",
        "description": "Get suppliers for a SKU other than the one specified -- use this when a primary supplier "
                        "falls short (Scenario 2) to see if another supplier could cover the gap, and compare "
                        "their lead time, MOQ, cost, and reliability.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "exclude_supplier_id": {"type": "string", "description": "optional, usually the supplier that fell short"},
        }, "required": ["sku"]},
    }},

    # ---- write / action tools ----
    {"type": "function", "function": {
        "name": "propose_purchase_order",
        "description": "Propose a new purchase order (does NOT create it). Will be REFUSED if it violates MOQ, "
                        "budget, or storage constraints -- there is no override. If refused, propose a smaller/"
                        "different quantity or supplier, or escalate_to_human. A proposal that passes is not "
                        "executed until a human approves it.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "warehouse_id": {"type": "string"}, "supplier_id": {"type": "string"},
            "qty": {"type": "integer"},
        }, "required": ["sku", "warehouse_id", "supplier_id", "qty"]},
    }},
    {"type": "function", "function": {
        "name": "escalate_to_human",
        "description": "Escalate this situation to a human buyer instead of proposing an autonomous action. Use "
                        "this when information is missing, conflicting, or the decision has consequences you are "
                        "not confident enough to own.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"}, "context": {"type": "object"},
        }, "required": ["reason"]},
    }},
]


def dispatch_tool_call(db, name: str, args: dict) -> dict:
    read_fns = {
        "get_product": read_tools.get_product,
        "get_inventory_status": read_tools.get_inventory_status,
        "get_demand_forecast": read_tools.get_demand_forecast,
        "get_open_pos": read_tools.get_open_pos,
        "get_supplier_catalog": read_tools.get_supplier_catalog,
        "get_warehouse": read_tools.get_warehouse,
        "get_po_status": read_tools.get_po_status,
        "get_alternate_suppliers": read_tools.get_alternate_suppliers,
    }
    write_fns = {
        "propose_purchase_order": write_tools.propose_purchase_order,
        "escalate_to_human": write_tools.escalate_to_human,
    }
    if name in read_fns:
        return {"ok": True, "is_write": False, "result": read_fns[name](db, **args)}
    if name in write_fns:
        return {"ok": True, "is_write": True, "result": write_fns[name](db, **args)}
    return {"ok": False, "is_write": False, "result": {"error": f"unknown tool {name}"}}
