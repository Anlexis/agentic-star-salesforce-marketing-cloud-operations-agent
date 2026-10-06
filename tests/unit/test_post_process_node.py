# CMN-C2-232 - Unit tests: PostProcessNode (outer backbone; the output gate)
#
# Canon: invoked via node(state) (the framework's __call__ routes trust gate ->
# input gate -> execute -> output gate); this backbone formatter declares
# ANONYMOUS -> the state builder sets caller_trust_level =
# TrustLevel.ANONYMOUS.value. The domain output gate is the MODULE-LEVEL
# _security_gate_output() helper (the framework gate methods are @final and the
# runtime auto-wraps _extra_ hooks), so the helper is also unit-tested directly
# as a plain function.
#
# The invariant under test: the raw salesforce_payload request dict is never
# surfaced - only whitelisted scalar addressing fields pass - and the gate scans
# nested containers recursively, on the success path and the error path alike.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    PostProcessNode,
    _security_gate_output,
    _vetted_payload_fields,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "1001",
        "record_ref": "sfmc://campaigns/1001",
        "campaign_id": "1001",
        "campaign_name": "Campaign 1001",
        "intent": "lookup_campaign",
        "confirmation": "Retrieved marketing campaign 'Campaign 1001' - ref=sfmc://campaigns/1001 - id=1001",
        "salesforce_payload": to_json({"campaign_id": "1001"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # AgentStatus is a str-Enum, so == passes for the bare enum too -
        # only an exact type check catches a bare-enum write.
        assert type(result["status"]) is str  # noqa: E721 - exact-type check is the point
        out = result["formatted_output"]
        assert out["record_id"] == "1001"
        assert out["record_ref"] == "sfmc://campaigns/1001"
        assert out["campaign_id"] == "1001"
        assert out["intent"] == "lookup_campaign"
        assert out["confirmation"].startswith("Retrieved marketing campaign")
        # Only whitelisted scalar addressing fields survive; here the lookup
        # body is already exactly the vetted campaign_id.
        assert out["salesforce_payload"] == {"campaign_id": "1001"}

    def test_credential_nested_in_payload_never_surfaced(self):
        """A credential-shaped value nested inside the assembled request
        payload (attributes /
        ContactAttributes.SubscriberAttributes carry arbitrary caller-typed
        "Key: value" strings) must NOT appear anywhere in the returned output -
        the whitelist drops non-vetted keys and nested containers before the
        payload is surfaced."""
        token_like = "Bearer " + "a" * 24  # built at runtime; no committed literal
        state = _state(
            intent="trigger_send",
            record_id="req-123",
            record_ref="sfmc://triggered-sends/WELCOME-01",
            salesforce_payload=to_json(
                {
                    "definition_key": "WELCOME-01",
                    "message": {
                        "To": {
                            "SubscriberKey": "SUB-1001",
                            "ContactAttributes": {
                                "SubscriberAttributes": {"Api Token": token_like},
                            },
                        }
                    },
                }
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        # Only the vetted scalar addressing field survives; the nested message
        # container (and the credential inside it) is dropped entirely.
        assert out["salesforce_payload"] == {"definition_key": "WELCOME-01"}
        assert token_like not in json.dumps(result, default=str)

    def test_free_text_payload_fields_never_surfaced(self):
        """Free-text request fields (campaign name/description typed by
        the requester) and the arbitrary attributes list stay out of the
        surfaced payload entirely."""
        state = _state(
            intent="create_campaign",
            salesforce_payload=to_json(
                {
                    "name": "summer launch 2026",
                    "campaignCode": "summer26",
                    "description": "internal launch notes with free text",
                    "attributes": [{"name": "Budget Owner", "value": "internal-only note"}],
                }
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["salesforce_payload"] == {"campaignCode": "summer26"}
        surfaced = json.dumps(out["salesforce_payload"], default=str)
        assert "internal launch notes" not in surfaced
        assert "Budget Owner" not in surfaced

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallSalesforceApiNode: SFMC API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "SFMC API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])

    def test_blocked_response_surfaces_nothing_pre_gate(self):
        """When the gate blocks, the raw inner `result` is cleared too.

        The invoke envelope falls back to `result` for its `output` key when no
        formatted_output exists, so leaving it in place would hand the caller
        exactly the pre-gate content the gate just refused."""
        token_like = "Bearer " + "a" * 24
        state = _state(campaign_name=token_like, result={"confirmation": token_like})
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] is None
        assert token_like not in json.dumps(result, default=str)

    def test_error_envelope_carries_the_closed_set_reason_only(self):
        """Direct execute() on an already-errored state (the backbone routes an
        errored run straight to finalize, so this is the only way in): the
        envelope must not fabricate success, and it carries the constant reason
        code only - the internal entry (which can carry a stack trace with
        absolute source paths) is not projected in any form."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=['[Node] failed: boom\nTraceback (most recent call last):\n  File "/x/y.py", line 1'],
        )
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] is None
        assert result["formatted_output"] == {"reason": "workflow_failed"}
        rendered = json.dumps(result, default=str)
        assert "boom" not in rendered
        assert "Traceback" not in rendered


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "1001", "record_ref": "sfmc://campaigns/1001", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"record_id": "1001", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []

    def test_blocks_credential_nested_in_dict_value(self):
        """The gate scans nested containers recursively - a credential buried
        inside a surfaced dict is flagged, not skipped."""
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {
                "record_id": "1001",
                "record_ref": "sfmc://campaigns/1001",
                "salesforce_payload": {"message": {"To": {"headers": [bearer_like]}}},
            },
            is_success=True,
        )
        assert any("salesforce_payload" in v for v in violations)


class TestVettedPayloadFieldsHelper:
    """Whitelist helper: only vetted scalar addressing fields pass."""

    def test_whitelists_scalar_fields_only(self):
        vetted = _vetted_payload_fields(
            {
                "campaign_id": "1001",
                "campaignCode": "summer26",
                "definition_key": "WELCOME-01",
                "name": "free text stays out",
                "description": "free text stays out",
                "color": "red",
                "attributes": [{"name": "Region", "value": "APAC"}],
                "message": {"To": {"SubscriberKey": "SUB-1"}},
            }
        )
        assert vetted == {
            "campaign_id": "1001",
            "campaignCode": "summer26",
            "definition_key": "WELCOME-01",
        }

    def test_drops_non_scalar_value_under_vetted_key(self):
        # Even a whitelisted KEY is dropped when its value is a container.
        vetted = _vetted_payload_fields({"definition_key": {"nested": "dict"}, "campaign_id": "7"})
        assert vetted == {"campaign_id": "7"}

    def test_non_dict_input_yields_empty(self):
        assert _vetted_payload_fields(["not", "a", "dict"]) == {}


class TestErrorEnvelopeIsGatedToo:
    """The invariant holds on the error path, not only the success path."""

    def setup_method(self):
        self.node = PostProcessNode()

    def test_credential_in_an_error_entry_is_blocked(self):
        token_like = "Bearer " + "a" * 24
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=[f"CallSalesforceApiNode: upstream rejected {token_like}"],
        )
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert token_like not in json.dumps(result, default=str)

    def test_error_path_does_not_require_record_evidence(self):
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallSalesforceApiNode: unresolved campaign id"],
        )
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": "workflow_failed"}
        assert "unresolved campaign id" not in json.dumps(result, default=str)
