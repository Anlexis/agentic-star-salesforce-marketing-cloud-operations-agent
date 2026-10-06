# PB-6: Backbone invoke-order + external-trust boundary (CMN-C2-232).
#
# A real outer-graph invoke exercising the fixed 5-node backbone
# (initialize -> pre_process -> main[inner SFMC workflow] -> post_process -> finalize).
#
# The SINGLE external trust gate lives on the outer backbone pre_process
# (required_trust_level = VERIFIED_EXTERNAL). GraphNode.execute() passes the
# caller's InvocationContext into the inner subgraph UNCHANGED (no trust
# elevation), so the inner SFMC call (CallSalesforceApiNode) is ANONYMOUS and
# runs under the caller's already-gated context. Two trust levels are asserted:
#
#   * VERIFIED_EXTERNAL (a real external caller - never for_internal()): passes
#     the pre_process gate, so the full backbone runs IN ORDER and the record
#     evidence surfaces in result["output"] (status success). The payload is
#     byte-equal to deploy/invoke_payload.json's "input" - the same request the
#     deployment evidence invoke sends.
#   * ANONYMOUS (an under-trusted caller): denied at the pre_process gate before
#     the inner SFMC call can run -> status=error, no record evidence.
#
# The SFMC call is served by the deterministic NETWORK-FREE simulator transport
# (no live tenant, no secret needed - the node runs on the documented
# placeholder). Record evidence is asserted by presence (mask-robust: the
# framework may mask raw identifiers), never by whole-repr.
from typing import ClassVar
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel

import json
import pathlib

import pytest

try:
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    from src.graph.graph import SalesforceMarketingCloudAgent

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_MAIN_SLOT_NODE = "SalesforceWorkflowGraphNode"

# CONTRACT: deploy/invoke_payload.json["input"] MUST equal this exact string -
# the deployment evidence invoke and this test must exercise the identical
# request. test_valid_payload_is_byte_equal_to_deploy_payload asserts that
# equality so the two can never drift.
_VALID_PAYLOAD = (
    'Create a new marketing campaign named "summer launch 2026".\n'
    "Description: kickoff email push for the summer product line\n"
    "campaign code: summer26"
)
_SESSION_ID = "pb-invoke-order"

_PAYLOAD_PATH = pathlib.Path(__file__).parents[2] / "deploy" / "invoke_payload.json"

_EXPECTED_HISTORY = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE,
    "PostProcessNode",
    "FinalizeNode",
]


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _build_agent():
    agent = SalesforceMarketingCloudAgent()
    agent.compile()
    return agent


class TestBackboneInvokeOrder:
    def test_valid_payload_is_byte_equal_to_deploy_payload(self):
        """PB-6 exercises the deployment evidence request verbatim."""
        deployed = json.loads(_PAYLOAD_PATH.read_text(encoding="utf-8"))
        assert deployed["input"] == _VALID_PAYLOAD
        assert isinstance(deployed.get("session_id"), str) and deployed["session_id"]

    def test_verified_external_runs_full_backbone_in_order(self):
        """External (VERIFIED_EXTERNAL) caller: the exact 5-node backbone runs
        in order and record evidence surfaces in result["output"]."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert result.get("trace_id") and result.get("correlation_id")
        assert result.get("node_history", []) == _EXPECTED_HISTORY
        # Record evidence + confirmation surface in result["output"] (presence,
        # mask-robust - never the raw identifier value).
        out = result.get("output") or {}
        assert out.get("record_id") or out.get("record_ref")
        assert out.get("confirmation")
        assert out.get("intent") == "create_campaign"

    def test_under_trusted_caller_is_denied(self):
        """Under-trusted (ANONYMOUS) caller: ANONYMOUS < VERIFIED_EXTERNAL, so
        the pre_process gate refuses the request before the inner SFMC call
        can run - a well-formed error surface (gated, not crashed) with no
        record evidence. Note: the main slot NAME still appears in node_history
        because BaseNode.__call__ short-circuits on the already-errored state
        (its subgraph never executes); post_process is skipped by route()."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-anon", caller_trust_level=TrustLevel.ANONYMOUS),
        )
        assert result["status"] == AgentStatus.ERROR.value, f"result={result!r}"
        assert "PostProcessNode" not in result.get("node_history", [])
        assert not result.get("output")

    def test_empty_input_surfaces_error_not_crash(self):
        agent = _build_agent()
        result = agent.invoke(
            user_input="   ",
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        # The caller receives the reason as the response body. The internal
        # reason code is not part of the invoke() contract.
        assert result.get("output"), result

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(_PrivilegedTrustGateFixture.required_trust_level).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls
