# Salesforce Marketing Cloud Operations Agent

AI agent for running Salesforce Marketing Cloud campaign and audience tasks, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Common
> **Template ID**: CMN-C2-232

## Overview

Turns a plain-language marketing instruction into a structured Salesforce Marketing Cloud (SFMC)
operation and executes it: look up a campaign, create one, update one, or trigger a transactional
email send. Each request is validated and bounded, classified into exactly one operation with a
deterministic keyword rule (a send outranks a write, and anything ambiguous falls back to the
read-only lookup — never a guessed write and never a guessed send), and the campaign id, display
name, send-definition key and `Key: value` attribute lines are extracted from the request text or
from explicitly supplied caller data. Targets are resolved **only** from an explicit mention: an
unresolved campaign id or send-definition key is left empty and surfaced as an error rather than
invented, so the agent cannot write to the wrong campaign or fire the wrong send. The assembled
request body follows the documented SFMC REST shapes (`/hub/v1/campaigns`,
`/messaging/v1/messageDefinitionSends/key:{key}/send`), and the caller gets back the affected
record id, a `sfmc://` reference and a human-readable confirmation.

The bundled SFMC connector is a deterministic, network-free simulator, so the whole pipeline runs
and tests end-to-end out of the box without a live tenant; a real deployment replaces it with live
transports behind the same client contract.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent
fails at graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design and operational documentation
```

See `docs/` for the design specification and test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the bundled SFMC simulator transport with live transports for your own tenant.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
