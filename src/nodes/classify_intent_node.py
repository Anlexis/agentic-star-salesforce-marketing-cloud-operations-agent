"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_campaign /
create_campaign / update_campaign / trigger_send with a deterministic keyword
rule, so the template is testable and runnable without a live model.
Low-confidence / unknown falls back to the read-only "lookup_campaign" default
with a note - never a write and never an email send.
"""

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT

_VALID_INTENTS = ("lookup_campaign", "create_campaign", "update_campaign", "trigger_send")

# Deterministic keyword signals (checked in priority order: the send first - it
# is the highest-impact side effect - then the writes, so a "create the campaign
# and send it" style request classifies as the send; lookup last).
_KEYWORDS = (
    ("trigger_send", ("send", "trigger", "blast", "dispatch", "fire off", "配信", "送信")),
    (
        "update_campaign",
        ("update", "change", "edit", "modify", "rename", "pause", "amend", "revise", "correct", "更新", "変更", "修正"),
    ),
    (
        "create_campaign",
        (
            "create",
            "register",
            "new campaign",
            "set up",
            "add a campaign",
            "build a campaign",
            "launch a",
            "作成",
            "新規",
            "登録",
            "追加",
        ),
    ),
    (
        "lookup_campaign",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "what is",
            "status of",
            "summarize",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into an SFMC marketing operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
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

        emit_progress("Classifying the request...")
        text = state.get("validated_input", "") or ""
        if not text:
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_campaign (read-only)"]
            intent = "lookup_campaign"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result: "dict[str, Any]" = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
