"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Two jobs:

* reject empty / non-request input, and re-apply the caller-data contract to
  the structured `input_context` that reaches this graph. The outer backbone
  already validated it, but the inner graph is independently invocable, so the
  same rule is enforced here too - one shared validator, applied at both
  boundaries, failing closed and naming only the offending field.
* run a deterministic (regex, NOT model-based) scan of the inbound text for
  email addresses and access-token-like strings, which are flagged and redacted
  before anything is logged. Recipients are addressed by subscriber key /
  send-definition key - never by raw email address - so redacting a pasted
  email never breaks a legitimate request (the framework input gate
  additionally masks emails/phones/names in user_input / validated_input).
  This is flag-and-redact for safe logging, not a hard reject.
"""

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INVALID_VALUE

from src.nodes.pre_process_node import validate_input_context
from src.schemas.state import to_json

# Deterministic patterns: email addresses and bearer/JWT/API-token-like strings
# that might appear in a pasted request. Flagged and redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate the inbound marketing request and flag-and-redact sensitive spans."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> "dict[str, Any]":
        emit_progress("Checking the request...")
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct invocation.
        text: Any = raw
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Caller data arrives through input_context, which the outer node bridges
        # across the subgraph boundary (src/graph/context_bridge.py). Re-validate
        # it with the shared contract so a direct inner invoke is as safe as the
        # public path; the field alphabets are inert, so no address, credential
        # or markup can enter through this channel.
        context, context_error = validate_input_context(state.get("input_context"))
        if context_error:
            emit_progress(INVALID_VALUE)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"ValidateInputNode: {context_error}"],
            }
        campaign_hint = (
            context.get("campaign_id")
            or context.get("campaign_hint")
            or context.get("definition_key")
            or state.get("campaign_hint", "")
            or ""
        )
        subscriber_key = context.get("subscriber_key", "")

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        flags: "list[str]" = []
        redacted = text
        if _EMAIL_RE.search(redacted):
            flags.append("email")
            redacted = _EMAIL_RE.sub(_REDACTION, redacted)
        if _TOKEN_RE.search(redacted):
            flags.append("token")
            redacted = _TOKEN_RE.sub(_REDACTION, redacted)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_campaign_hint": bool(campaign_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "campaign_hint": campaign_hint,
            "subscriber_key": subscriber_key,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
