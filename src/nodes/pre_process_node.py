"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone. This node owns the caller contract:

  * it is the single external trust gate (required_trust_level =
    VERIFIED_EXTERNAL) - an under-trusted caller is denied before anything runs;
  * it validates the request text (present, bounded, no instruction-override
    form) and the structured caller data (`input_context`) field by field
    against explicit bounds, failing CLOSED and naming only the offending FIELD;
  * it serializes the validated request text into a JSON string in
    `validated_input`, which the GraphNode (`main` slot) hands to the inner
    Salesforce Marketing Cloud workflow graph. The structured caller data stays
    on the `input_context` channel, which that wrapper bridges into the inner
    graph - one channel per kind of data, nothing carried twice.

The refusal rules are enforced HERE, in the node's own execute(), not delegated
to a surrounding gate: the template must hold its guarantees wherever it is
deployed, so the tests drive execute() directly with no framework wrapper in
front of it.

Business validation (intent, field extraction, SFMC request shaping) happens
inside the inner graph.
"""

import json
import re
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import DISALLOWED_FORM, EMPTY_INPUT, INVALID_VALUE, TOO_LONG

from src.services.security import contains_credential_like, find_injection, sanitize_query

# Upper bound on the request text. Long enough for a multi-line campaign brief,
# short enough that no caller can push an unbounded body through the pipeline.
MAX_REQUEST_CHARS = 4000

# ── Caller-data (input_context) contract ─────────────────────────────────────
# input_context is caller-supplied and untrusted. Every declared field is locked
# to an inert identifier alphabet before it can influence the run or be rendered
# back, so free text - and with it any address, credential or markup - cannot
# enter through this channel at all (the framework's PII masking covers
# user_input / validated_input only, never the structured context channel).
# Violations return status=ERROR naming the FIELD, never the rejected VALUE.
# Undeclared keys are ignored; the entry-point adapter caps the serialized size.
_CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
_DEFINITION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_SUBSCRIBER_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")

_CONTEXT_FIELDS: "tuple[tuple[str, re.Pattern[str], str], ...]" = (
    ("campaign_id", _CAMPAIGN_ID_RE, "1-20 chars from A-Z a-z 0-9 _ -, starting alphanumeric"),
    ("campaign_hint", _CAMPAIGN_ID_RE, "1-20 chars from A-Z a-z 0-9 _ -, starting alphanumeric"),
    ("definition_key", _DEFINITION_KEY_RE, "1-40 chars from A-Z a-z 0-9 _ -, starting alphanumeric"),
    ("subscriber_key", _SUBSCRIBER_KEY_RE, "1-64 chars from A-Z a-z 0-9 . _ -, starting alphanumeric"),
    ("channel", _CHANNEL_RE, "a lowercase identifier (a-z, 0-9, _; 1-32 chars)"),
)


def validate_input_context(input_context: Any) -> "tuple[dict[str, str], Optional[str]]":
    """Validate the caller's structured data against the declared contract.

    Returns ``(normalised_context, error_message)``. Error messages name the
    field and the expected shape - never the rejected value. Accepted fields:

        campaign_id / campaign_hint  campaign id or code
        definition_key               triggered-send definition key
        subscriber_key               send recipient key
        channel                      caller channel tag (audit metadata)

    Any other key is ignored. A non-mapping input_context is rejected.
    """
    if input_context is None:
        return {}, None
    if not isinstance(input_context, dict):
        return {}, "input_context must be an object"

    normalised: "dict[str, str]" = {}
    for field, pattern, shape in _CONTEXT_FIELDS:
        value = input_context.get(field)
        if value is None:
            continue
        # str-only on purpose: numbers, booleans and containers are all rejected
        # by the same rule, so no numeric shape (NaN/Infinity included) can slip
        # through a permissive coercion. A string that merely SPELLS "NaN" is an
        # ordinary identifier here - nothing in the pipeline ever parses these
        # fields as numbers.
        if not isinstance(value, str) or not pattern.match(value):
            return {}, f"input_context.{field} must be {shape}"
        # The identifier alphabet already excludes addresses and spaced bearer
        # forms, but an underscore-and-digits token still fits it. The same
        # credential shapes the request-text scan redacts are refused here, at
        # the door, rather than being caught later by the output gate.
        if contains_credential_like(value):
            return {}, f"input_context.{field} must not carry a credential-shaped value"
        normalised[field] = value
    return normalised, None


class PreProcessNode(FunctionNode):
    """Validate the caller's request and structured data, then shape it for the workflow."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner SFMC call runs under this same (unelevated)
    # context, so the external gate lives HERE, not on the inner API node. An
    # under-trusted (ANONYMOUS) caller is denied at this gate before any call.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> "dict[str, Any]":
        emit_progress("Checking the request...")
        user_input = state.get("user_input", "")
        input_context = state.get("input_context") or {}  # read-only

        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            emit_trace_event("input_validation_failed", {"reason": "empty_input"}, state)
            emit_progress(EMPTY_INPUT)
            # The caller can send a request and try again, so the run completes
            # carrying the reason rather than terminating and surfacing only an
            # exception type.
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        if len(user_input) > MAX_REQUEST_CHARS:
            emit_trace_event(
                "input_validation_failed",
                {"reason": "request_too_long", "length": len(user_input)},
                state,
            )
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [f"PreProcessNode: request exceeds {MAX_REQUEST_CHARS} characters"],
            }

        # Instruction-override refusal, owned by this node. Nothing derived from
        # a refused request is carried forward, and the matched text is never
        # echoed back.
        if find_injection(user_input):
            emit_trace_event("input_validation_failed", {"reason": "disallowed_request_form"}, state)
            emit_progress(DISALLOWED_FORM)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request contains a disallowed instruction form"],
            }

        # Caller data contract: bounded, inert, fail-closed.
        context, context_error = validate_input_context(input_context)
        if context_error:
            emit_trace_event("input_validation_failed", {"reason": "invalid_input_context"}, state)
            emit_progress(INVALID_VALUE)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: {context_error}"],
            }

        # Strip markup and control characters, normalise spacing, cap length.
        sanitized_input = sanitize_query(user_input.strip())

        # The request TEXT travels in validated_input; the validated STRUCTURED
        # data stays on the input_context channel, which the main-slot wrapper
        # bridges into the inner graph (src/graph/context_bridge.py). One
        # channel per kind of data - nothing is carried twice.
        validated_input = json.dumps({"text": sanitized_input})

        # Audit the shaped request - presence signals only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_campaign_hint": bool(
                    context.get("campaign_id") or context.get("campaign_hint") or context.get("definition_key")
                ),
                "has_subscriber_key": bool(context.get("subscriber_key")),
                "channel": context.get("channel", "unknown"),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }
