# Template Design Specification — CMN-C2-232 Salesforce Marketing Cloud Agent

## Position in AgentCore Architecture

- **Agent Class**: `SalesforceMarketingCloudAgent` (`src/graph/graph.py`)
- **L1 Base (framework base class)**: AgentBaseGraph — direct framework inheritance
- **Category**: Cat 2 (multi-step domain workflow — tool-calling pattern; one
  pattern per template). The fixed outer backbone delegates the domain pipeline
  to an inner `BaseGraph` via a `GraphNode` in the `main` slot.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | **trust gate (VERIFIED_EXTERNAL)** + request validation (bounds, markup/control-character strip, instruction-override refusal) + the `input_context` caller-data contract; serialize the request text into `validated_input` | user_input, input_context | validated_input | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Salesforce Marketing Cloud workflow subgraph; bridge `input_context` across the boundary | validated_input, input_context | result, intent, campaign_id, record_id, record_ref, campaign_name, confirmation, salesforce_payload | GraphNode (caller ctx forwarded unchanged) | SalesforceWorkflowGraphNode (GraphNode) |
| post_process | shape the caller-facing `formatted_output`; **output gate** (payload whitelist + recursive credential scan + record-evidence requirement) | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` (fully custom linear topology). The 5
pipeline steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; re-apply the caller-data contract to the bridged `input_context`; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, campaign_hint, subscriber_key, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> lookup_campaign / create_campaign / update_campaign / trigger_send; low-confidence -> lookup_campaign (read-only default — never a write, never a send) | intent | ANONYMOUS |
| infer_salesforce_fields | 3 InferSalesforceFields | extract campaign id/code, display name (reduced to the inert display alphabet), triggered-send definition key, subscriber key, and bounded "Key: value" fields; assemble the SFMC REST request body per intent; an unresolved campaign id / definition key is left empty (never invented) | campaign_name, campaign_id, salesforce_payload | ANONYMOUS |
| call_salesforce_api | 4 CallSalesforceApi | GET /hub/v1/campaigns/{id} (lookup) / POST /hub/v1/campaigns (create) / PATCH /hub/v1/campaigns/{id} (update) / POST /messaging/v1/messageDefinitionSends/key:{key}/send (trigger send) via `SfmcClient`; token via ctx.secrets; 4xx/5xx -> status=error | record_id, record_ref, campaign_id, campaign_name | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max_retry) ^
Inner (inside main / SalesforceWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_salesforce_fields
              -> call_salesforce_api -> confirm -> END
```

**One channel per kind of data.** The request TEXT travels as a JSON string in
`validated_input` (`{"text": ...}`), which
`SalesforceWorkflowGraphNode.extract_input()` hands to the subgraph and the first
inner node parses back. The STRUCTURED caller data travels on the framework's
`input_context` channel. Nothing is carried twice, so there is no second copy to
drift.

### The input_context bridge

`GraphNode.execute()` invokes the inner graph as
`subgraph.invoke(user_input, session_id=..., ctx=...)` — it does **not** forward
the outer state's `input_context`. Without a bridge, every inner read of
`state["input_context"]` would see `{}` and the caller's campaign id would
silently do nothing. `src/graph/context_bridge.py` closes that gap with a
`ContextVar`: `extract_input()` (the last hook that sees the outer state before
the inner invoke) stashes the context, and the inner graph's
`_extra_initial_state()` seeds it back. A `ContextVar` keeps the hand-off correct
per thread/task, so concurrent invocations in one process cannot see each other's
data. `tests/proof_of_boundary/test_invoke_e2e.py` proves the bridge end to end:
a lookup succeeds only because `campaign_id` travelled on `input_context`, and
the same request without it fails closed.

**Completion is not the same as answering.** A run that ends with
`AgentStatus.SUCCESS` reports that the request was handled safely to a defined
end, not that the request was carried out. A value the caller can correct (an
out-of-contract parameter, an empty or over-long request) ends this way so the
caller receives the reason and can send a corrected request on the same
conversation; terminating instead would end the calling surface's turn and
surface only an exception type, leaving the reason reachable solely from the
audit trail. The reason travels as `error_code` in State, every later domain
node passes through without doing work once it is set, the structured output
fields are withheld, and the output boundary renders the reason as a static
caller-facing sentence.

Two classes keep terminating, and must not be folded into the above: content
the agent refuses outright (an instruction-override payload — re-sending a
reworded variant is not a correction), and a breach of a contract the caller
cannot influence.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (fields are absent until their
producer node writes them). Dict/list payloads are stored as JSON strings
(`Optional[str]`) via the module helpers `to_json` / `from_json`, used by every
producer and consumer, so checkpoint serialisation stays loss-free.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| campaign_hint | NotRequired[str] | validated caller-supplied campaign id / send-definition-key hint; never inferred | validate_input |
| campaign_id | NotRequired[str] | resolved SFMC campaign id/code (pass-through when the hint/text already carries one) | infer_salesforce_fields |
| subscriber_key | NotRequired[str] | validated caller-supplied send recipient key | validate_input |
| redaction_flags | NotRequired[Optional[str]] | JSON list of patterns redacted before logging | validate_input |
| campaign_name | NotRequired[str] | campaign display name / record label, reduced to the inert display alphabet | infer_salesforce_fields / call_salesforce_api |
| salesforce_payload | NotRequired[Optional[str]] | JSON — assembled SFMC REST API request body (stored as a JSON string via `to_json`/`from_json`) | infer_salesforce_fields |
| salesforce_config | NotRequired[Optional[str]] | JSON — the `salesforce:` settings section forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| record_id | NotRequired[str] | campaign id / send request id returned by SFMC | call_salesforce_api |
| record_ref | NotRequired[str] | human-readable record reference (`sfmc://campaigns/<id>` / `sfmc://triggered-sends/<key>`) | call_salesforce_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`user_input`, `input_context`, `intent`, `result`, `validated_input`,
`formatted_output` are inherited from `AgentState` and are **not** re-declared.

**State constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No credentials in State — the SFMC access token is accessed via `ctx.secrets`.
- `InvocationContext` read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration

Two files, two jobs — and no value lives in both:

| File | Contents | Read by |
|------|----------|---------|
| `config/agent.yaml` | the static manifest: identity (id / name / namespace / version / category / industry / base_type), the single dotted `class:` entry point, `required_trust_level`, `generation_mode`, and the compile-time `requires` gates. Flat — every key at ROOT level, no `agent:` nesting. | the registry, at discovery time |
| `config/config.yaml` | the runtime parameters: `max_retry`, `timeout_s`, and the `salesforce:` integration section | the registry (passed as `Graph(config=...)`), `src/api/server.py`, and `SalesforceWorkflowGraphNode._parent_config()` |

Nodes take **no constructor arguments** (nodes are no-arg; configuration never
rides on node instances). `_parent_config()` receives the runtime config from the outer graph and
forwards the settings to the inner graph under `config["configurable"]` — never
`{}`. Every numeric it forwards is validated there (real integer, finite, in
range); an invalid or absent key is simply not forwarded and the consumer keeps
its documented default, so a malformed configuration file can neither crash graph
construction nor silently disable a bound. The inner graph's
`_extra_initial_state()` then injects the `salesforce` section into State as the
JSON `salesforce_config` field, where `CallSalesforceApiNode.execute(state,
config=None)` reads it (an explicit `config["configurable"]["salesforce"]`
override is also honoured for direct invocation).

`requires.secrets` and `requires.extras` are both empty on purpose: they are
compile-time gates, and a declared-but-unprovisioned entry fails the agent's
compile. The bundled build constructs no model client and runs the network-free
SFMC simulator, so it requires neither. The SFMC access token is read
opportunistically at call time via `ctx.secrets.get("SFMC_ACCESS_TOKEN")`.

## Structured Product (get_output)

`AgentBaseGraph.get_output()` returns the minimal
`{output, status, trace_id, correlation_id, node_history}` envelope, which would
drop the structured domain result from `agent.invoke()`.
`SalesforceMarketingCloudAgent.get_output()` therefore **extends**
`super().get_output()` (never replaces it) and surfaces the structured keys
(`record_id`, `record_ref`, `campaign_id`, `campaign_name`, `intent`,
`confirmation`, plus the gated `formatted_output`) **only when
`status == SUCCESS`**.

It is fail-closed for **every** representation. The base envelope's `output` key
falls back to the raw `result` written by the inner workflow when no
`formatted_output` exists — content that never passed the output gate — so on any
non-success outcome that fallback is dropped as well. A failed invoke still
reports WHY: `error_log` is surfaced, reduced by `safe_error_lines()` to
caller-safe message lines (a raw entry can carry a stack trace with absolute
source paths), each naming a field and never a rejected caller value.

## Security Design

### Trust boundary

The single external trust gate is on the outer backbone:
`PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`. Every inner
domain node — **including the write-capable `CallSalesforceApiNode`** — declares
`TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
`InvocationContext` into the subgraph **unchanged** (no elevation), and
`VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
would deny a legitimate external caller before the call runs — the boundary is
therefore enforced exactly once, at `pre_process`. `src/api/server.py` enforces
the standalone entry-point Bearer-token auth boundary (`INVOKE_AUTH_TOKEN` ->
VERIFIED_EXTERNAL elevation).

### The caller contract — every input hostile until proven bounded

`PreProcessNode` owns it, and enforces it in its own `execute()` rather than
relying on any surrounding framework gate being active; the tests drive
`execute()` directly to prove that.

- **Request text**: must be present and non-blank; bounded at
  `MAX_REQUEST_CHARS`; HTML markup and control characters stripped, whitespace
  normalised.
- **Instruction-override refusal**: a deterministic pattern set refuses requests
  carrying an instruction-override form (ignore/disregard previous instructions,
  system-prompt disclosure, role reassignment, chat-markup delimiters). The rule
  is deliberately narrow and tested in BOTH directions: ordinary marketing
  requests using the same words ("send the campaign instructions", a campaign
  literally named "Ignore Previous") are unaffected. Nothing derived from a
  refused request is carried forward, and the refused text is never echoed back.
- **Structured caller data (`input_context`)**: `campaign_id`, `campaign_hint`,
  `definition_key`, `subscriber_key`, `channel` — each locked to an explicit
  inert identifier alphabet with its own length bound, and additionally refused
  if it carries a credential shape. Every field is **str-only**, so no numeric
  shape — NaN and Infinity included, which parse through `float()` and compare
  False forever — can reach the pipeline through a permissive coercion. A
  violation fails **CLOSED** naming the FIELD and the expected shape, never the
  rejected value. Undeclared keys are ignored.
- **Why the alphabet, not a sanitizer**: the framework's PII masking covers
  `user_input` / `validated_input` only — never the structured context channel.
  The identifier alphabet is what keeps that channel clean: an address, a bearer
  token or a markup fragment cannot validate, so it can neither be carried
  forward nor rendered back.
- **Structural limits**: the entry-point adapter caps the serialized
  `input_context` size (413); parsed "Key: value" entries and their values are
  capped in `InferSalesforceFieldsNode`.

The template holds these guarantees itself. Where the surrounding runtime also
rejects a hostile payload, that is defence in depth, not the guarantee.

### Input redaction

`ValidateInputNode.execute()` runs a deterministic (regex, not model-based) scan
for email addresses and access-token-like strings (`eyJ...`, `secret_...`,
`sk-...`) and redacts them before any logging; `redaction_flags` records what was
redacted. Recipients are addressed by **subscriber key / send-definition key**,
never by raw email address, so redacting a pasted email does not break a
legitimate request; this is flag-and-redact for safe logging, not a hard reject.
The framework's input gate additionally masks emails/phones/names in
`user_input`/`validated_input`. The only deterministic auto-reject at this step is
the empty/non-request guard.

### The output boundary

The domain output gate is the **module-level** `_security_gate_output()` in
`src/nodes/post_process_node.py`, called inline from
`PostProcessNode.execute()`. Two reasons it is not a framework hook:
`_security_gate_output` is `@final` on `FunctionNode` and cannot be overridden at
all (a TypeError at class definition — asserted by TC-06/TC-07), and the
`_extra_security_gate_output(result) -> result` hook can only pass a result
through or raise, whereas this gate must TURN a would-be success into a
structured `status=error` carrying its own `error_log`. A module-level helper
also makes the rule testable as a plain function, independent of any node. No
node defines `_extra_security_gate_input/_output` instance methods.

The stated invariant, and how it is enforced for EVERY representation:

1. **The raw request body never reaches the caller.**
   `InferSalesforceFieldsNode` copies arbitrary "Key: value" request fields into
   nested `attributes` / `ContactAttributes.SubscriberAttributes` containers, so
   the assembled body can carry any string the requester typed. Only the
   whitelisted SCALAR addressing fields (`campaign_id` / `campaignCode` /
   `definition_key` — code-shaped by construction upstream) pass the fail-closed
   `_vetted_payload_fields()` helper; free-text bodies and nested containers are
   dropped entirely.
2. **No credential-shaped string appears anywhere in a response.** Every value is
   scanned RECURSIVELY, so a surfaced container cannot smuggle one past the gate.
   The scan runs on the success path AND the error path — an error envelope
   carries `error_log` text, which is output too. One definition of "credential
   shape" lives in `src/services/security.py` and is used by the gate and by the
   invoke envelope alike; its thresholds are at least as sensitive as the inbound
   redaction, so nothing the input scan would have caught is missed on the way
   out.
3. **A SUCCESS response always carries record evidence** (`record_id` /
   `record_ref`), so the outcome of an SFMC action is never misrepresented.
4. **When the gate blocks, nothing pre-gate is surfaced.** The blocked response
   also clears the inner `result` — the envelope falls back to `result` for its
   `output` key when no `formatted_output` exists, so leaving it in place would
   hand the caller exactly the content the gate just refused.
5. **The one free-form value that renders back is bounded.** A campaign display
   name is reduced to an inert display alphabet (word characters, spaces, a small
   punctuation set) and length-capped before it is stored, so caller-controlled
   markup, quotes, colons, at-signs and newlines cannot ride into the response.
6. **Internal error text is reduced before it becomes output.** `safe_error_lines()`
   keeps the first line of each entry (the remainder can be a stack trace with
   absolute source paths), drops trace fragments and credential shapes,
   truncates, de-duplicates and caps the count.

**No monetary aggregates are rendered.** This template returns record
identifiers, references, a campaign display name and a confirmation line — it
computes and renders no monetary figures, so a numeric-precision rendering grid
is **not applicable** here. The invariant this template does state is the one
above, and it is what the output gate enforces. Boundary tests assert both
directions: leak forms are blocked, and structural identifiers (campaign codes
such as `SUMMER-26`, send-definition keys such as `WELCOME-01`, `sfmc://`
references) survive byte-identical.

### Credentials

The integration token is read via `ctx.secrets.get("SFMC_ACCESS_TOKEN")`
(`InvocationContext.from_state(state)`), never `os.environ`, never stored in
State or logs. It is **not** declared in `requires.secrets`: that list is a
compile-time gate, and the bundled build's network-free simulator runs without a
credential (no request leaves the process), so declaring it would make the agent
fail to compile wherever the secret is not provisioned. The client keeps a
defensive guard — a non-simulator transport with no token is a hard
`status=error`, a real API is never called unauthenticated — reachable only via
the unit-test seam. `INVOKE_AUTH_TOKEN` is a deployment-level caller credential
(the entry-point exception), not an agent secret.

### Audit

Every node's `execute()` emits exactly one positional
`emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
SUCCESS path (intent / presence signals only — never request text, campaign
content, or credentials); `PreProcessNode` additionally emits
`input_validation_failed` with a reason code on each rejection path.
`__call__()` is never overridden. Event names (documented for operations):

| Node | Event |
|------|-------|
| pre_process | `pre_process_complete`, `input_validation_failed` |
| validate_input | `validate_input_complete` |
| classify_intent | `classify_intent_complete` |
| infer_salesforce_fields | `infer_salesforce_fields_complete` |
| call_salesforce_api | `call_salesforce_api_complete` |
| confirm | `confirm_complete` |
| post_process | `post_process_complete` |

## Deterministic pipeline

The pipeline is fully deterministic: intent classification (`ClassifyIntentNode`)
uses a keyword rule and field inference (`InferSalesforceFieldsNode`) uses
regex/line-structure extraction, so the template runs and tests without a live
model. **No model client is constructed anywhere** and no system prompt is read
(no dead config) — which is why the manifest declares
`generation_mode: deterministic` and an empty `requires.extras`. Model-backed
synthesis (richer intent classification, free-text-to-field mapping,
natural-language campaign summaries) is an additive follow-up that requires no
graph-shape change.

## Bundled build — Salesforce Marketing Cloud simulator (explicit)

`src/services/sfmc_client.py` is a deterministic, **network-free SFMC simulator**.
Its built-in transport returns the documented SFMC REST API response shapes (a
campaign object for lookups; a campaign object with a synthetic id echo for
create/update, derived from the request; the `requestId`/`responses`
triggered-send receipt for sends) so the pipeline is runnable and testable
without a live SFMC tenant or an HTTP client library. It does **not** perform a
live SFMC call. The rule it follows: never fake a live call — document the
limitation.

**No deployment path reaches a live SFMC tenant in this build.**
`CallSalesforceApiNode.execute()` always constructs the simulator client —
neither injecting transports nor provisioning `SFMC_ACCESS_TOKEN` changes that.
The constructor transport parameters (`post`/`patch`/`get`) exist **only as the
unit-test seam** (deterministic fault/response injection in
`tests/unit/test_sfmc_client.py` / `test_call_salesforce_api_node.py`). Live SFMC
integration is explicit future code scope: a reviewed change set adds the real
transports and their provisioning path — it is not a configuration flip. The
method contracts and payload shapes are already SFMC REST API exact
(`/hub/v1/campaigns`, `/messaging/v1/messageDefinitionSends/key:{key}/send`),
which keeps that scope small.

## Framework Utilization

### Shared components used
- [x] `InvocationContext` — read in `CallSalesforceApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallSalesforceApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("SFMC_ACCESS_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start / node_complete / node_error) are NOT re-emitted

### Composition pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `SalesforceWorkflowGraph` (`BaseGraph`) via `SalesforceWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `SalesforceWorkflowGraphNode._parent_config()` receives the runtime config from the outer graph (threaded in at `register_nodes()` time from `self.config`) and forwards `{salesforce, runtime}` under `config["configurable"]` to the subgraph.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures (no silent pass).

## Entry Points

The agent is reachable through three entry points, all of which build the graph
from the same `config/config.yaml`:

| Entry point | Construction | Notes |
|---|---|---|
| Platform registry | `Graph(config=...)` by the registry | Reads `config/config.yaml` itself |
| Standalone HTTP (`src/api/server.py`) | Loads `config/config.yaml`, passes `Graph(config=...)` | Caller-auth boundary; see Security Design |
| Marketplace (`cli.py`) | `run_agent_marketplace(...)` is handed the graph class and the resolved config | The runner constructs the graph itself, so `cli.py` resolves `config/config.yaml` with `load_agent_config()` and passes it in; `extend_config` is the seam for deployment-specific overrides |

`cli.py` sits at the repository root because the deployment image starts it as
`CMD ["python", "cli.py"]`. It adds no business logic: graph construction,
lifecycle, secret provisioning and the invocation loop belong to
`run_agent_marketplace()`.

## Caller-Facing Events

Nodes report progress and rejection reasons to the caller as non-terminal
events, so a caller watching a run sees the pipeline advance instead of a
silent wait, and learns what to change when a request is refused.

- **Progress** — each node reports its phase at the top of `execute()`.
- **Rejection reason** — a node that returns `status: error` sends the reason
  first. It has to happen there: once the run carries an error status the
  framework skips `execute()` on every later node, so no downstream node could
  send it. Wording separates what the caller can fix (missing question,
  oversized request, malformed value) from what they cannot (retrieval or
  output failures), so a caller is not invited into a pointless retry.

Both are best-effort: the emitter is resolved lazily and failures are
swallowed, because reporting must never change the outcome of a run. Messages
are static phase and reason labels — no request value, record value or
internal identifier is ever included, since these events leave the process and
are not covered by the S-3 output gate. Terminal delivery (success/failure)
belongs to the platform runner alone.

## Import Isolation Confirmation
- [x] Template imports the public `framework/` and `shared/` surface only; no platform SDK import anywhere
- [x] `src/services/sfmc_client.py` and `src/services/security.py` have no framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat single main node | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat — the 5 domain steps live in the inner graph |
| Caller-data channel | duplicate structured data into the text envelope | one channel per kind of data + a ContextVar bridge | one channel + bridge | no second copy to drift; the bridge is proven load-bearing end to end |
| Model dependency | model client in the bundled build | deterministic build, model synthesis as an additive follow-up | deterministic | template runs/tests without a live model; no dead prompt/config reads |
| SFMC client | live HTTP call | deterministic network-free simulator; ctor transport params = unit-test seam only | simulator-only | no live network in the bundled build; live integration is explicit future code scope |
| Node configuration | ctor-arg dependency injection | no-arg nodes + settings forwarded via `_parent_config()` -> `configurable` -> state | no-arg nodes | nodes are no-arg (ctor args raise TypeError at graph build); one settings file stays the single source |
| Runtime settings home | keep them in the manifest | move them to `config/config.yaml` | `config/config.yaml` | the manifest is flat and carries no settings block, so a reader there would return `{}` and every declared value would go dead |
| Write/send target | infer campaign id / send key from the text freely | caller-supplied or explicitly stated id/key only; unresolved left empty | explicit only | never write to the wrong campaign or fire the wrong send; unresolved -> status=error, not invented |
| Recipient addressing | raw email addresses in the request | subscriber key / send-definition key only | subscriber key only | pasted emails are redacted before logging; a send addressed by SubscriberKey means redaction never breaks a legitimate request |
| Default intent | create_campaign / trigger_send | lookup_campaign | lookup_campaign | low-confidence classification must never default to a write or an email send |
| Structured product | replace get_output() | extend super().get_output(), SUCCESS-only keys | extend, SUCCESS-only | keeps the base envelope (status/trace_id/node_history) and stays fail-closed |
| Failure reporting | return status only | return status + caller-safe error lines | status + reduced error lines | a caller who sends a malformed field learns which field, with no stack trace or path leaking |
