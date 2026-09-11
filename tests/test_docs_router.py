def test_openapi_schema_still_served_after_disabling_default_docs(authed_client):
    # docs_url=None/redoc_url=None (main.py) only disable the two doc-viewer
    # pages, not the schema endpoint they both load from.
    resp = authed_client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "Unbundle"


def test_swagger_docs_include_the_theme_detection_script_and_dark_css(authed_client):
    resp = authed_client.get("/docs")
    assert resp.status_code == 200
    assert "swagger-ui" in resp.text
    assert "unbundleDocsTheme" in resp.text or "localStorage.getItem(\"theme\")" in resp.text
    assert "invert(88%)" in resp.text


def test_redoc_includes_the_theme_detection_script_and_dark_theme_json(authed_client):
    resp = authed_client.get("/redoc")
    assert resp.status_code == 200
    assert "<redoc" in resp.text
    assert "localStorage.getItem(\"theme\")" in resp.text
    # The ReDoc theme JSON is embedded as a JSON-encoded JS string literal
    # (see app/routers/docs.py), so its keys appear escaped, not literal — just
    # confirm the accent color made it into the response at all.
    assert "5ea3d9" in resp.text
