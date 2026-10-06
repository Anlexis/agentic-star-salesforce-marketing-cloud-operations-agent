# CMN-C2-232 - Unit tests: SfmcClient service (SFMC REST API shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.sfmc_client import SfmcApiError, SfmcClient


def test_find_campaign_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"id": "1001", "name": "Campaign 1001", "campaignCode": "1001"}

    client = SfmcClient("https://sub.rest.marketingcloudapis.example/", get=get)
    resp = client.find_campaign("1001", "tok123")
    assert resp["id"] == "1001"
    assert captured["url"] == "https://sub.rest.marketingcloudapis.example/hub/v1/campaigns/1001"
    # SFMC REST API auth: the per-call OAuth2 access token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["campaign_id"] == "1001"


def test_create_campaign_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"id": "summer26", "name": "summer launch 2026", "campaignCode": "summer26"}

    client = SfmcClient("https://sub.rest.marketingcloudapis.example", post=post)
    payload = {"name": "summer launch 2026", "campaignCode": "summer26"}
    resp = client.create_campaign(payload, "tok")
    assert resp["id"] == "summer26"
    assert captured["url"] == "https://sub.rest.marketingcloudapis.example/hub/v1/campaigns"
    assert captured["body"] == payload


def test_update_campaign_success_with_injected_patch():
    captured = {}

    def patch(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"id": "summer26", "campaignCode": "summer26"}

    client = SfmcClient("https://sub.rest.marketingcloudapis.example", patch=patch)
    payload = {"description": "revised copy"}
    resp = client.update_campaign("summer26", payload, "tok")
    assert resp["id"] == "summer26"
    assert captured["url"] == "https://sub.rest.marketingcloudapis.example/hub/v1/campaigns/summer26"
    assert captured["body"] == payload


def test_trigger_send_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 202, {"requestId": "req-1", "responses": [{"recipientSendId": "rs-1", "hasErrors": False}]}

    client = SfmcClient("https://sub.rest.marketingcloudapis.example", post=post)
    message = {"To": {"SubscriberKey": "sub-1001"}}
    resp = client.trigger_send("welcome-01", message, "tok")
    assert resp["requestId"] == "req-1"
    assert captured["url"] == (
        "https://sub.rest.marketingcloudapis.example/messaging/v1/messageDefinitionSends/key:welcome-01/send"
    )
    assert captured["body"] == message


def test_non_2xx_raises_sfmc_api_error():
    def post(url, headers, body):
        return 400, {"errors": ["name is required"]}

    client = SfmcClient("https://sub.rest.marketingcloudapis.example", post=post)
    with pytest.raises(SfmcApiError) as exc:
        client.create_campaign({}, "tok")
    assert exc.value.status_code == 400
    assert "name is required" in str(exc.value)


def test_non_2xx_message_key_raises():
    def get(url, headers, body):
        return 404, {"message": "campaign not found"}

    client = SfmcClient(get=get)
    with pytest.raises(SfmcApiError) as exc:
        client.find_campaign("nope", "tok")
    assert exc.value.status_code == 404
    assert "campaign not found" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = SfmcClient()
    assert client.uses_stub_transport is True
    resp = client.find_campaign("1001", "tok")
    assert resp.get("_stub") is True
    assert resp["id"] == "1001"
    assert resp["name"] == "Campaign 1001"
    assert resp["campaignCode"] == "1001"


def test_default_stub_transport_create_echoes_campaign_code():
    client = SfmcClient()
    resp = client.create_campaign({"name": "summer launch 2026", "campaignCode": "summer26"}, "tok")
    assert resp.get("_stub") is True
    assert resp["id"] == "summer26"
    assert resp["campaignCode"] == "summer26"


def test_default_stub_transport_trigger_send_receipt():
    client = SfmcClient()
    resp = client.trigger_send("welcome-01", {"To": {"SubscriberKey": "sub-1001"}}, "tok")
    assert resp.get("_stub") is True
    assert resp["requestId"]
    assert resp["responses"][0]["hasErrors"] is False


def test_injected_transport_disables_stub_flag():
    client = SfmcClient(get=lambda url, headers, body: (200, {"id": "1001"}))
    assert client.uses_stub_transport is False
