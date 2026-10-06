"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Salesforce
Marketing Cloud workflow graph has run. GraphNode.merge_output() maps the inner
result into the outer state; this node shapes the caller-facing
`formatted_output` and owns the domain output gate.

The gate is the MODULE-LEVEL `_security_gate_output()` below, called inline
from execute(). Two reasons it is not a framework hook: `_security_gate_output`
itself is @final on FunctionNode and cannot be overridden at all (a TypeError at
class definition - see tests/unit/test_framework_compliance_tc06_tc07.py), and
the `_extra_security_gate_output(result) -> result` hook can only pass a result
through or raise, whereas this gate must TURN a would-be success into a
structured `status=error` with its own `error_log`. A module-level helper also
makes the rule testable as a plain function, independent of any node.

The documented output invariant, enforced for EVERY representation the caller
can observe:

  1. the raw `salesforce_payload` request body never reaches the caller.
     InferSalesforceFieldsNode copies arbitrary "Key: value" request fields into
     nested `attributes` / `ContactAttributes.SubscriberAttributes` containers,
     so the assembled body can carry any string the requester typed. Only the
     whitelisted SCALAR addressing fields in `_VETTED_PAYLOAD_FIELDS`
     (campaign_id / campaignCode / definition_key - code-shaped identifiers by
     construction upstream) pass through;
  2. no credential-shaped string appears anywhere in the response - every
     nested string of the success output is scanned RECURSIVELY;
  3. a SUCCESS response always carries record evidence (record_id/record_ref),
     so the outcome of an SFMC action is never misrepresented;
  4. when the gate blocks, NOTHING pre-gate is surfaced - the node also clears
     the raw inner `result`, which the outer envelope would otherwise fall back
     to as `output`;
  5. the caller-visible ERROR envelope carries closed-set labels only (see
     `error_envelope()`): never an `error_log` line, a gate message or an
     exception's text. `error_log` stays the internal audit channel.
"""

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, OUTPUT_BLOCKED, TOO_LONG

from src.schemas.state import from_json
from src.services.security import contains_credential_like

# The credential-shape rule and the recursive walk live in
# src/services/security.py so the output gate here and the invoke envelope in
# src/graph/graph.py enforce ONE definition (defence in depth - the framework
# credential scan in FunctionNode also runs on every result).

# The ONLY salesforce_payload fields surfaced to the caller: scalar
# addressing/identity fields exclusively (code-shaped by construction upstream)
# - never free-text bodies ("name", "description") and never nested containers
# ("attributes", "message").
_VETTED_PAYLOAD_FIELDS = ("campaign_id", "campaignCode", "definition_key")

# ── Caller-visible ERROR envelope ─────────────────────────────────────────────
#
# On every non-success path the caller receives closed-set labels ONLY: a
# constant reason code this module chose. Nothing read from `error_log`, from
# the gate's own violation messages, or from any other node-authored string is
# projected - an internal entry can carry an upstream exception message or a
# third-party response body, and truncation or credential-only redaction of
# such text is not a closed set. `error_log` itself is untouched: it stays the
# internal channel the state reducer appends to and the audit trail reads.
#
# The envelope always carries its constant key, so it is always truthy.
# AgentBaseGraph.get_output() selects `formatted_output or result`, and a falsy
# envelope would re-open that fallback onto whatever survived in `result`.
_REASON_WORKFLOW_FAILED = "workflow_failed"  # the inner workflow reported status=error
_REASON_OUTPUT_WITHHELD = "output_withheld"  # this node's output gate blocked the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def error_envelope(reason: str) -> "dict[str, Any]":
    """The caller-visible ERROR envelope: a constant reason code and nothing else."""
    if reason not in ERROR_REASONS:
        raise ValueError("error envelope reason must be one of ERROR_REASONS")
    return {"reason": reason}


def _vetted_payload_fields(payload: Any) -> "dict[str, Any]":
    """Whitelist the parsed SFMC request body down to vetted scalar fields.

    Fail-closed: a key passes only if it is in `_VETTED_PAYLOAD_FIELDS` AND its
    value is a scalar (str/int/float/bool/None); everything else is dropped.
    """
    if not isinstance(payload, dict):
        return {}
    vetted: "dict[str, Any]" = {}
    for key in _VETTED_PAYLOAD_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or isinstance(value, (str, int, float, bool)):
            vetted[key] = value
    return vetted


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Returns the violations that block the response:

      - a SUCCESS response with no record evidence (record_id/record_ref), which
        would misrepresent the SFMC action outcome to the caller;
      - any credential-shaped string value anywhere in the caller-facing output,
        scanned RECURSIVELY so a surfaced container cannot smuggle one past the
        gate.

    The violation messages name a field key, never a caller value, and go to
    `error_log` only - they are not part of the caller-visible envelope.
    """
    problems: "list[str]" = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    for key, value in formatted_output.items():
        if contains_credential_like(value):
            problems.append(f"PostProcess output gate: credential-like value in formatted_output['{key}']")
    return problems


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """Fail-closed ERROR result: the closed-set envelope, with `result` cleared.

    `result` is cleared explicitly - the outer envelope falls back to it for its
    `output` key - so nothing pre-gate can surface. `new_errors` (this node's
    own gate violations) go to `error_log`, the internal channel, never into
    the envelope. Entries already in `error_log` are not re-emitted: the state
    reducer appends, so they would be duplicated.
    """
    emit_progress(OUTPUT_BLOCKED)
    contained: "dict[str, Any]" = {
        "status": AgentStatus.ERROR.value,
        "formatted_output": error_envelope(reason),
        "result": None,
    }
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


# Caller-facing wording for a run that completed without an answer. The marker
# is an internal reason code; this maps it to the sentence the caller sees.
# Static sentences only - no request value is ever substituted, so nothing the
# caller sent can be reflected back through this path.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Format the final agent output and enforce the output invariant."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> "dict[str, Any]":
        emit_progress("Finalising the response...")

        # The run completed without an answer because the request could not be
        # accepted as written. Report the reason as the response: the caller
        # needs to know what to change, and an empty body would leave them with
        # nothing. Status stays SUCCESS - the run did what it could with the
        # request it was given, and the caller can correct it and send again on
        # the same conversation.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "formatted_output": message,
                "result": message,
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }
        errored = state.get("status") == AgentStatus.ERROR.value

        # If the inner workflow errored, preserve the error status (do not mask
        # it). The backbone routes an errored run straight to finalize, so on
        # the compiled graph this branch is reached only by a direct execute();
        # it still returns the same closed-set envelope as every other error
        # path - the entries already in error_log stay internal.
        if errored:
            return _contain(_REASON_WORKFLOW_FAILED)

        # NEVER surface the raw salesforce_payload request dict (nested
        # attributes / SubscriberAttributes carry arbitrary caller-typed
        # "Key: value" strings) - only the whitelisted scalar addressing fields
        # reach the caller.
        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "campaign_id": state.get("campaign_id", ""),
            "campaign_name": state.get("campaign_name", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "salesforce_payload": _vetted_payload_fields(from_json(state.get("salesforce_payload"), {})),
        }

        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # Audit the block - outcome signals only (a reason code and a count).
            emit_trace_event(
                "post_process_output_blocked",
                {"reason": _REASON_OUTPUT_WITHHELD, "violations": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {"intent": state.get("intent", ""), "has_record_id": bool(state.get("record_id"))},
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
