"""AgentCore Platform v1.0 - inner Salesforce Marketing Cloud workflow graph (Cat 2).

Instantiated by SalesforceWorkflowGraphNode.get_subgraph() in graph.py.
Inherits BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_salesforce_fields
          -> call_salesforce_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    salesforce - integration settings (base_url, ...) from config/config.yaml;
                 injected into State as the JSON `salesforce_config` field via
                 _extra_initial_state() so the no-arg nodes can read it
    runtime    - validated backbone settings (max_retry, timeout_s)

_extra_initial_state() also seeds the caller's input_context, which the
framework does not forward across the subgraph boundary - see
src/graph/context_bridge.py.

Nodes are registered WITHOUT constructor arguments (SDK nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.call_salesforce_api_node import CallSalesforceApiNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.confirm_node import ConfirmNode
from src.nodes.infer_salesforce_fields_node import InferSalesforceFieldsNode
from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import State, to_json


class SalesforceWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "salesforce_marketing_cloud_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the salesforce section is optional (the client
        # falls back to the documented default base_url and the network-free
        # simulator transport), and a missing/unusable setting is handled at
        # CallSalesforceApiNode.execute() as a graceful status=error rather
        # than a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_salesforce_fields"] = InferSalesforceFieldsNode()
        self._nodes["call_salesforce_api"] = CallSalesforceApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_salesforce_fields")
        self._sg.add_edge("infer_salesforce_fields", "call_salesforce_api")
        self._sg.add_edge("call_salesforce_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: AgentState) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        """Seed the inner initial state.

        Two hand-offs the framework does not perform on its own:

        * the `salesforce` settings section arriving under
          config["configurable"] from _parent_config() is injected as a JSON
          string (state values stay msgpack-safe) so the no-arg
          CallSalesforceApiNode can read it via state["salesforce_config"];
        * the caller's input_context, which GraphNode.execute() does not pass to
          subgraph.invoke() - the outer node stashes it in a ContextVar and this
          hook reads it back (src/graph/context_bridge.py). Without it every
          inner read of state["input_context"] would see {}.
        """
        extra: "dict[str, Any]" = {"input_context": get_caller_input_context()}
        configurable = self.config.get("configurable") or {}
        salesforce = configurable.get("salesforce") or {}
        if salesforce:
            extra["salesforce_config"] = to_json(salesforce)
        return extra

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            # Carried explicitly: the boundary only moves the keys named here.
            "error_code": state.get("error_code"),
            "intent": state.get("intent", ""),
            "campaign_id": state.get("campaign_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "campaign_name": state.get("campaign_name", ""),
            "confirmation": state.get("confirmation", ""),
            "salesforce_payload": state.get("salesforce_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
