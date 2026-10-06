"""AgentCore Platform v1.0 - Salesforce Marketing Cloud REST API client.

Service layer: a thin wrapper around the Salesforce Marketing Cloud (SFMC)
REST API campaign + triggered-send endpoints. Contains NO business logic, NO
routing, and NO credentials - the access token is passed in per call by the
node (which reads it via ctx.secrets). This module imports no framework
internals - pure stdlib.

BUNDLED BUILD SCOPE (deliberate, documented):
    The bundled build is a deterministic, NETWORK-FREE SFMC SIMULATOR - period.
    The built-in simulator transport below is the only transport a deployment
    ever runs: it returns the documented SFMC response shapes (a campaign
    object for lookups; a campaign object with a synthetic ``id`` echo for
    create/update, derived from the request; the ``requestId``/``responses``
    triggered-send receipt for sends) so the pipeline is runnable and testable
    without a live SFMC tenant or an HTTP client library - it does NOT perform
    a live SFMC call. The rule it follows: never fake a live call; document the
    limitation.

    The constructor ``post`` / ``patch`` / ``get`` parameters are the
    UNIT-TEST SEAM only (deterministic fault/response injection) - no
    deployment path injects them; CallSalesforceApiNode always constructs
    this client without transports. Live SFMC integration is explicit future
    code scope: a reviewed change set adds the real transports and their
    provisioning. The method contracts and payload shapes are already SFMC
    REST API exact (``/hub/v1/campaigns``,
    ``/messaging/v1/messageDefinitionSends``), which keeps that scope small.
    The defensive guard in CallSalesforceApiNode (a non-simulator transport
    without a token is a hard error) stays and is reachable only via the test
    seam.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, str]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

# SFMC tenant-specific REST base URL - the real value carries the tenant
# subdomain (https://<subdomain>.rest.marketingcloudapis.com) and is set via
# `salesforce.base_url` in config/config.yaml.
_BASE_URL = "https://mc.rest.marketingcloudapis.com"


class SfmcApiError(Exception):
    """Raised when the SFMC REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"SFMC API error {status_code}: {message}")


class SfmcClient:
    """Salesforce Marketing Cloud REST API campaign / triggered-send client.

    Args:
        base_url: SFMC tenant REST base URL (default
            https://mc.rest.marketingcloudapis.com; the real tenant subdomain
            comes from `salesforce.base_url` in config/config.yaml).
        post/patch/get: optional injected transports - the UNIT-TEST seam
            only (no deployment path injects them). When none is injected,
            the deterministic NETWORK-FREE simulator is used (see the module
            docstring - it returns the documented shape without a live SFMC
            call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
        patch: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post
        self._patch = patch
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when no transport is injected (the network-free simulator - the
        only mode a deployment reaches; injection is the unit-test seam)."""
        return self._post is None and self._patch is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the SFMC REST API auth headers (OAuth2 bearer access token).

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_token,
        }

    # -- deterministic simulator transport (default; NO network) --------------

    def _stub_transport(
        self, url: str, headers: "dict[str, str]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free simulator - returns the documented SFMC shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the
        simulator scope (transport injection = unit-test seam; live SFMC =
        explicit future code scope).
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if json_body.get("_sfmc_op") == "lookup":
            campaign_id = str(json_body.get("campaign_id", "")) or f"c-{digest[:8]}"
            # Documented GET /hub/v1/campaigns/{id} shape: the campaign object.
            return 200, {
                "id": campaign_id,
                "name": f"Campaign {campaign_id}",
                "campaignCode": campaign_id,
                "description": "",
                "_stub": True,  # marks the network-free simulator response
            }
        if "/messageDefinitionSends/" in url:
            # Documented triggered-send receipt shape:
            # {"requestId": ..., "responses": [{"recipientSendId": ..., "hasErrors": false}]}
            return 202, {
                "requestId": digest[:12],
                "responses": [{"recipientSendId": digest[12:24], "hasErrors": False}],
                "_stub": True,  # marks the network-free simulator response
            }
        # POST /hub/v1/campaigns (create) / PATCH /hub/v1/campaigns/{id}
        # (update) - documented campaign-object echo, plus a synthetic id
        # (derived from the request) so the caller can reference the affected
        # record without a follow-up lookup.
        campaign_id = str(json_body.get("campaignCode", "")) or f"c-{digest[:8]}"
        return 200, {
            "id": campaign_id,
            "name": str(json_body.get("name", "")),
            "campaignCode": str(json_body.get("campaignCode", "")),
            "_stub": True,  # marks the network-free simulator response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def find_campaign(self, campaign_id: str, api_token: str) -> "dict[str, Any]":
        """GET /hub/v1/campaigns/{id} - look up a campaign by id/code.

        Returns the parsed campaign object dict. Raises SfmcApiError on a
        non-2xx status.
        """
        url = f"{self._base_url}/hub/v1/campaigns/{campaign_id}"
        transport = self._resolve(self._get)
        status, body = transport(url, self._headers(api_token), {"_sfmc_op": "lookup", "campaign_id": campaign_id})
        if not (200 <= status < 300):
            raise SfmcApiError(status, _err_message(body))
        return body

    def create_campaign(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /hub/v1/campaigns - create a new marketing campaign object.

        ``payload`` is the documented campaign body (``name`` /
        ``description`` / ``campaignCode`` / ``color``). Returns the parsed
        response dict (the created campaign object). Raises SfmcApiError on a
        non-2xx status.
        """
        url = f"{self._base_url}/hub/v1/campaigns"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise SfmcApiError(status, _err_message(body))
        return body

    def update_campaign(self, campaign_id: str, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """PATCH /hub/v1/campaigns/{id} - partially update an existing campaign.

        ``payload`` is the documented campaign body (changed fields only).
        Returns the parsed response dict (the updated campaign object). Raises
        SfmcApiError on a non-2xx status.
        """
        url = f"{self._base_url}/hub/v1/campaigns/{campaign_id}"
        transport = self._resolve(self._patch)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise SfmcApiError(status, _err_message(body))
        return body

    def trigger_send(self, definition_key: str, message: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /messaging/v1/messageDefinitionSends/key:{key}/send - trigger an email send.

        ``message`` is the documented send body (``{"To": {"SubscriberKey": ...,
        "ContactAttributes": {...}}}``). Returns the parsed response dict (the
        send receipt with ``requestId``/``responses``). Raises SfmcApiError on
        a non-2xx status.
        """
        url = f"{self._base_url}/messaging/v1/messageDefinitionSends/key:{definition_key}/send"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), message)
        if not (200 <= status < 300):
            raise SfmcApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from an SFMC error body."""
    if isinstance(body, dict):
        msg = body.get("message")
        if msg:
            return str(msg)
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
    return str(body)
