# CMN-C2-232 - The caller-visible ERROR envelope carries closed-set labels only.
#
# On any non-success path the caller must receive values this template chose
# from a closed set - a constant reason code - and nothing read from error_log,
# from the output gate's violation messages, or from any other node-authored
# string. An internal entry can carry an upstream exception message or a
# third-party response body (identifiers, names, emails), and truncation or
# credential-only redaction of such text is not a closed-set contract.
#
# Two surfaces are held, each parameterised over every error path it has:
#   - PostProcessNode's `formatted_output` (the field the framework envelope
#     selects as `output`), driven directly and through the framework pipeline;
#   - SalesforceMarketingCloudAgent.get_output() (the invoke envelope every
#     deployment shape returns), including the states a compiled run cannot be
#     coaxed into.
# The source half is held too: the API node writes closed-set values into
# error_log in the first place (HTTP status / exception class, never a message).
#
# The sentinel is deliberately NOT credential-shaped and carries no trace
# fragment: a redaction-based envelope passes it straight through, which is the
# defect these tests must fail on - not one a redaction happened to catch.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SalesforceMarketingCloudAgent
from src.nodes.call_salesforce_api_node import CallSalesforceApiNode
from src.nodes.post_process_node import ERROR_REASONS, PostProcessNode, error_envelope
from src.schemas.state import to_json
from src.services.sfmc_client import SfmcApiError

# Assembled at runtime (never a committed literal): a name, an email and a
# token-shaped fragment - what an upstream response body can carry.
_FRAGMENTS = ("A. Tanaka", "a.tanaka@example.com", "sk-" + "live-xxx")
_SENTINEL = (
    "boom: upstream said {'customer':'"
    + _FRAGMENTS[0]
    + "','email':'"
    + _FRAGMENTS[1]
    + "','token':'"
    + _FRAGMENTS[2]
    + "'}"
)
_MARKERS = (_SENTINEL, "upstream said", *_FRAGMENTS)
# A framework-authored entry (the trust gate's own wording, without a real
# credential) - internal too, never projected.
_FRAMEWORK_ENTRY = "[PreProcessNode] trust gate denied: required=verified_external, caller=anonymous"
_BEARER_LIKE = "Bearer " + "a" * 24  # runtime-built; no committed literal


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.call_salesforce_api_node.emit_trace_event", lambda *a, **k: None)


def _strings(value):
    """Every string reachable in value: dict keys and values, list/tuple items,
    and the repr of anything else that is not a plain scalar."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _strings(child)
    elif value is not None and not isinstance(value, (bool, int, float)):
        yield repr(value)


def _assert_markers_absent(mapping: dict) -> None:
    for text in _strings(mapping):
        for marker in _MARKERS:
            assert marker not in text, f"marker reached the caller: {marker!r}"
    rendered = json.dumps(mapping, default=str)
    for marker in _MARKERS:
        assert marker not in rendered


# ── PostProcessNode: formatted_output on every error path ─────────────────────


def _post_state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "1001",
        "record_ref": "sfmc://campaigns/1001",
        "campaign_id": "1001",
        "campaign_name": "Campaign 1001",
        "intent": "lookup_campaign",
        "confirmation": "Retrieved marketing campaign 'Campaign 1001' - id=1001",
        "salesforce_payload": to_json({"campaign_id": "1001"}),
        # Every internal channel is seeded with the sentinel: the inner
        # product and the log.
        "result": {"confirmation": _SENTINEL, "record_id": "1001"},
        "error_log": [_SENTINEL, _FRAMEWORK_ENTRY],
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "closed-set-test",
        "node_history": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _drive(state: dict, entry: str) -> dict:
    node = PostProcessNode()
    return node.execute(state) if entry == "execute" else node(state)


# (state overrides, expected reason, entry point). The errored-state path is
# reached only by a direct execute(): the backbone routes an errored run
# straight to finalize, and the framework pipeline short-circuits before
# execute() on an errored incoming state (returning the state itself, which is
# a graph-internal partial update, not the caller's envelope). The gate paths
# are driven both directly and through the framework pipeline.
_POST_PROCESS_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "record_id": "", "record_ref": ""},
        "workflow_failed",
        "execute",
        id="inner-workflow-errored/execute",
    ),
    pytest.param({"record_id": "", "record_ref": ""}, "output_withheld", "execute", id="gate-no-evidence/execute"),
    pytest.param({"record_id": "", "record_ref": ""}, "output_withheld", "call", id="gate-no-evidence/call"),
    pytest.param({"confirmation": _BEARER_LIKE}, "output_withheld", "execute", id="gate-credential/execute"),
    pytest.param({"confirmation": _BEARER_LIKE}, "output_withheld", "call", id="gate-credential/call"),
    pytest.param(
        {"campaign_name": _SENTINEL, "confirmation": _BEARER_LIKE, "record_id": "", "record_ref": ""},
        "output_withheld",
        "call",
        id="gate-both-rules/call",
    ),
]
_GATE_PATHS = [p for p in _POST_PROCESS_ERROR_PATHS if p.values[1] == "output_withheld"]


class TestPostProcessErrorEnvelope:
    @pytest.mark.parametrize("overrides, reason, entry", _POST_PROCESS_ERROR_PATHS)
    def test_envelope_values_are_drawn_from_the_declared_constants(self, overrides, reason, entry):
        result = _drive(_post_state(**overrides), entry)
        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}
        assert envelope["reason"] in ERROR_REASONS
        assert envelope == error_envelope(reason)
        assert result["result"] is None

    @pytest.mark.parametrize("overrides, reason, entry", _POST_PROCESS_ERROR_PATHS)
    def test_envelope_is_truthy_so_the_result_fallback_stays_closed(self, overrides, reason, entry):
        # AgentBaseGraph.get_output() selects `formatted_output or result`; a
        # falsy envelope would re-open the fallback onto `result`.
        result = _drive(_post_state(**overrides), entry)
        assert result["formatted_output"]

    @pytest.mark.parametrize("overrides, reason, entry", _POST_PROCESS_ERROR_PATHS)
    def test_sentinel_appears_nowhere_in_the_returned_mapping(self, overrides, reason, entry):
        result = _drive(_post_state(**overrides), entry)
        _assert_markers_absent(result)
        assert _FRAMEWORK_ENTRY not in json.dumps(result, default=str)

    @pytest.mark.parametrize("overrides, reason, entry", _GATE_PATHS)
    def test_gate_messages_stay_in_the_internal_channel(self, overrides, reason, entry):
        result = _drive(_post_state(**overrides), entry)
        # The audit trail keeps the violation; the caller-visible field does not.
        assert any("output gate" in line for line in result["error_log"])
        assert not any("output gate" in text for text in _strings(result["formatted_output"]))


# ── The invoke envelope: get_output() on every non-success state ──────────────


def _invoke_state(**overrides) -> dict:
    state = {
        "status": AgentStatus.ERROR.value,
        "result": {"confirmation": _SENTINEL, "record_id": "1001"},
        "error_log": [_SENTINEL, _FRAMEWORK_ENTRY],
        "trace_id": "tr",
        "correlation_id": "co",
        "node_history": ["InitializeNode", "FinalizeNode"],
    }
    state.update(overrides)
    return state


_INVOKE_ERROR_PATHS = [
    pytest.param({}, "workflow_failed", id="inner-error-routed-to-finalize"),
    pytest.param({"status": AgentStatus.TIMEOUT.value}, "workflow_failed", id="timeout-status"),
    pytest.param(
        {"formatted_output": error_envelope("output_withheld"), "result": None},
        "output_withheld",
        id="post-process-gate-block",
    ),
    pytest.param({"formatted_output": _SENTINEL}, "workflow_failed", id="non-dict-formatted-output"),
    pytest.param({"formatted_output": {"reason": _SENTINEL}}, "workflow_failed", id="reason-outside-the-closed-set"),
    pytest.param({"formatted_output": {"reason": ["not", "a", "str"]}}, "workflow_failed", id="reason-not-a-string"),
]


class TestInvokeErrorEnvelope:
    @pytest.mark.parametrize("overrides, reason", _INVOKE_ERROR_PATHS)
    def test_error_values_are_drawn_from_the_declared_constants(self, overrides, reason):
        out = SalesforceMarketingCloudAgent().get_output(_invoke_state(**overrides))
        assert out["status"] != AgentStatus.SUCCESS.value
        assert set(out["error"]) == {"reason"}
        assert out["error"]["reason"] in ERROR_REASONS
        assert out["error"] == error_envelope(reason)
        assert out["output"] is None
        assert "error_log" not in out
        for key in ("record_id", "record_ref", "campaign_id", "campaign_name", "intent", "confirmation"):
            assert key not in out
        assert "formatted_output" not in out

    @pytest.mark.parametrize("overrides, reason", _INVOKE_ERROR_PATHS)
    def test_sentinel_appears_nowhere_in_the_invoke_envelope(self, overrides, reason):
        out = SalesforceMarketingCloudAgent().get_output(_invoke_state(**overrides))
        _assert_markers_absent(out)
        assert _FRAMEWORK_ENTRY not in json.dumps(out, default=str)

    def test_success_envelope_is_unchanged(self):
        gated = {"record_id": "1001", "record_ref": "sfmc://campaigns/1001", "confirmation": "ok"}
        out = SalesforceMarketingCloudAgent().get_output(
            _invoke_state(status=AgentStatus.SUCCESS.value, formatted_output=gated, error_log=[])
        )
        assert out["output"] == gated
        assert "error" not in out
        assert "error_log" not in out


# ── The envelope builder itself ───────────────────────────────────────────────


class TestErrorEnvelopeBuilder:
    def test_every_declared_reason_builds_a_truthy_single_key_envelope(self):
        for reason in ERROR_REASONS:
            envelope = error_envelope(reason)
            assert envelope
            assert envelope == {"reason": reason}

    def test_a_reason_outside_the_closed_set_is_refused_and_not_echoed(self):
        with pytest.raises(ValueError) as info:
            error_envelope(_SENTINEL)
        assert _SENTINEL not in str(info.value)


# ── The source half: error_log is built from closed-set values ───────────────


class _UpstreamBodyClient:
    """SfmcClient stand-in: the API error carries an upstream response body."""

    uses_stub_transport = True

    def __init__(self, *args, **kwargs):
        pass

    def find_campaign(self, campaign_id, api_token):
        raise SfmcApiError(403, _SENTINEL)


class _TransportFailureClient:
    """SfmcClient stand-in: the transport itself fails with a message."""

    uses_stub_transport = True

    def __init__(self, *args, **kwargs):
        pass

    def find_campaign(self, campaign_id, api_token):
        raise ConnectionError(_SENTINEL)


def _api_state() -> dict:
    return {
        "salesforce_payload": to_json({"campaign_id": "1001"}),
        "intent": "lookup_campaign",
        "campaign_id": "1001",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "closed-set-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestUpstreamTextNeverEntersTheLog:
    def test_api_error_logs_the_http_status_not_the_response_body(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_salesforce_api_node.SfmcClient", _UpstreamBodyClient)
        result = CallSalesforceApiNode()(_api_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"] == ["CallSalesforceApiNode: SFMC API error 403"]
        _assert_markers_absent(result)

    def test_transport_failure_logs_the_exception_class_not_its_message(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_salesforce_api_node.SfmcClient", _TransportFailureClient)
        result = CallSalesforceApiNode()(_api_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"] == ["CallSalesforceApiNode: SFMC call failed: ConnectionError"]
        _assert_markers_absent(result)
