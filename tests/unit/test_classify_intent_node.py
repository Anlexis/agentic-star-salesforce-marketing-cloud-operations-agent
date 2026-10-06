# CMN-C2-232 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_campaign / create_campaign / update_campaign / trigger_send
# (deterministic keyword heuristic, v1 - no LLM; unknown falls back to the
# read-only lookup). Priority is send-first (highest-impact side effect), then
# the writes, then lookup.
#
# Canon: invoked via node(state) (the framework's __call__ routes trust gate ->
# input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_campaign(self):
        result = self.node(_state("Look up the marketing campaign with campaign id 1001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_campaign"

    def test_keyword_create_campaign(self):
        result = self.node(_state("Create a marketing campaign for the autumn product line"))
        assert result["intent"] == "create_campaign"

    def test_keyword_update_campaign(self):
        result = self.node(_state("Update the campaign description for campaign code summer26"))
        assert result["intent"] == "update_campaign"

    def test_keyword_trigger_send(self):
        result = self.node(_state("Trigger the welcome send for subscriber key sub-1001"))
        assert result["intent"] == "trigger_send"

    def test_send_keyword_wins_over_write(self):
        # Priority order is send-first (the highest-impact side effect): a
        # "create the campaign and send it" style request classifies as the send.
        result = self.node(_state("Create the campaign and send it to the spring segment"))
        assert result["intent"] == "trigger_send"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_campaign"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_campaign" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the marketing campaign with campaign id 1001."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_campaign"
        assert payloads["classify_intent_complete"]["defaulted"] is False
