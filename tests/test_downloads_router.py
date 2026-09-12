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


def test_downloads_page_shows_default_concurrency_when_unconfigured(authed_client):
    from app.config import settings

    resp = authed_client.get("/downloads")
    assert f'value="{settings.download_concurrency}"' in resp.text


def test_save_concurrency_persists_and_is_reflected_on_reload(authed_client, db):
    from app.models.download_settings import DownloadSettings

    resp = authed_client.post("/downloads/concurrency", data={"concurrency": "5"})
    assert resp.status_code == 200
    assert db.get(DownloadSettings, 1).concurrency == 5

    resp = authed_client.get("/downloads")
    assert 'value="5"' in resp.text


def test_save_concurrency_rejects_non_positive_values(authed_client, db):
    from app.models.download_settings import DownloadSettings

    resp = authed_client.post("/downloads/concurrency", data={"concurrency": "0"})
    assert resp.status_code == 200
    assert db.get(DownloadSettings, 1) is None  # never created from a bad value


def test_history_location_cell_wraps_instead_of_overflowing_its_card(authed_client, db, make_bundle):
    # Regression: a real absolute path has no spaces to wrap at, so without
    # overflow-wrap the table (and the long unbroken filename in particular)
    # visibly overflowed past the History card — confirmed live, not just a
    # style nitpick. .path-cell (app.css) is what fixes it.
    _make_download(
        db, make_bundle,
        current_location_path="/mnt/library/network-share/comics/long/nested/path/averyveryverylongfilenamewithnobreaks.pdf",
    )
    resp = authed_client.get("/downloads")
    assert 'class="muted path-cell"' in resp.text


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


def test_active_reflects_a_running_job(authed_client, db, make_bundle):
    from app.models.download_job import STATUS_RUNNING, DownloadJob

    make_bundle(gamekey="GK1", order=make_order(name="Running Bundle"))
    db.add(DownloadJob(gamekey="GK1", bundle_name="Running Bundle", status=STATUS_RUNNING))
    db.commit()

    resp = authed_client.get("/downloads/active")
    assert "Running Bundle" in resp.text
    assert "Running" in resp.text
    assert "every 2s" in resp.text


def test_active_reflects_a_queued_job_distinctly_from_running(authed_client, db, make_bundle):
    # Multiple jobs can now be "started" at once — a queued one should read
    # as waiting, not be confused with the one actually running.
    from app.models.download_job import STATUS_QUEUED, DownloadJob

    make_bundle(gamekey="GK1", order=make_order(name="Waiting Bundle"))
    db.add(DownloadJob(gamekey="GK1", bundle_name="Waiting Bundle", status=STATUS_QUEUED))
    db.commit()

    resp = authed_client.get("/downloads/active")
    assert "Waiting Bundle" in resp.text
    assert "Queued" in resp.text


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


def test_scan_preview_does_not_write_any_download_rows(authed_client, db, make_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan", data={"folder": str(tmp_path)})
    assert resp.status_code == 200
    assert "book.epub" in resp.text
    assert db.query(Download).count() == 0


def test_scan_preview_matched_table_uses_fixed_column_widths(authed_client, db, make_bundle, tmp_path, monkeypatch):
    # Regression guard: without table-layout:fixed, a long bundle name wraps
    # onto several lines and every other short-content cell in that row ends
    # up vertically centered in the resulting empty space (reported live —
    # see app.css's own comment on .scan-matched-table for the full story).
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan", data={"folder": str(tmp_path)})
    assert 'class="scan-matched-table"' in resp.text


def test_scan_preview_ambiguous_table_uses_fixed_column_widths(authed_client, db, make_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Book One", "machine_name": "book-one", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    make_bundle(gamekey="GK2", order=make_order(subproducts=[{"human_name": "Book Two", "machine_name": "book-two", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (tmp_path / "book.epub").write_bytes(b"x" * 10)

    resp = authed_client.post("/downloads/scan", data={"folder": str(tmp_path)})
    assert 'class="scan-ambiguous-table"' in resp.text


def test_scan_commit_creates_download_rows(authed_client, db, make_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan/commit", data={"folder": str(tmp_path)})
    assert resp.status_code == 200
    assert "Marked 1 item" in resp.text

    row = db.query(Download).one()
    assert row.status == STATUS_COMPLETED
    assert row.current_location_path == str(tmp_path / "book.epub")


def test_scan_allows_a_subdirectory_of_the_configured_root(authed_client, db, make_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    subdir = tmp_path / "my-library"
    subdir.mkdir()
    (subdir / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan", data={"folder": str(subdir)})
    assert resp.status_code == 200
    assert "book.epub" in resp.text


def test_scan_with_blank_folder_scans_the_whole_root(authed_client, db, make_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan", data={"folder": "", "root_index": 0})
    assert resp.status_code == 200
    assert "book.epub" in resp.text


def test_scan_targets_the_selected_root_when_multiple_are_configured(authed_client, db, make_bundle, tmp_path, monkeypatch):
    root0, root1 = tmp_path / "root0", tmp_path / "root1"
    root0.mkdir()
    root1.mkdir()
    monkeypatch.setattr("app.config.settings.scan_roots", f"{root0},{root1}")
    make_bundle(gamekey="GK1", order=make_order(subproducts=[{"human_name": "Cool Book", "machine_name": "coolbook", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}}]}]}]))
    (root1 / "book.epub").write_bytes(b"x" * 5)  # only in the second root

    resp_root0 = authed_client.post("/downloads/scan", data={"folder": "", "root_index": 0})
    assert "book.epub" not in resp_root0.text

    resp_root1 = authed_client.post("/downloads/scan", data={"folder": "", "root_index": 1})
    assert "book.epub" in resp_root1.text


def test_scan_rejects_an_out_of_bounds_root_index(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    resp = authed_client.post("/downloads/scan", data={"folder": "", "root_index": 5})
    assert resp.status_code == 200
    assert "not a directory" in resp.text


def test_scan_form_shows_a_root_dropdown_only_when_multiple_roots_configured(authed_client, tmp_path, monkeypatch):
    root0, root1 = tmp_path / "root0", tmp_path / "comics-library"
    root0.mkdir()
    root1.mkdir()
    monkeypatch.setattr("app.config.settings.scan_roots", f"{root0},{root1}")

    resp = authed_client.get("/downloads")
    assert 'select name="root_index"' in resp.text
    assert "comics-library" in resp.text


def test_scan_form_has_no_dropdown_with_a_single_root(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    monkeypatch.setattr("app.config.settings.scan_roots", "")

    resp = authed_client.get("/downloads")
    assert 'select name="root_index"' not in resp.text


def test_scan_preview_truncates_display_but_commit_still_covers_everything(authed_client, db, make_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    monkeypatch.setattr("app.routers.downloads._SCAN_PREVIEW_LIMIT", 2)
    for i in range(3):
        make_bundle(
            gamekey=f"GK{i}",
            order=make_order(subproducts=[{"human_name": f"Book {i}", "machine_name": f"book{i}", "downloads": [{"download_struct": [{"name": "EPUB", "file_size": 5, "url": {"web": f"https://dl.humble.com/book{i}.epub"}}]}]}]),
        )
        (tmp_path / f"book{i}.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan", data={"folder": ""})
    assert "Showing the first 2 of 3 matches" in resp.text
    shown = sum(1 for i in range(3) if f"book{i}.epub" in resp.text)
    assert shown == 2

    commit_resp = authed_client.post("/downloads/scan/commit", data={"folder": ""})
    assert "Marked 3 item(s) as downloaded" in commit_resp.text
    assert db.query(Download).count() == 3


def test_scan_rejects_a_path_outside_the_configured_root(authed_client, tmp_path, monkeypatch):
    root = tmp_path / "scan-root"
    root.mkdir()
    monkeypatch.setattr("app.config.settings.scan_root_dir", root)
    outside = tmp_path / "outside-the-root"
    outside.mkdir()
    (outside / "book.epub").write_bytes(b"x" * 5)

    resp = authed_client.post("/downloads/scan", data={"folder": str(outside)})
    assert resp.status_code == 200
    assert "not a directory" in resp.text
    assert "book.epub" not in resp.text


def test_scan_rejects_a_path_that_is_not_a_directory(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    missing = tmp_path / "does-not-exist"
    resp = authed_client.post("/downloads/scan", data={"folder": str(missing)})
    assert resp.status_code == 200
    assert "not a directory" in resp.text


def test_scan_rejects_a_malformed_path_without_raising(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    # Path.resolve() raises ValueError on an embedded null byte — this must be
    # caught by _resolve_scan_folder and turned into the normal "not a
    # directory" response, not surface as a 500.
    resp = authed_client.post("/downloads/scan", data={"folder": "foo\x00bar"})
    assert resp.status_code == 200
    assert "not a directory" in resp.text
