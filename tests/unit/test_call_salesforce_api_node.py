# CMN-C2-232 - Unit tests: CallSalesforceApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (the framework's __call__ routes trust gate ->
# input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, the trust gate is unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's SfmcClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_salesforce_api_node import CallSalesforceApiNode
from src.services.sfmc_client import SfmcApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_salesforce_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "salesforce_payload": to_json({"campaign_id": "1001"}),
        "intent": "lookup_campaign",
        "campaign_id": "1001",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-sfmc-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for SfmcClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_campaign(self, campaign_id, api_token):
        raise SfmcApiError(403, "forbidden by integration permissions")


class _FakeLiveClient:
    """Stands in for SfmcClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def find_campaign(self, campaign_id, api_token):
        _FakeLiveClient.captured = {"campaign_id": campaign_id, "api_token": api_token}
        return {"id": campaign_id, "name": f"Campaign {campaign_id}", "campaignCode": campaign_id}


class TestCallSalesforceApiNode:
    def setup_method(self):
        self.node = CallSalesforceApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"
        assert result["record_ref"] == "sfmc://campaigns/1001"
        assert result["campaign_id"] == "1001"
        assert result["campaign_name"] == "Campaign 1001"

    def test_create_success_via_default_v1_stub(self):
        state = _state(
            intent="create_campaign",
            campaign_id="summer26",
            salesforce_payload=to_json({"name": "summer launch 2026", "campaignCode": "summer26"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "summer26"
        assert result["record_ref"] == "sfmc://campaigns/summer26"

    def test_update_success_via_default_v1_stub(self):
        state = _state(
            intent="update_campaign",
            campaign_id="summer26",
            salesforce_payload=to_json({"campaignCode": "summer26", "description": "revised copy"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "summer26"

    def test_trigger_send_success_via_default_v1_stub(self):
        state = _state(
            intent="trigger_send",
            campaign_id="",
            salesforce_payload=to_json(
                {"definition_key": "welcome-01", "message": {"To": {"SubscriberKey": "sub-1001"}}}
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The stub receipt carries a synthetic requestId -> record_id is present.
        assert result["record_id"]
        assert result["record_ref"] == "sfmc://triggered-sends/welcome-01"

    def test_salesforce_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `salesforce:` section as the JSON
        # salesforce_config state field; the stub transport still serves the call.
        state = _state(salesforce_config=to_json({"base_url": "https://sub.rest.marketingcloudapis.example"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "sfmc://campaigns/1001"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"salesforce": {"base_url": "https://sub.rest.marketingcloudapis.example"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"

    def test_missing_payload_errors(self):
        result = self.node(_state(salesforce_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_id_errors(self):
        state = _state(campaign_id="", salesforce_payload=to_json({"campaign_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved campaign id" in entry for entry in result["error_log"])

    def test_update_with_unresolved_id_errors(self):
        state = _state(
            intent="update_campaign",
            campaign_id="",
            salesforce_payload=to_json({"description": "revised copy"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_trigger_send_with_unresolved_definition_key_errors(self):
        state = _state(
            intent="trigger_send",
            campaign_id="",
            salesforce_payload=to_json({"definition_key": "", "message": {"To": {}}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved send-definition key" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_campaign"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_salesforce_api_node.SfmcClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing SFMC_ACCESS_TOKEN is a hard
        # error - a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_salesforce_api_node.SfmcClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_salesforce_api_node.SfmcClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"SFMC_ACCESS_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["campaign_id"] == "1001"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_salesforce_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_salesforce_api_complete"]
        assert payload["intent"] == "lookup_campaign"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True
