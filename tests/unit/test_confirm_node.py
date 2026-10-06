# CMN-C2-232 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (the framework's __call__ routes trust gate ->
# input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "1001",
        "record_ref": "sfmc://campaigns/1001",
        "campaign_name": "Campaign 1001",
        "intent": "lookup_campaign",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved marketing campaign" in result["confirmation"]
        assert "Campaign 1001" in result["confirmation"]
        assert "ref=sfmc://campaigns/1001" in result["confirmation"]
        assert "id=1001" in result["confirmation"]
        assert result["result"]["record_id"] == "1001"
        assert result["result"]["record_ref"] == "sfmc://campaigns/1001"

    def test_create_verb(self):
        result = self.node(
            _state(
                intent="create_campaign",
                record_id="summer26",
                record_ref="sfmc://campaigns/summer26",
                campaign_name="summer launch 2026",
            )
        )
        assert "Created marketing campaign" in result["confirmation"]

    def test_update_verb(self):
        result = self.node(_state(intent="update_campaign"))
        assert "Updated marketing campaign" in result["confirmation"]

    def test_trigger_send_verb(self):
        result = self.node(_state(intent="trigger_send", record_ref="sfmc://triggered-sends/welcome-01"))
        assert "Triggered email send" in result["confirmation"]
        assert "ref=sfmc://triggered-sends/welcome-01" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed marketing request" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=1001" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_name_missing(self):
        result = self.node(_state(campaign_name=""))
        assert "'1001'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
