"""AgentCore Platform v1.0 - inner workflow Step 4: CallSalesforceApi (tool side-effect).

Performs the lookup/create/update/trigger-send call against the Salesforce
Marketing Cloud REST API endpoints via src/services/sfmc_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Credentials: the integration token is read via
       ctx.secrets.get("SFMC_ACCESS_TOKEN") (InvocationContext.from_state(state))
       - never os.environ, never stored in state. This build ALWAYS constructs
       the network-free simulator client, so a missing token is tolerated (a
       sentinel placeholder is used - it is never sent anywhere because no
       request leaves the process). The defensive guard below (a non-simulator
       transport with no token is a hard status=error - a real API is never
       called unauthenticated) is reachable only via the client's unit-test
       seam; live SFMC integration is explicit future code scope
       (docs/02_design.md, "Bundled build - Salesforce Marketing Cloud simulator").
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external marketing system; HTTP 4xx/5xx surfaces as
       status=error + error_log (no silent pass).

Configuration: this node takes NO constructor arguments (SDK nodes are no-arg).
SFMC settings (base_url) arrive as the JSON `salesforce_config` state field -
injected by the inner graph's _extra_initial_state() from the settings section
forwarded by SalesforceWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["salesforce"]` argument for direct invocation. The
client is constructed locally per call (no module-global mutation).
"""

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import PROCESSING_FAILED

from src.schemas.state import from_json
from src.services.sfmc_client import SfmcApiError, SfmcClient

_SECRET_KEY = "SFMC_ACCESS_TOKEN"
# Placeholder handed to the network-free simulator transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallSalesforceApiNode(FunctionNode):
    """Look up / create / update a campaign or trigger an email send via the SFMC REST API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        emit_progress("Sending the request...")
        payload = from_json(state.get("salesforce_payload"), None)
        if not payload:
            emit_progress(PROCESSING_FAILED)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallSalesforceApiNode: missing salesforce_payload"],
            }

        intent = state.get("intent", "lookup_campaign") or "lookup_campaign"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["salesforce"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("salesforce_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("salesforce") or {}
        settings.update(override)

        # Client built locally per call - ALWAYS the deterministic NETWORK-FREE
        # simulator; the client's transport ctor params are a unit-test seam and
        # no deployment path injects them.
        base_url = str(settings.get("base_url", "") or "").strip()
        client = SfmcClient(base_url=base_url) if base_url else SfmcClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Simulator limitation: no request leaves the process, so run
                # with a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                emit_progress(PROCESSING_FAILED)
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallSalesforceApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        campaign_id = state.get("campaign_id", "") or str(payload.get("campaign_id", "") or "")
        campaign_name = state.get("campaign_name", "")

        try:
            if intent == "lookup_campaign":
                if not campaign_id:
                    emit_progress(PROCESSING_FAILED)
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSalesforceApiNode: unresolved campaign id - cannot look up campaign"],
                    }
                resp = client.find_campaign(campaign_id, api_token) or {}
                if not (resp.get("id") or resp.get("campaignCode")):
                    emit_progress(PROCESSING_FAILED)
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [f"CallSalesforceApiNode: no campaign found for id {campaign_id}"],
                    }
                record_id = str(resp.get("id", "") or "") or campaign_id
                campaign_name = campaign_name or str(resp.get("name", ""))
                record_ref = f"sfmc://campaigns/{record_id}"
            elif intent in ("create_campaign", "update_campaign"):
                if intent == "update_campaign" and not campaign_id:
                    emit_progress(PROCESSING_FAILED)
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSalesforceApiNode: unresolved campaign id - cannot update campaign"],
                    }
                if intent == "create_campaign":
                    resp = client.create_campaign(payload, api_token) or {}
                else:
                    resp = client.update_campaign(campaign_id, payload, api_token) or {}
                record_id = str(resp.get("id", "") or "") or campaign_id
                record_ref = f"sfmc://campaigns/{record_id}" if record_id else ""
            elif intent == "trigger_send":
                definition_key = str(payload.get("definition_key", "") or "")
                if not definition_key:
                    emit_progress(PROCESSING_FAILED)
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallSalesforceApiNode: unresolved send-definition key - " "cannot trigger email send"
                        ],
                    }
                resp = client.trigger_send(definition_key, payload.get("message") or {}, api_token) or {}
                record_id = str(resp.get("requestId", "") or "") or definition_key
                record_ref = f"sfmc://triggered-sends/{definition_key}"
            else:
                emit_progress(PROCESSING_FAILED)
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallSalesforceApiNode: unknown intent '{intent}'"],
                }
        except SfmcApiError as exc:
            emit_progress(PROCESSING_FAILED)
            # Closed-set only: the HTTP status. The exception's message carries
            # the upstream response body verbatim and must not enter error_log.
            http_status = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallSalesforceApiNode: SFMC API error {http_status}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            emit_progress(PROCESSING_FAILED)
            # Closed-set only: the exception class, never its message.
            failure_class = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallSalesforceApiNode: SFMC call failed: {failure_class}"],
            }

        # Audit the tool side-effect - intent + presence signals only,
        # never campaign content or credentials.
        emit_trace_event(
            "call_salesforce_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "campaign_id": campaign_id or record_id,
            "campaign_name": campaign_name,
            "status": AgentStatus.SUCCESS.value,
        }
