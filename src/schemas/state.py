"""AgentCore Platform v1.0 - CMN-C2-232 Salesforce Marketing Cloud Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects (and nested dict/list
# containers) are not msgpack-safe. Extend AgentState with agent-specific fields
# only, and declare every domain field NotRequired[...] (fields are absent until
# their producer node writes them). salesforce_payload / salesforce_config /
# redaction_flags are dicts/lists at the point of use but are stored in State as
# JSON strings via to_json/from_json below. Do NOT add credentials, secrets, or
# Pydantic models. The SFMC access token is NEVER stored here - it is read via
# ctx.secrets in CallSalesforceApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact, msgpack-safe JSON string.

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Salesforce Marketing Cloud agent state.

    Shared fields (user_input, validated_input, input_context, intent, result,
    status, formatted_output, session_id, node_history, error_log,
    correlation_id, trace_id, hitl_*, etc.) are inherited from AgentState and
    NOT re-declared. Only SFMC-workflow fields are added below, all NotRequired.
    All values are JSON/msgpack-serializable primitives - the SFMC access token
    is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target hint (campaign id/code or triggered-send
    # definition key, taken from the validated input_context). Never inferred;
    # resolution to an SFMC campaign id is explicit-only (pass-through when the
    # hint or the request text already carries one).
    campaign_hint: NotRequired[str]
    campaign_id: NotRequired[str]  # resolved SFMC campaign id/code
    # Caller-supplied send recipient key (validated input_context).
    subscriber_key: NotRequired[str]

    # ValidateInput (deterministic redaction scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferSalesforceFields
    campaign_name: NotRequired[str]  # campaign display name / record label
    # JSON - assembled SFMC REST API request body (stored as a JSON string,
    # not a native dict; (de)serialize via to_json/from_json).
    salesforce_payload: NotRequired[Optional[str]]

    # `salesforce` settings section forwarded by _parent_config() and injected
    # by the inner graph's _extra_initial_state() (JSON string).
    salesforce_config: NotRequired[Optional[str]]

    # CallSalesforceApi
    record_id: NotRequired[str]  # campaign id / send request id returned by SFMC
    record_ref: NotRequired[str]  # human-readable reference (sfmc://campaigns/<id>)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message

    # Set when the run completes WITHOUT performing the request because the
    # caller's input could not be accepted as written - a rejection the caller
    # can correct and retry. The run still completes: nothing is sent to the
    # marketing platform, no confirmation is produced, and the domain audit
    # event for the rejection is still emitted. Carrying this as a completion
    # marker rather than a terminal error is what lets the caller see the
    # reason and send a corrected request on the same conversation.
    #
    # Content the agent refuses outright, and a breach of a contract the caller
    # cannot influence, are NOT reported here - those stay terminal.
    error_code: NotRequired[str]
