# CMN-C2-232 - Unit tests: InferSalesforceFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (the framework's __call__ routes trust gate ->
# input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value. Positive payloads are
# PII-free: the framework input gate rewrites Title-Case bigrams in
# validated_input, so quoted display names and field lines stay lower-case.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_salesforce_fields_node import (
    _MAX_ATTRIBUTE_VALUE_CHARS,
    _MAX_ATTRIBUTES,
    InferSalesforceFieldsNode,
)
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_salesforce_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_campaign", campaign_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "campaign_hint": campaign_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferSalesforceFieldsNode:
    def setup_method(self):
        self.node = InferSalesforceFieldsNode()

    def test_lookup_extracts_code_from_text(self):
        result = self.node(_state("Look up the marketing campaign with campaign id 1001 and summarize it."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == "1001"
        # salesforce_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["salesforce_payload"], str)
        assert from_json(result["salesforce_payload"], {}) == {"campaign_id": "1001"}

    def test_code_shaped_hint_used_when_text_has_no_code(self):
        result = self.node(_state("Show the current campaign summary", campaign_hint="summer26"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == "summer26"
        assert from_json(result["salesforce_payload"], {}) == {"campaign_id": "summer26"}

    def test_create_builds_campaign_body(self):
        # Field values stay lower-case: the framework name mask rewrites
        # Title-Case word pairs even ACROSS newlines before execute() sees the text.
        text = (
            'Create a marketing campaign named "summer launch 2026".\n'
            "Description: kickoff email push for the summer product line\n"
            "campaign code: summer26\n"
            "audience: existing subscribers"
        )
        result = self.node(_state(text, intent="create_campaign"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == "summer26"
        assert result["campaign_name"] == "summer launch 2026"
        body = from_json(result["salesforce_payload"], {})
        assert body["name"] == "summer launch 2026"
        assert body["campaignCode"] == "summer26"
        assert body["description"] == "kickoff email push for the summer product line"
        # Unmapped "Key: value" lines ride as the bounded attributes list.
        assert {"name": "audience", "value": "existing subscribers"} in body["attributes"]

    def test_update_builds_campaign_body(self):
        text = "Update the campaign with campaign code summer26\ndescription: revised copy for the summer push"
        result = self.node(_state(text, intent="update_campaign"))
        assert result["status"] == AgentStatus.SUCCESS.value
        body = from_json(result["salesforce_payload"], {})
        assert body["campaignCode"] == "summer26"
        assert body["description"] == "revised copy for the summer push"

    def test_trigger_send_builds_message_body(self):
        text = (
            "trigger the welcome send now\n"
            "definition key: welcome-01\n"
            "subscriber key: sub-1001\n"
            "promo: summer sale"
        )
        result = self.node(_state(text, intent="trigger_send"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["salesforce_payload"], {})
        assert payload["definition_key"] == "welcome-01"
        to = payload["message"]["To"]
        # Recipients are addressed by SubscriberKey - never raw email, by design.
        assert to["SubscriberKey"] == "sub-1001"
        assert to["ContactAttributes"]["SubscriberAttributes"] == {"promo": "summer sale"}

    def test_trigger_send_definition_key_falls_back_to_hint(self):
        result = self.node(_state("fire off the promo blast now", intent="trigger_send", campaign_hint="promo-a"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["salesforce_payload"], {})
        assert payload["definition_key"] == "promo-a"

    def test_unresolved_code_left_empty_never_invented(self):
        result = self.node(_state("Show the campaign summary for the flagged request"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == ""
        assert from_json(result["salesforce_payload"], {}) == {"campaign_id": ""}

    def test_non_code_shaped_hint_left_unresolved(self):
        result = self.node(_state("Show the campaign summary", campaign_hint="not a valid code!"))
        assert result["campaign_id"] == ""

    def test_display_name_is_reduced_to_the_inert_alphabet(self):
        """The campaign display name is the one free-form value that reaches the
        external response, so punctuation the request sanitizer does not touch
        (colons, at-signs, stray quotes) is dropped before it is stored."""
        text = 'Create a campaign named "launch: mkt@team #1" for the season\ncampaign code: summer26'
        result = self.node(_state(text, intent="create_campaign"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_name"] == "launch mkt team #1"
        assert ":" not in result["campaign_name"]
        assert "@" not in result["campaign_name"]

    def test_display_name_is_length_capped(self):
        text = 'Create a campaign named "' + "a" * 200 + '"'
        result = self.node(_state(text, intent="create_campaign"))
        assert len(result["campaign_name"]) == 100

    def test_caller_subscriber_key_is_used_when_the_text_has_none(self):
        """The validated caller key travels on the bridged context channel."""
        result = self.node(
            _state(
                "trigger the welcome send now\ndefinition key: welcome-01",
                intent="trigger_send",
                subscriber_key="sub-2002",
            )
        )
        payload = from_json(result["salesforce_payload"], {})
        assert payload["message"]["To"]["SubscriberKey"] == "sub-2002"

    def test_attribute_lines_are_capped_at_the_declared_bound(self):
        """A request cannot grow the assembled body without bound by repeating
        "Key: value" lines."""
        lines = ["Update the campaign with campaign code summer26"]
        lines += [f"field{i}: value{i}" for i in range(_MAX_ATTRIBUTES + 20)]
        result = self.node(_state("\n".join(lines), intent="update_campaign"))
        body = from_json(result["salesforce_payload"], {})
        assert len(body["attributes"]) == _MAX_ATTRIBUTES
        assert _MAX_ATTRIBUTES <= 50  # the declared bound stays a real bound

    def test_attribute_values_are_length_capped(self):
        text = "Update the campaign with campaign code summer26\nnote: " + "a" * 500
        result = self.node(_state(text, intent="update_campaign"))
        body = from_json(result["salesforce_payload"], {})
        assert len(body["attributes"][0]["value"]) == _MAX_ATTRIBUTE_VALUE_CHARS

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
