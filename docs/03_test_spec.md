# Test Specification - CMN-C2-232 Salesforce Marketing Cloud Agent

## Test Strategy

- Test types: Unit (per node + service + config + inner graph), Integration (the
  invoke envelope contract), Proof-of-Boundary (full outer-graph invoke, real
  ASGI `/invoke` end-to-end, import isolation, state safety, server boot, HITL
  stub).
- Location: `tests/unit/`, `tests/integration/`, `tests/proof_of_boundary/`.
- The SFMC call is exercised through the deterministic, network-free simulator
  transport (default) and through injected fake transports; no live SFMC call is
  ever made.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - the framework's `__call__` routes the full security pipeline
  (trust gate -> input gate -> `execute()` -> output gate) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for `PreProcessNode` (the single external
  gate) and `TrustLevel.ANONYMOUS.value` for every other node. The trust-denial
  test asserts on the RETURNED error dict (`status == AgentStatus.ERROR.value`,
  execute-only keys absent) - `__call__` never raises for a trust denial.
- **Deliberate exceptions to that canon** (each one is the point of its test):
  - the refusal tests in `test_pre_process_node.py` call `execute()` DIRECTLY,
    with no framework wrapper in front, because a refusal that only happens when
    a surrounding gate is active is not a guarantee the template holds;
  - `test_post_process_node.py`'s error-envelope tests call `execute()` directly
    because the framework short-circuits before `execute()` on an already-errored
    state, so that branch is unreachable through `__call__`;
  - `CallSalesforceApiNode.execute(state, config=...)` takes a second argument
    that `__call__` cannot forward.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history`, extended by this template's
  `get_output()` with the structured product on success and with caller-safe
  `error_log` lines on failure. Status is compared to `AgentStatus.SUCCESS`/
  `.value` (lowercase `success`/`error`). Security assertions are BEHAVIOURAL
  (error status, nothing carried forward, nothing surfaced) - never the wording
  of a framework gate's message.
- Framework pipeline behaviours the suite encodes: `__call__` short-circuits on
  an incoming errored state (`execute()` is skipped; error status/error_log pass
  through); the framework input gate masks Title-Case bigrams (across newlines),
  emails, and digit groups in `user_input`/`validated_input` to `[MASKED]` before
  `execute()` sees the text, so positive payloads are written PII-free and
  intentional-PII tests assert the `[MASKED]` path; the framework does NOT mask
  the `input_context` channel, which is why that channel is locked to inert
  identifier alphabets instead.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS an error dict (execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | the caller contract: request-text serialization, markup/control-character stripping, length bound, instruction-override refusal (both directions), `input_context` validation field by field | text serialized as `{"text": ...}`; over-long request rejected; hostile forms refused with nothing carried forward while ordinary requests using the same words pass; malformed / non-string / credential-shaped context fails CLOSED naming the FIELD, never the value |
| U-03 | test_validate_input_node.py | empty/short guard; JSON envelope; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`); re-application of the caller contract inside the inner graph | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); bridged `input_context` yields campaign_hint/subscriber_key; malformed context fails closed here too; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_campaign / create_campaign / update_campaign / trigger_send (keyword, send-first priority, read-only default) | correct intent per keyword; send wins over a write; no-signal defaults to lookup_campaign with a non-fatal note; empty -> error; audit emits the intent label only |
| U-05 | test_infer_salesforce_fields_node.py | campaign id/code resolution (text > code-shaped hint; never invented); quoted display name reduced to the inert display alphabet and length-capped; `Key: value` fields with entry and value caps; SFMC REST body per intent | lookup `{campaign_id}`; create/update campaign body (name/campaignCode/description/attributes); trigger_send message body with definition key and the validated caller subscriber key; unresolved id/key left `""`; empty input -> error |
| U-06 | test_call_salesforce_api_node.py | lookup/create/update/trigger-send through the network-free simulator; `salesforce_config` state field + `execute(state, config=...)` override; API error / unresolved id / unresolved definition key / unknown intent / missing payload; secret posture | record_id/record_ref on success (`sfmc://campaigns/<id>` / `sfmc://triggered-sends/<key>`); 403 surfaces in error_log; live transport + no secret -> error; live transport + bound secret -> token passed to the client; audit emits presence signals |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; name fallback | "Retrieved/Created/Updated marketing campaign ... / Triggered email send ... ref=... id=..."; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping; the output gate (module-level `_security_gate_output()` helper, tested through the node AND directly): payload whitelist, recursive nested-string credential scan, record-evidence requirement, blocked-response fail-closure, error-envelope reduction | only vetted scalar payload fields surface; a credential nested anywhere blocks; SUCCESS without record evidence blocks; a blocked response clears `result`; error envelopes carry caller-safe message lines only |
| U-09 | test_sfmc_client.py | SFMC REST client: find/create/update campaign + trigger send; `Authorization: Bearer` header; `SfmcApiError` on non-2xx; simulator response shapes; `uses_stub_transport` | correct URLs (`/hub/v1/campaigns`, `/messaging/v1/messageDefinitionSends/key:{key}/send`) / headers / bodies; 400/404 raise; simulator shapes deterministic |
| U-10 | test_config.py | `config/agent.yaml` (flat manifest) + `config/config.yaml` (runtime parameters) + the declared-numeric parser | id CMN-C2-232, namespace `cmn`, Cat 2, CMN, ToolCallingAgent, single dotted `class:`, no `agent:` nesting, VERIFIED_EXTERNAL, `generation_mode: deterministic`, empty `requires`; runtime file carries max_retry / timeout_s / salesforce.base_url and those values REACH the inner graph; a bool / string / fractional / out-of-range / NaN / Infinity setting is rejected and simply not forwarded rather than crashing graph construction |
| U-11 | test_domain_workflow_graph.py | inner `SalesforceWorkflowGraph`: identity, `_extra_initial_state()` (salesforce_config JSON + bridged input_context), `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the simulator | name/state_schema correct; settings forwarded as a JSON string; bridged caller context seeded; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |
| U-12 | test_security_service.py | the shared input/output rules on their own: `sanitize_query`, `find_injection` (both directions), `sanitize_display_name`, `contains_credential_like`, `safe_error_lines` | markup/control characters stripped and length capped; override forms recognised while ordinary marketing phrasing is not; display names reduced to the inert alphabet and capped; credential shapes found at any nesting depth; error entries reduced to caller-safe first lines, de-duplicated and count-capped |
| U-13 | test_framework_compliance_tc06_tc07.py | TC-06 / TC-07: the framework's input/output gate methods cannot be overridden by a domain node | overriding either raises TypeError at class definition |

## Integration Tests (`tests/integration/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| I-01 | test_get_output_contract.py | the invoke envelope: the structured product is surfaced only from the gated output, and the envelope is fail-closed for every representation | success extends the base envelope with the gated keys; on any non-success the structured keys are withheld, `output` never falls back to the pre-gate `result`, a non-dict `formatted_output` is not trusted, and failures report caller-safe `error_log` lines with no stack trace |

## Marketplace Entry Point — `tests/unit/test_cli_entry_point.py`

| ID | Case | Expected |
|----|------|----------|
| CLI-01 | `cli.py` imports | module loads; `run_agent_marketplace`, `load_agent_config` and `SalesforceMarketingCloudAgent` are present |
| CLI-02 | override seam ships empty | `extend_config == {}`; a stray value would silently outrank `config/config.yaml` on the Marketplace path only |
| CLI-03 | the runner receives what the image's CMD would send | executing `cli.py` as `__main__` with the runner replaced captures the call: the graph class, `agent_name`, `namespace`, and every value declared in `config/config.yaml`. Loading the module alone never runs that block, so a wrong class or a dropped config there would otherwise ship unnoticed |

`cli.py` is imported by no other module, so nothing else in the suite would
notice if its import path, graph class or config assembly broke; the image
would build and fail only when the Pod starts. Skipped where the platform
events package is absent.

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no import of the platform SDK package |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json["input"]` (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, SalesforceWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]` (`intent=create_campaign`); ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not a crash |
| PB (invoke E2E) | The real request boundary | test_invoke_e2e.py | Through the real ASGI `POST /invoke` with Bearer auth: create / lookup / update / trigger-send produce real record evidence; the caller's `campaign_id` reaches the inner pipeline through the context bridge and the same request WITHOUT it fails closed; ambiguous requests fall back to the read-only lookup; every malformed `input_context` shape (charset, length, int, bool, NaN, Infinity, object) rejects with the field named and the value never echoed; an instruction-override request is refused; oversized `input_context` -> 413; wrong token -> 401; no raw request body, credential shape, markup or stack trace appears anywhere in a response; identifiers survive byte-identical |
| PB-5 | `test_state_safety.py` | **Auto-waived — checkpointing disabled** (`config/config.yaml` enables neither `memory_enabled` nor `hitl.enabled`); the conditional gate and the non-lossy traversal helper ship with the stub |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (no graph class declares `propagate_hitl=True`; no cross-boundary `interrupt()` checkpoint): class-level skipif; the stub body raises a real AssertionError so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (pre_process / validate / classify / call nodes assert on the event payload,
> `call.args[1]`). PB-3 (live external service) is not exercised: the bundled
> transport is the documented network-free simulator.

## Test Execution Summary

> **Pending re-run.** The figures below predate the Marketplace entry point work.
> The entry-point test and the PB-5 / PB-6 additions were added after this run and
> have not been executed locally — the framework wheel is not installed in the
> authoring environment. **They are not yet verified anywhere**; this summary is
> updated once a pipeline run has executed them.

- Runner: `python -m pytest tests/ -v` against the published
  `agenticstar-agentcore[anthropic]==1.0.1` wheel.
- Total tests: 275
- Pass: 274 / Fail: 0 / Skip: 1 (PB-7 - auto-waived, non-HITL)
