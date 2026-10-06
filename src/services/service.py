"""AgentCore Platform v1.0"""

# Service layer: domain queries, external API wrappers, data aggregation.
# Must NOT contain business logic, routing, or credentials.
# Nodes call this; this calls the platform's shared services for external
# integrations.
#
# Contract: CMN-C2-232's external integration is the Salesforce Marketing Cloud
# REST client in src/services/sfmc_client.py, which CallSalesforceApiNode uses
# directly. `Service` is the standard service-layer seam and is retained
# deliberately — a deployment that needs a second backend (a campaign catalog,
# an audience store) implements fetch() here behind the same node contract.
# Kept as an explicit NotImplementedError stub so the unimplemented contract
# stays visible rather than silently returning empty data.

from __future__ import annotations

from typing import Any


class Service:
    """Domain service (seam — see the module retention note above)."""

    async def fetch(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch domain data for the given query.

        Not implemented in the bundled build — the SFMC integration is handled
        by src/services/sfmc_client.py.
        """
        raise NotImplementedError("Service.fetch() is a deliberate stub in the bundled build")
