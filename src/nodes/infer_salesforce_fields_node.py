"""AgentCore Platform v1.0 - inner workflow Step 3: InferSalesforceFields.

Extracts the campaign id/code, display name, triggered-send definition key,
subscriber key, and "Key: value" fields from the (redacted) request and
assembles a validated Salesforce Marketing Cloud REST API request body for the
classified intent. The campaign id and the send-definition key are taken only
from an explicit mention in the text or the caller-supplied hint - an
unresolved id/key is left empty rather than invented, so the workflow can never
write to the wrong campaign or fire the wrong send; the executor surfaces the
miss as status=error. Deterministic: no model call.

The campaign display name is the one free-form value that reaches the external
response, so it is reduced to an inert display alphabet
(src/services/security.py) before it is stored.
"""

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT

from src.schemas.state import to_json
from src.services.security import sanitize_display_name

# An SFMC campaign id/code: short alphanumeric identifier (no spaces).
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit campaign id/code mention in the request text, EN or JA
# ("campaign id 1001" / "campaign code: SUMMER26" / "キャンペーンID 1001").
_CODE_IN_TEXT_RE = re.compile(
    r"(?:campaign)\s+(?:id|code|number)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:キャンペーンID|キャンペーンコード|キャンペーン番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Explicit triggered-send definition key mention ("definition key WELCOME-01" /
# "send key: PROMO_A" / "送信定義キー WELCOME-01").
_DEF_KEY_IN_TEXT_RE = re.compile(
    r"(?:definition|send)\s+key\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,39})"
    r"|(?:送信定義キー|定義キー)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,39})",
    re.IGNORECASE,
)
# Explicit subscriber key mention ("subscriber key SUB-1001" / "購読者キー SUB-1001").
_SUB_KEY_IN_TEXT_RE = re.compile(
    r"subscriber\s+(?:key|id)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,63})"
    r"|(?:購読者キー|購読者ID)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,63})",
    re.IGNORECASE,
)
# Quoted display name: named "Foo" / called "Foo". Curly quotes included so a
# pasted rich-text request still matches.
_NAME_QUOTED_RE = re.compile(r'(?:named|called|titled|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" field lines (ASCII or full-width colon), EN or CJK keys.
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys that are the id/name/send-target themselves, not campaign attributes.
_CODE_KEYS = ("code", "campaign code", "campaign id", "id")
_NAME_KEYS = ("name", "campaign name")
_DEF_KEYS = ("definition key", "send key", "send definition")
_SUB_KEYS = ("subscriber key", "subscriber", "subscriber id")
# Campaign-body keys mapped 1:1 onto the documented POST/PATCH /hub/v1/campaigns fields.
_DESC_KEYS = ("description", "campaign description")
_COLOR_KEYS = ("color", "colour")

# Structural caps on the assembled body: a request cannot grow the payload
# without bound by repeating "Key: value" lines.
_MAX_ATTRIBUTES = 20
_MAX_ATTRIBUTE_VALUE_CHARS = 200


class InferSalesforceFieldsNode(FunctionNode):
    """Extract entities and assemble the SFMC REST API request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> "dict[str, Any]":
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        emit_progress("Interpreting the request...")
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_campaign") or "lookup_campaign"
        campaign_hint = state.get("campaign_hint", "") or ""

        if not text.strip():
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferSalesforceFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        campaign_id = self._resolve_code(text, campaign_hint, fields)
        campaign_name = self._resolve_name(text, fields)

        if intent in ("create_campaign", "update_campaign"):
            payload = self._build_campaign_body(campaign_id, campaign_name, fields)
        elif intent == "trigger_send":
            payload = self._build_send_body(
                self._resolve_definition_key(text, campaign_hint, fields),
                self._resolve_subscriber_key(text, state.get("subscriber_key", "") or "", fields),
                fields,
            )
        else:  # lookup_campaign (read-only default)
            payload = {"campaign_id": campaign_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_salesforce_fields_complete",
            {"intent": intent, "has_campaign_id": bool(campaign_id), "n_fields": len(fields)},
            state,
        )

        return {
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "salesforce_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_code(self, text: str, campaign_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit id only: text mention > code-shaped hint > 'Code:' field. Never invented."""
        m = _CODE_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = campaign_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS and _CODE_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_name(self, text: str, fields: "list[tuple[str, str]]") -> str:
        """Return the campaign display name, reduced to the inert display alphabet.

        This value is rendered back to the caller (formatted_output and the
        confirmation line), so caller-controlled markup, quotes, colons and
        control characters are dropped rather than carried through.
        """
        m = _NAME_QUOTED_RE.search(text)
        if m:
            return sanitize_display_name(m.group(1))
        for key, value in fields:
            if key.strip().lower() in _NAME_KEYS:
                return sanitize_display_name(value)
        return ""

    def _resolve_definition_key(self, text: str, campaign_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit send-definition key only: text mention > 'Definition Key:' field >
        code-shaped hint. Never invented - an empty key is surfaced as status=error
        downstream (CallSalesforceApiNode)."""
        m = _DEF_KEY_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        for key, value in fields:
            if key.strip().lower() in _DEF_KEYS and value.strip():
                return value.strip()[:40]
        hint = campaign_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        return ""

    def _resolve_subscriber_key(self, text: str, caller_key: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit recipient key only: text mention > 'Subscriber Key:' field >
        the validated caller-supplied key."""
        m = _SUB_KEY_IN_TEXT_RE.search(text)
        if m:
            return (m.group(1) or m.group(2) or "").strip()
        for key, value in fields:
            if key.strip().lower() in _SUB_KEYS and value.strip():
                return value.strip()[:64]
        return caller_key.strip()

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] fields parsed from the request lines."""
        fields: "list[tuple[str, str]]" = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()[:_MAX_ATTRIBUTE_VALUE_CHARS]))
            if len(fields) >= _MAX_ATTRIBUTES:
                break
        return fields

    # -- payload assembly (SFMC REST API shapes) -------------------------------

    def _build_campaign_body(
        self, campaign_id: str, campaign_name: str, fields: "list[tuple[str, str]]"
    ) -> "dict[str, Any]":
        """Documented POST/PATCH /hub/v1/campaigns body: name / description /
        campaignCode / color; unmapped "Key: value" lines are carried as a
        bounded attributes list (forwarded metadata)."""
        body: "dict[str, Any]" = {}
        if campaign_name:
            body["name"] = campaign_name
        if campaign_id:
            body["campaignCode"] = campaign_id
        attributes = []
        for key, value in fields:
            k = key.strip().lower()
            if k in _CODE_KEYS + _NAME_KEYS + _DEF_KEYS + _SUB_KEYS:
                continue
            if k in _DESC_KEYS:
                body["description"] = value
            elif k in _COLOR_KEYS:
                body["color"] = value
            else:
                attributes.append({"name": key, "value": value})
        if attributes:
            body["attributes"] = attributes[:_MAX_ATTRIBUTES]
        return body

    def _build_send_body(
        self, definition_key: str, subscriber_key: str, fields: "list[tuple[str, str]]"
    ) -> "dict[str, Any]":
        """Documented POST /messaging/v1/messageDefinitionSends/key:{key}/send
        message body under "message"; the definition key rides alongside so the
        executor can address the endpoint. Recipients are addressed by
        SubscriberKey (raw emails are redacted upstream, by design)."""
        to: "dict[str, Any]" = {}
        if subscriber_key:
            to["SubscriberKey"] = subscriber_key
        attrs: "dict[str, str]" = {}
        for key, value in fields:
            k = key.strip().lower()
            if k in _CODE_KEYS + _NAME_KEYS + _DEF_KEYS + _SUB_KEYS:
                continue
            attrs[key] = value
        if attrs:
            to["ContactAttributes"] = {"SubscriberAttributes": attrs}
        return {"definition_key": definition_key, "message": {"To": to}}
