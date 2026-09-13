import logging

from app import applog


def test_logs_requires_auth(client):
    resp = client.get("/settings/logs", follow_redirects=False)
    assert resp.status_code == 303


def test_logs_page_renders(authed_client):
    applog.clear()
    logging.getLogger("x").info("a test log line")
    resp = authed_client.get("/settings/logs")
    assert resp.status_code == 200
    assert "a test log line" in resp.text


def test_logs_content_fragment(authed_client):
    applog.clear()
    logging.getLogger("x").info("fragment line")
    resp = authed_client.get("/settings/logs/content")
    assert resp.status_code == 200
    assert "fragment line" in resp.text


def test_logs_download(authed_client):
    applog.clear()
    logging.getLogger("x").info("download me")
    resp = authed_client.get("/settings/logs/download")
    assert resp.status_code == 200
    assert "download me" in resp.text
    assert "attachment" in resp.headers["content-disposition"]


def test_logs_clear(authed_client):
    applog.clear()
    logging.getLogger("x").info("to be cleared")
    resp = authed_client.post("/settings/logs/clear")
    assert resp.status_code == 200
    assert "to be cleared" not in resp.text
    assert not any("to be cleared" in line for line in applog.recent_lines())
