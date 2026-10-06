# CMN-C2-232 - The invoke envelope is fail-closed for EVERY representation.
#
# SalesforceMarketingCloudAgent.get_output() extends the framework envelope with
# the structured domain product. The framework's own envelope falls back to the
# raw `result` written by the inner workflow when no `formatted_output` exists -
# so a run that produced an inner result but never cleared the output gate (the
# output gate raising, for instance) would otherwise hand the caller exactly the
# pre-gate content the gate exists to withhold.
#
# These tests drive get_output() directly with hand-built terminal states, which
# is the only way to reach the states a compiled run cannot be coaxed into.

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import SalesforceMarketingCloudAgent

_GATED_OUTPUT = {
    "record_id": "1001",
    "record_ref": "sfmc://campaigns/1001",
    "campaign_id": "1001",
    "campaign_name": "Summer Launch 2026",
    "intent": "lookup_campaign",
    "confirmation": "Retrieved marketing campaign 'Summer Launch 2026' - id=1001",
    "salesforce_payload": {"campaign_id": "1001"},
}


def _agent() -> SalesforceMarketingCloudAgent:
    return SalesforceMarketingCloudAgent()


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "formatted_output": dict(_GATED_OUTPUT),
        "trace_id": "tr",
        "correlation_id": "co",
        "node_history": ["InitializeNode", "FinalizeNode"],
        "error_log": [],
    }
    state.update(overrides)
    return state


class TestSuccessEnvelope:
    def test_structured_keys_come_from_the_gated_output(self):
        out = _agent().get_output(_state())
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["output"] == _GATED_OUTPUT
        assert out["formatted_output"] == _GATED_OUTPUT
        assert out["record_id"] == "1001"
        assert out["record_ref"] == "sfmc://campaigns/1001"
        assert out["campaign_name"] == "Summer Launch 2026"
        assert out["intent"] == "lookup_campaign"
        # The base envelope is extended, never replaced.
        assert out["trace_id"] == "tr" and out["node_history"]

    def test_no_error_log_on_success(self):
        assert "error_log" not in _agent().get_output(_state())


class TestFailClosedEnvelope:
    def test_pre_gate_result_is_never_surfaced_when_the_gate_left_no_output(self):
        """`result` holds the inner workflow's own product, which never passed
        the output gate. With no formatted_output, it must not become `output`."""
        state = _state(
            status=AgentStatus.ERROR.value,
            result={"confirmation": "pre-gate text", "record_id": "1001"},
            error_log=["PostProcessNode: output gate raised"],
        )
        del state["formatted_output"]
        out = _agent().get_output(state)
        assert out["output"] is None
        assert "pre-gate text" not in str(out)

    def test_structured_keys_are_withheld_on_a_non_success_status(self):
        out = _agent().get_output(_state(status=AgentStatus.ERROR.value))
        for key in ("record_id", "record_ref", "campaign_id", "campaign_name", "intent", "confirmation"):
            assert key not in out
        assert "formatted_output" not in out

    def test_failure_reports_a_closed_set_reason_only(self):
        """The caller-visible error is the constant reason code; the internal
        entry is not projected in any form - not its message line either."""
        state = _state(
            status=AgentStatus.ERROR.value,
            error_log=['[Node] failed: boom\nTraceback (most recent call last):\n  File "/abs/x.py", line 1'],
        )
        del state["formatted_output"]
        out = _agent().get_output(state)
        assert out["error"] == {"reason": "workflow_failed"}
        assert "error_log" not in out
        assert "boom" not in str(out)
        assert "Traceback" not in str(out)

    def test_a_non_dict_formatted_output_is_not_trusted(self):
        out = _agent().get_output(_state(formatted_output="a string, not the gated dict"))
        assert out["output"] is None
        assert "formatted_output" not in out
