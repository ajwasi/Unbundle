from app.models.download import STATUS_COMPLETED, STATUS_FAILED, Download
from app.models.download_destination import DownloadDestination
from app.models.tag import Tag
from tests.factories import make_order


def _make_download(db, make_bundle, gamekey="GK1", bundle_name="My Bundle", **overrides):
    make_bundle(gamekey=gamekey, order=make_order(name=bundle_name))
    row = Download(
        gamekey=gamekey,
        item_name=overrides.pop("item_name", "Cool Book"),
        original_filename=overrides.pop("original_filename", "book.epub"),
        file_format=overrides.pop("file_format", "EPUB"),
        status=overrides.pop("status", STATUS_COMPLETED),
        expected_size_bytes=overrides.pop("expected_size_bytes", 1024),
        **overrides,
    )
    db.add(row)
    db.commit()
    return row


def test_downloads_page_requires_auth(client):
    resp = client.get("/downloads", follow_redirects=False)
    assert resp.status_code == 303


def test_downloads_page_loads(authed_client, db, make_bundle):
    _make_download(db, make_bundle)
    resp = authed_client.get("/downloads")
    assert resp.status_code == 200
    assert "Cool Book" in resp.text
    assert "My Bundle" in resp.text


def test_downloads_page_renders_missing_completed_at_as_a_real_dash_not_literal_text(authed_client, db, make_bundle):
    # Regression: the "Completed" cell's fallback used a Jinja if/else expression
    # with no `| safe` on the whole result, so autoescaping re-escaped the "&" in
    # the literal "&mdash;" string into "&amp;mdash;" — which a browser renders
    # as the visible text "&mdash;" instead of decoding it into an em dash. The
    # correct, working output still legitimately contains the raw "&mdash;"
    # entity in the HTML source (that's what the browser is meant to decode) —
    # it's the doubled escaping that signals the bug. Caught live in a browser,
    # not by any status-code/content assertion.
    _make_download(db, make_bundle, completed_at=None)
    resp = authed_client.get("/downloads")
    assert "&amp;mdash;" not in resp.text


def test_active_shows_no_download_running_by_default(authed_client):
    resp = authed_client.get("/downloads/active")
    assert "No download running" in resp.text


def test_active_reflects_a_running_job(authed_client, monkeypatch):
    monkeypatch.setattr("app.downloads.worker.is_download_running", lambda: True)
    resp = authed_client.get("/downloads/active")
    assert "in progress" in resp.text
    assert "every 2s" in resp.text


def test_history_filters_by_status(authed_client, db, make_bundle):
    _make_download(db, make_bundle, gamekey="GK1", item_name="Done Book", status=STATUS_COMPLETED)
    _make_download(db, make_bundle, gamekey="GK2", bundle_name="Other Bundle", item_name="Failed Book", status=STATUS_FAILED)

    resp = authed_client.get("/downloads/history", params={"status": "failed"})
    assert "Failed Book" in resp.text
    assert "Done Book" not in resp.text


def test_history_filters_by_format(authed_client, db, make_bundle):
    _make_download(db, make_bundle, gamekey="GK1", item_name="Epub Item", file_format="EPUB")
    _make_download(db, make_bundle, gamekey="GK2", bundle_name="Other Bundle", item_name="Pdf Item", file_format="PDF")

    resp = authed_client.get("/downloads/history", params={"file_format": "PDF"})
    assert "Pdf Item" in resp.text
    assert "Epub Item" not in resp.text


def test_history_filters_by_bundle_name_substring(authed_client, db, make_bundle):
    _make_download(db, make_bundle, gamekey="GK1", bundle_name="Fantasy Collection", item_name="Item A")
    _make_download(db, make_bundle, gamekey="GK2", bundle_name="Sci-Fi Collection", item_name="Item B")

    resp = authed_client.get("/downloads/history", params={"q": "Fantasy"})
    assert "Item A" in resp.text
    assert "Item B" not in resp.text


def test_history_tolerates_blank_filter_values(authed_client, db, make_bundle):
    _make_download(db, make_bundle)
    resp = authed_client.get("/downloads/history", params={"status": "", "file_format": "", "q": ""})
    assert resp.status_code == 200
    assert "Cool Book" in resp.text


def test_create_destination_round_trip(authed_client, db):
    tag = Tag(name="Comic")
    db.add(tag)
    db.commit()

    resp = authed_client.post(
        "/downloads/destinations",
        data={"name": "Comics", "formats": "pdf, cbz", "tag_id": str(tag.id), "path": "/mnt/comics"},
    )
    assert resp.status_code == 200
    assert "Comics" in resp.text
    assert "/mnt/comics" in resp.text

    dest = db.query(DownloadDestination).one()
    assert dest.tag_id == tag.id
    assert dest.formats == "pdf, cbz"


def test_create_destination_requires_name_and_path(authed_client, db):
    resp = authed_client.post("/downloads/destinations", data={"name": "", "path": ""})
    assert resp.status_code == 200
    assert db.query(DownloadDestination).count() == 0


def test_delete_destination(authed_client, db):
    dest = DownloadDestination(name="Comics", formats="pdf", path="/mnt/comics")
    db.add(dest)
    db.commit()

    resp = authed_client.post(f"/downloads/destinations/{dest.id}/delete")
    assert resp.status_code == 200
    assert db.query(DownloadDestination).count() == 0


def test_scan_preview_does_not_write_any_download_rows(authed_client, db, make_bundle, tmp_path):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan", data={"folder": str(tmp_path)})
    assert resp.status_code == 200
    assert "book.epub" in resp.text
    assert db.query(Download).count() == 0


def test_scan_commit_creates_download_rows(authed_client, db, make_bundle, tmp_path):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan/commit", data={"folder": str(tmp_path)})
    assert resp.status_code == 200
    assert "Marked 1 item" in resp.text

    row = db.query(Download).one()
    assert row.status == STATUS_COMPLETED
    assert row.current_location_path == str(tmp_path / "book.epub")


def test_scan_rejects_a_path_that_is_not_a_directory(authed_client, tmp_path):
    missing = tmp_path / "does-not-exist"
    resp = authed_client.post("/downloads/scan", data={"folder": str(missing)})
    assert resp.status_code == 200
    assert "not a directory" in resp.text
