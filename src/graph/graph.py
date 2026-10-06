"""AgentCore Platform v1.0 - CMN-C2-232 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in SalesforceWorkflowGraphNode
(`main` slot), which wraps the inner SalesforceWorkflowGraph (validate ->
classify -> infer -> call -> confirm). add_edges() is NOT overridden - backbone
wiring is the framework's concern.

Configuration: the static manifest is `config/agent.yaml` (identity + compile-time
gates only, flat schema). Every RUNTIME parameter lives in `config/config.yaml`,
which the platform registry loads and passes as Graph(config=...); the standalone
server and _parent_config() read the same file so a registry-loaded agent and a
standalone one see identical settings.
"""

import math
from pathlib import Path
from typing import Any, ClassVar, Optional, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import ERROR_REASONS, PostProcessNode, _REASON_WORKFLOW_FAILED, error_envelope
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

# Runtime parameters: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Bounds for the declared runtime settings. A value outside its bound is not
# forwarded (the consumer keeps its built-in default) rather than crashing graph
# construction on a malformed configuration file.
_MAX_RETRY_MIN, _MAX_RETRY_MAX = 0, 9
_TIMEOUT_MIN, _TIMEOUT_MAX = 1, 600


def _runtime_config() -> "dict[str, Any]":
    """Read the runtime parameters from config/config.yaml.

    This is the same file the platform registry loads and passes as
    Graph(config=...); the standalone server (src/api/server.py) and
    _parent_config() read it here so every deployment shape sees identical
    configuration. Returns an empty dict - never raises - when the file is
    absent, unreadable, not valid YAML, or not a mapping (the graph then runs
    on its built-in defaults).
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast("dict[str, Any]", loaded)


def _config_int(value: Any, lo: int, hi: int) -> Optional[int]:
    """Validate a declared integer setting: a real int, finite, within [lo, hi].

    Bools, strings, floats, NaN/Infinity, and out-of-range values return None so
    the consumer keeps its documented default. A non-finite number is the
    dangerous case: NaN comparisons are always False, so a NaN retry budget
    would make every bound check silently pass.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number != int(number):
        return None
    parsed = int(number)
    return parsed if lo <= parsed <= hi else None


class SalesforceWorkflowGraphNode(GraphNode):
    """Wraps the inner Salesforce Marketing Cloud workflow graph; assigned to the
    `main` slot.

    No constructor arguments (SDK nodes are no-arg) - configuration reaches the
    subgraph via _parent_config(), which loads config/config.yaml.
    """

    def __init__(self, runtime_config: dict[str, Any] | None = None) -> None:
        """Receive the runtime config from the outer graph.

        A BaseNode has no config back-reference of its own, so the outer
        AgentBaseGraph reads `self.config` and threads it in here at
        register_nodes() time. Static construction input - not mutable state.
        """
        # A non-mapping runtime config degrades to {} instead of raising: reading and
        # parsing config/config.yaml belongs to the entry point, and this node only has
        # to survive whatever it is handed.
        self._runtime_config = dict(runtime_config) if isinstance(runtime_config, dict) else {}

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> Any:
        from src.graph.domain_workflow_graph import SalesforceWorkflowGraph

        return SalesforceWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the string handed to the inner graph's invoke().

        pre_process serialized the validated request into validated_input (a
        JSON string); structured params travel as JSON and the first inner node
        parses them back.

        Also bridges the caller's input_context to the inner graph:
        GraphNode.execute() does not forward input_context on subgraph.invoke(),
        and extract_input is the last hook in this repo's code that sees the
        outer state before the inner invoke - see src/graph/context_bridge.py.
        """
        set_caller_input_context(cast("dict[str, Any] | None", state.get("input_context")))
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            # Outer reason wins: a reason settled before the inner run is the
            # real one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "intent": sub_result.get("intent", ""),
            "campaign_id": sub_result.get("campaign_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "campaign_name": sub_result.get("campaign_name", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "salesforce_payload": sub_result.get("salesforce_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the declared runtime settings to the inner graph under
        config["configurable"].

        Reads config/config.yaml (see _runtime_config) - the live settings file.
        The `salesforce` integration section is forwarded as-is (the inner graph
        injects it into State as the JSON `salesforce_config` field); the
        numeric backbone settings are validated here (type, finiteness, range)
        so a malformed configuration file can neither crash graph construction
        nor silently disable a bound. Invalid or absent keys are simply not
        forwarded.
        """
        cfg = self._runtime_config
        configurable: "dict[str, Any]" = {}

        salesforce = cfg.get("salesforce")
        if isinstance(salesforce, dict) and salesforce:
            configurable["salesforce"] = salesforce

        declared: "dict[str, Any]" = {}
        max_retry = _config_int(cfg.get("max_retry"), _MAX_RETRY_MIN, _MAX_RETRY_MAX)
        if max_retry is not None:
            declared["max_retry"] = max_retry
        timeout_s = _config_int(cfg.get("timeout_s"), _TIMEOUT_MIN, _TIMEOUT_MAX)
        if timeout_s is not None:
            declared["timeout_s"] = timeout_s
        if declared:
            configurable["runtime"] = declared

        return {"configurable": configurable}


class SalesforceMarketingCloudAgent(AgentBaseGraph):
    """CMN-C2-232 outer graph - Salesforce Marketing Cloud Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in SalesforceWorkflowGraphNode (`main` slot); SFMC
    settings flow from config/config.yaml via _parent_config().

    Runtime configuration: the platform registry loads config/config.yaml and
    passes it as Graph(config=...); the standalone server mirrors that with
    _runtime_config(). AgentBaseGraph itself consumes max_retry from that dict,
    so a no-config construction (unit tests) simply runs on the framework
    defaults.
    """

    @property
    def name(self) -> str:
        return "cmn_c2_232"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = SalesforceWorkflowGraphNode(runtime_config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        """Surface the structured domain product on the outer invoke() return.

        AgentBaseGraph.get_output() returns only the minimal
        ``{output, status, trace_id, correlation_id, node_history}`` envelope,
        which would drop the structured result (record_id / record_ref /
        campaign_id / campaign_name / intent / confirmation) from the dict
        returned by ``agent.invoke()``. This override EXTENDS the base envelope
        (never replaces it - status/trace_id/node_history are preserved).

        Fail-closed for EVERY representation: the base envelope's ``output``
        key falls back to the raw ``result`` written by the inner workflow when
        no ``formatted_output`` exists, so on any non-success outcome that
        fallback is dropped here as well. Nothing that did not pass
        PostProcessNode's output gate reaches the caller - not the structured
        keys, and not ``output``.

        A failed invoke still reports WHY - as closed-set labels only. The
        ``error`` key carries the same envelope PostProcessNode builds
        (``error_envelope()``): a constant reason code, never an ``error_log``
        line, a gate message or an exception's text. ``error_log`` stays the
        internal audit channel and is not projected.
        """
        output = cast("dict[str, Any]", super().get_output(state))
        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        formatted = state.get("formatted_output")

        # A run that completed without performing the request carries the
        # sentence saying what to correct, not a gated product dict. The base
        # envelope already holds that sentence, and none of the structured keys
        # below apply: nothing was created, so there is no record to describe.
        # SUCCESS here reports that the run reached a defined end safely, not
        # that the request was carried out.
        if state.get("error_code"):
            return output

        if not succeeded:
            # No gated product: withhold ``output`` entirely (the base envelope
            # fell back to the pre-gate ``result`` when no formatted_output
            # exists) and the structured keys, and report the reason as the
            # closed-set envelope. PostProcessNode's own reason is kept when it
            # ran; an inner-workflow error routes straight to finalize, so it
            # is the workflow code. Anything else in that slot is not trusted.
            reason = formatted.get("reason") if isinstance(formatted, dict) else None
            if not (isinstance(reason, str) and reason in ERROR_REASONS):
                reason = _REASON_WORKFLOW_FAILED
            output["output"] = None
            output["error"] = error_envelope(reason)
            return output
        if not isinstance(formatted, dict):
            # A success status with no gated dict is not trusted - and never
            # the pre-gate ``result`` the base envelope fell back to.
            output["output"] = None
            return output
        output["formatted_output"] = formatted
        output["record_id"] = formatted.get("record_id", "")
        output["record_ref"] = formatted.get("record_ref", "")
        output["campaign_id"] = formatted.get("campaign_id", "")
        output["campaign_name"] = formatted.get("campaign_name", "")
        output["intent"] = formatted.get("intent", "")
        output["confirmation"] = formatted.get("confirmation", "")
        return output

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
