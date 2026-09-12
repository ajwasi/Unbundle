from app.downloads import scan
from app.models.download import STATUS_COMPLETED, Download
from tests.factories import make_order, make_subproduct


def _seed(make_bundle, gamekey, item_name, filename, machine_name=None, size=1000):
    order = make_order(
        subproducts=[
            make_subproduct(
                human_name=item_name,
                machine_name=machine_name,
                downloads=[{"download_struct": [{"name": "EPUB", "file_size": size, "url": {"web": f"https://dl.humble.com/{filename}"}}]}],
            )
        ]
    )
    return make_bundle(gamekey=gamekey, order=order)


def test_build_expected_index_keys_by_lowercase_filename(db, make_bundle):
    _seed(make_bundle, "GK1", "Cool Book", "Book.EPUB")
    index = scan.build_expected_index(db)
    assert "book.epub" in index
    assert index["book.epub"][0]["item_name"] == "Cool Book"


def test_scan_folder_single_match(db, make_bundle, tmp_path):
    bundle = _seed(make_bundle, "GK1", "Cool Book", "book.epub", size=5)
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))

    assert len(result.matched) == 1
    assert result.matched[0]["gamekey"] == bundle.gamekey
    assert result.matched[0]["size_mismatch"] is False
    assert result.ambiguous == []
    assert result.unmatched_count == 0


def test_scan_folder_flags_size_mismatch_but_still_matches(db, make_bundle, tmp_path):
    _seed(make_bundle, "GK1", "Cool Book", "book.epub", size=999999)
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))

    assert len(result.matched) == 1
    assert result.matched[0]["size_mismatch"] is True


def test_scan_folder_ambiguous_when_two_items_share_a_filename(db, make_bundle, tmp_path):
    _seed(make_bundle, "GK1", "Book One", "book.epub")
    _seed(make_bundle, "GK2", "Book Two", "book.epub")
    (tmp_path / "book.epub").write_bytes(b"x" * 10)

    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))

    assert result.matched == []
    assert len(result.ambiguous) == 1
    assert len(result.ambiguous[0]["candidates"]) == 2


def test_scan_folder_counts_unmatched_files_without_erroring(db, tmp_path):
    (tmp_path / "mystery.zip").write_bytes(b"x")

    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))

    assert result.unmatched_count == 1
    assert result.matched == []
    assert result.ambiguous == []


def test_scan_folder_matches_files_in_subdirectories(db, make_bundle, tmp_path):
    _seed(make_bundle, "GK1", "Cool Book", "book.epub", size=5)
    nested = tmp_path / "some" / "nested" / "folder"
    nested.mkdir(parents=True)
    (nested / "book.epub").write_bytes(b"x" * 5)

    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))
    assert len(result.matched) == 1


def test_commit_matches_creates_a_completed_download_row(db, make_bundle, tmp_path):
    bundle = _seed(make_bundle, "GK1", "Cool Book", "book.epub", size=5)
    (tmp_path / "book.epub").write_bytes(b"x" * 5)
    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))

    committed = scan.commit_matches(db, result)

    assert committed == 1
    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.status == STATUS_COMPLETED
    assert row.current_location_path == str(tmp_path / "book.epub")
    assert row.completed_at is None  # real completion time was never observed


def test_commit_matches_renames_an_underscore_filename_to_a_readable_one(db, make_bundle, tmp_path):
    bundle = _seed(make_bundle, "GK1", "Cool Book", "some_book_vol_02.epub", size=5)
    (tmp_path / "some_book_vol_02.epub").write_bytes(b"x" * 5)
    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))

    committed = scan.commit_matches(db, result)

    assert committed == 1
    expected_path = tmp_path / "some book vol 02.epub"
    assert expected_path.is_file()
    assert not (tmp_path / "some_book_vol_02.epub").exists()
    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.current_location_path == str(expected_path)


def test_scan_preview_never_renames_files_on_disk(db, make_bundle, tmp_path):
    _seed(make_bundle, "GK1", "Cool Book", "some_book_vol_02.epub", size=5)
    (tmp_path / "some_book_vol_02.epub").write_bytes(b"x" * 5)

    scan.scan_folder(tmp_path, scan.build_expected_index(db))  # preview only, no commit

    assert (tmp_path / "some_book_vol_02.epub").exists()
    assert not (tmp_path / "some book vol 02.epub").exists()


def test_commit_matches_skips_a_row_already_completed_instead_of_overwriting_it(db, make_bundle, tmp_path):
    """A row already tracked as completed (e.g. from a real download that was
    then relocated) must not have its location clobbered by a scan that
    happens to find a stray duplicate copy elsewhere.
    """
    bundle = _seed(make_bundle, "GK1", "Cool Book", "book.epub", size=5)
    existing = Download(
        gamekey=bundle.gamekey,
        item_name="Cool Book",
        original_filename="book.epub",
        status=STATUS_COMPLETED,
        current_location_path="/mnt/library/book.epub",
    )
    db.add(existing)
    db.commit()

    (tmp_path / "book.epub").write_bytes(b"x" * 5)
    result = scan.scan_folder(tmp_path, scan.build_expected_index(db))
    committed = scan.commit_matches(db, result)

    assert committed == 0
    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.current_location_path == "/mnt/library/book.epub"


def test_commit_matches_is_idempotent_across_two_runs(db, make_bundle, tmp_path):
    _seed(make_bundle, "GK1", "Cool Book", "book.epub", size=5)
    (tmp_path / "book.epub").write_bytes(b"x" * 5)

    result1 = scan.scan_folder(tmp_path, scan.build_expected_index(db))
    first = scan.commit_matches(db, result1)
    result2 = scan.scan_folder(tmp_path, scan.build_expected_index(db))
    second = scan.commit_matches(db, result2)

    assert first == 1
    assert second == 0
    assert db.query(Download).count() == 1
