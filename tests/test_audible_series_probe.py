from unittest.mock import AsyncMock, patch

from app.sync import audible_sync


def test_probe_reports_not_connected_as_a_conflict(authed_client):
    resp = authed_client.get("/audible/probe-series", params={"asin": "B01M1RDL6W"})
    assert resp.status_code == 409
    assert "not connected" in resp.text.lower()


def test_probe_returns_the_raw_catalogue_json(authed_client):
    raw = {"product": {"asin": "B01M1RDL6W", "relationships": [{"asin": "B01L082HJ2", "sort": "1"}]}}
    with patch.object(audible_sync, "probe_catalog_product", new=AsyncMock(return_value=raw)):
        resp = authed_client.get("/audible/probe-series", params={"asin": "B01M1RDL6W"})

    assert resp.status_code == 200
    assert '"relationships"' in resp.text
    assert "B01L082HJ2" in resp.text
