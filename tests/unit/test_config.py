# CMN-C2-232 - Unit tests: configuration files.
#
# Two files, two jobs:
#   config/agent.yaml   the static manifest the registry reads to discover and
#                       instantiate the agent - flat, every key at root level
#   config/config.yaml  the runtime parameters the registry passes as
#                       Graph(config=...), which src/graph/graph.py also reads
#                       directly for the standalone deployment

import pathlib

import pytest

from src.graph.graph import _MAX_RETRY_MAX, _MAX_RETRY_MIN, _config_int

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text(encoding="utf-8"))


class TestManifest:
    def test_identity(self):
        data = _manifest()
        assert data["id"] == "CMN-C2-232"
        assert data["name"] == "SalesforceMarketingCloudAgent"
        assert data["namespace"] == "cmn"
        assert data["category"] == "Cat 2"
        assert data["industry"] == "CMN"
        assert data["base_type"] == "ToolCallingAgent"
        assert data["enabled"] is True

    def test_keys_are_flat(self):
        """The registry reads every key at ROOT level - no `agent:` nesting and
        no split module:/class: entry point."""
        data = _manifest()
        assert "agent" not in data
        assert "module" not in data
        assert data["class"] == "src.graph.graph.SalesforceMarketingCloudAgent"

    def test_entry_trust_level(self):
        # Agent-level entry trust, enforced by the outer pre_process gate;
        # inner domain nodes stay ANONYMOUS.
        assert _manifest()["required_trust_level"] == "VERIFIED_EXTERNAL"

    def test_generation_mode_matches_the_implementation(self):
        """The pipeline is deterministic - no model client is constructed
        anywhere - so the manifest must not declare an llm generation mode."""
        assert _manifest()["generation_mode"] == "deterministic"

    def test_compile_time_requirements_are_empty(self):
        """`requires` is a compile-time gate: a declared secret or extra must be
        provisioned or the agent fails to compile. The bundled build runs the
        network-free SFMC simulator and constructs no model client, so both
        lists are empty. The SFMC token is read opportunistically via
        ctx.secrets at call time (CallSalesforceApiNode) and is not required."""
        requires = _manifest()["requires"]
        assert requires["secrets"] == []
        assert requires["extras"] == []


class TestRuntimeConfig:
    def test_backbone_parameters(self):
        cfg = _runtime()
        assert isinstance(cfg["max_retry"], int)
        assert isinstance(cfg["timeout_s"], int)

    def test_salesforce_section_is_the_live_settings_source(self):
        """`salesforce` lives in the runtime file - the manifest carries
        identity and compile-time gates only. src/graph/graph.py reads this
        file, so a reader of the config knows where the value takes effect."""
        cfg = _runtime()
        assert cfg["salesforce"]["base_url"] == "https://mc.rest.marketingcloudapis.com"

    def test_runtime_settings_reach_the_inner_graph(self):
        """Declared values must not be dead: _parent_config() reads this same
        file and forwards it to the subgraph."""
        from src.graph.graph import SalesforceWorkflowGraphNode

        from src.graph.graph import _runtime_config

        configurable = SalesforceWorkflowGraphNode(runtime_config=_runtime_config())._parent_config()["configurable"]
        assert configurable["salesforce"] == _runtime()["salesforce"]
        assert configurable["runtime"]["max_retry"] == _runtime()["max_retry"]
        assert configurable["runtime"]["timeout_s"] == _runtime()["timeout_s"]


class TestDeclaredNumericValidation:
    """The only numerics this template reads are the declared runtime settings,
    and each goes through the same finite+bounded parser before it is
    forwarded. A non-finite value is the dangerous case: NaN comparisons are
    always False, so an unchecked bound would silently stop bounding."""

    @pytest.mark.parametrize("value", [_MAX_RETRY_MIN, 3, _MAX_RETRY_MAX, 3.0])
    def test_accepts_in_range_integers(self, value):
        assert _config_int(value, _MAX_RETRY_MIN, _MAX_RETRY_MAX) == int(value)

    @pytest.mark.parametrize(
        "value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            False,
            "3",
            "NaN",
            None,
            [3],
            {"n": 3},
            3.5,
            _MAX_RETRY_MIN - 1,
            _MAX_RETRY_MAX + 1,
        ],
        ids=[
            "nan",
            "inf",
            "neg-inf",
            "true",
            "false",
            "str",
            "str-nan",
            "none",
            "list",
            "dict",
            "fractional",
            "under-range",
            "over-range",
        ],
    )
    def test_rejects_everything_else(self, value):
        assert _config_int(value, _MAX_RETRY_MIN, _MAX_RETRY_MAX) is None

    def test_a_rejected_setting_is_simply_not_forwarded(self, monkeypatch):
        """A malformed configuration file must not crash graph construction -
        the consumer keeps its documented default instead."""
        from src.graph import graph as graph_module

        malformed = {"max_retry": float("nan"), "timeout_s": 30}
        configurable = graph_module.SalesforceWorkflowGraphNode(
            runtime_config=malformed
        )._parent_config()["configurable"]
        assert "max_retry" not in configurable.get("runtime", {})
        assert configurable["runtime"]["timeout_s"] == 30
