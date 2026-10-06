# CMN-C2-232 - Unit tests: ValidateInputNode (inner Step 1, flag-and-redact)
#
# Canon: nodes are invoked via node(state) - through the framework's __call__
# (trust gate -> input gate -> execute -> output gate) - never bare
# node.execute(state). This inner domain node declares ANONYMOUS, so the state
# builder sets caller_trust_level = TrustLevel.ANONYMOUS.value.
#
# Two redaction layers are exercised here:
#   * the FRAMEWORK input gate in __call__ rewrites emails (any '@') in
#     validated_input to "[MASKED]" BEFORE execute() sees the text - the
#     intentional-PII test asserts that [MASKED] path;
#   * the NODE's own deterministic scan handles token-shaped strings the
#     framework mask does not cover (secret_* / sk-* / eyJ*) - flag + [REDACTED].

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "validated_input": "Look up the marketing campaign with campaign id 1001.",
        "input_context": {},
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "validate-input-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode()

    def test_success_plain_text(self):
        result = self.node(_state(validated_input="Show the campaign summary for the flagged request"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "Show the campaign summary for the flagged request"
        assert from_json(result["redaction_flags"], None) == []

    def test_success_serialized_json_input(self):
        """The outer node serializes the request text into a JSON envelope."""
        payload = json.dumps({"text": "summarize the campaign on file"})
        result = self.node(_state(validated_input=payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "summarize the campaign on file"

    def test_caller_data_reaches_the_node_through_input_context(self):
        """Structured caller data travels on the input_context channel, which
        the main-slot wrapper bridges into this graph."""
        result = self.node(_state(input_context={"campaign_id": "summer26", "subscriber_key": "sub-1001"}))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_hint"] == "summer26"
        assert result["subscriber_key"] == "sub-1001"

    def test_malformed_caller_data_fails_closed_here_too(self):
        """The inner graph is independently invocable, so it re-applies the
        contract instead of trusting an upstream validation."""
        result = self.node(_state(input_context={"campaign_id": "not a code!"}))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")
        assert any("input_context.campaign_id" in e for e in result["error_log"])
        # The rejected VALUE is never echoed back.
        assert not any("not a code!" in e for e in result["error_log"])

    def test_empty_input_errors(self):
        result = self.node(_state(validated_input="  "))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")
        assert result["error_log"]

    def test_short_input_errors(self):
        result = self.node(_state(validated_input="ab"))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")

    def test_framework_gate_masks_email_before_execute(self):
        """Intentional-PII path: the framework input gate in __call__ rewrites
        the email to [MASKED] before execute() runs, so no raw address survives."""
        result = self.node(
            _state(validated_input="send the campaign report for campaign code summer26 to mkt.lead@example.com")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "mkt.lead@example.com" not in result["validated_input"]
        assert "[MASKED]" in result["validated_input"]

    def test_node_redacts_token_shaped_string(self):
        """The node's own deterministic scan covers token shapes the framework
        PII mask does not (secret_*): flagged + [REDACTED] before logging."""
        text = "integration key secret_abcdef123456 for campaign code summer26"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "secret_abcdef123456" not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_audit_emits_scan_outcome_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.validate_input_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state(validated_input="summarize the campaign with campaign code summer26"))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - flags only, never the text.
        assert payloads["validate_input_complete"]["redaction_flags"] == []
        assert "text" not in payloads["validate_input_complete"]
