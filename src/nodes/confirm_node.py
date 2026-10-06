"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the looked-up / created / updated campaign (or the triggered email
send) - id + reference + name - into a human-readable confirmation message,
surfacing the affected record for human review (risk mitigation).
"""

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import NOTHING_TO_ACT_ON

_VERBS = {
    "lookup_campaign": "Retrieved marketing campaign",
    "create_campaign": "Created marketing campaign",
    "update_campaign": "Updated marketing campaign",
    "trigger_send": "Triggered email send",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-fetched data -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> "dict[str, Any]":
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        emit_progress("Confirming the request...")
        record_id = state.get("record_id", "")
        record_ref = state.get("record_ref", "")
        campaign_name = state.get("campaign_name", "")
        intent = state.get("intent", "lookup_campaign")

        if not record_id and not record_ref:
            emit_progress(NOTHING_TO_ACT_ON)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no record_id/record_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed marketing request")
        parts = [f"{verb} '{campaign_name or record_id}'"]
        if record_ref:
            parts.append(f"ref={record_ref}")
        if record_id:
            parts.append(f"id={record_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_record_ref": bool(record_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "record_id": record_id,
                "record_ref": record_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
