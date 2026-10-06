# CMN-C2-232 - Unit tests: inner SalesforceWorkflowGraph (BaseGraph) contract.
#
# The compiled outer path is exercised end-to-end by
# tests/proof_of_boundary/; this module unit-checks the inner graph's identity,
# config forwarding, routing, output contract, and a direct inner invoke on the
# network-free simulator.

import pytest

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import set_caller_input_context
from src.graph.domain_workflow_graph import SalesforceWorkflowGraph
from src.schemas.state import State, from_json


@pytest.fixture(autouse=True)
def _clear_bridge():
    """The context bridge is a ContextVar: clear it so one test's caller data
    cannot leak into the next."""
    set_caller_input_context(None)
    yield
    set_caller_input_context(None)


def _graph(config=None):
    return SalesforceWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "salesforce_marketing_cloud_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_salesforce_config_as_json():
    g = _graph({"configurable": {"salesforce": {"base_url": "https://sub.rest.marketingcloudapis.example"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict (checkpoint-safe).
    assert isinstance(extra["salesforce_config"], str)
    assert from_json(extra["salesforce_config"], {}) == {"base_url": "https://sub.rest.marketingcloudapis.example"}


def test_extra_initial_state_without_salesforce_section_seeds_only_the_bridge():
    extra = _graph()._extra_initial_state()
    assert extra == {"input_context": {}}


def test_extra_initial_state_seeds_the_bridged_caller_context():
    """The framework does not forward input_context across the subgraph
    boundary; the outer wrapper stashes it and this hook seeds it back."""
    set_caller_input_context({"campaign_id": "summer26"})
    assert _graph()._extra_initial_state()["input_context"] == {"campaign_id": "summer26"}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "1001", "record_ref": "sfmc://campaigns/1001", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_campaign",
            "campaign_id": "1001",
            "record_id": "1001",
            "record_ref": "sfmc://campaigns/1001",
            "campaign_name": "Campaign 1001",
            "confirmation": "ok",
            "salesforce_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_campaign"
    assert out["record_ref"] == "sfmc://campaigns/1001"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "1001", "record_ref": "sfmc://campaigns/1001", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_the_simulator():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node declares
    ANONYMOUS): validate -> classify -> infer -> call -> confirm."""
    g = _graph({"configurable": {"salesforce": {"base_url": "https://mc.rest.marketingcloudapis.com"}}})
    g.compile()
    result = g.invoke(user_input="Look up the marketing campaign with campaign id 1001 and summarize it.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "1001"
    assert result["record_ref"] == "sfmc://campaigns/1001"
    assert result["intent"] == "lookup_campaign"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferSalesforceFieldsNode",
        "CallSalesforceApiNode",
        "ConfirmNode",
    ]
