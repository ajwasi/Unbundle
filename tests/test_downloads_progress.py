from app.downloads.progress import bundle_download_progress
from app.models.download import STATUS_COMPLETED, STATUS_FAILED, Download
from tests.factories import make_order, make_subproduct


def test_bundle_download_progress_counts_completed_items(make_bundle, db):
    make_bundle(
        gamekey="GK1",
        order=make_order(
            subproducts=[
                make_subproduct("Item One", machine_name="item-one"),
                make_subproduct("Item Two", machine_name="item-two"),
            ]
        ),
    )
    db.add(
        Download(
            gamekey="GK1", item_name="Item One", original_filename="book.epub",
            subproduct_index=1, status=STATUS_COMPLETED,
        )
    )
    db.commit()

    progress = bundle_download_progress(db)
    assert progress["GK1"] == (1, 2)


def test_bundle_download_progress_excludes_items_with_no_downloadable_files(make_bundle, db):
    # A bundle with zero subproducts (e.g. a subscriptionplan record) must
    # report (0, 0), never divide-by-zero or count something nonexistent.
    make_bundle(gamekey="GK1", order=make_order(subproducts=[]))
    progress = bundle_download_progress(db)
    assert progress["GK1"] == (0, 0)


def test_bundle_download_progress_counts_an_item_downloaded_once_any_variant_completes(make_bundle, db):
    # Deliberately coarser than the bundle-detail page's own per-item status
    # (which requires *every* variant completed) — see progress.py's own
    # docstring on why: this stays purely SQL-side, no raw_json parsing, so
    # one completed variant is enough to count the whole item as downloaded.
    order = make_order(
        subproducts=[
            {
                "human_name": "Multi Format Item",
                "machine_name": "multi",
                "downloads": [
                    {
                        "download_struct": [
                            {"name": "EPUB", "file_size": 5, "url": {"web": "https://dl.humble.com/book.epub"}},
                            {"name": "PDF", "file_size": 5, "url": {"web": "https://dl.humble.com/book.pdf"}},
                        ]
                    }
                ],
            }
        ]
    )
    make_bundle(gamekey="GK1", order=order)
    db.add(
        Download(
            gamekey="GK1", item_name="Multi Format Item", original_filename="book.epub",
            subproduct_index=1, status=STATUS_COMPLETED,
        )
    )
    db.add(
        Download(
            gamekey="GK1", item_name="Multi Format Item", original_filename="book.pdf",
            subproduct_index=1, status=STATUS_FAILED,
        )
    )
    db.commit()

    progress = bundle_download_progress(db)
    assert progress["GK1"] == (1, 1)


def test_bundle_download_progress_reflects_a_completed_download_immediately(make_bundle, db):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Item One", machine_name="item-one")]))
    assert bundle_download_progress(db)["GK1"] == (0, 1)

    db.add(
        Download(
            gamekey="GK1", item_name="Item One", original_filename="book.epub",
            subproduct_index=1, status=STATUS_COMPLETED,
        )
    )
    db.commit()

    assert bundle_download_progress(db)["GK1"] == (1, 1)


def test_bundle_download_progress_distinguishes_items_by_subproduct_index(make_bundle, db):
    # Two different items in the same bundle, only one completed — the other
    # item's own (uncompleted) row must not inflate the count.
    make_bundle(
        gamekey="GK1",
        order=make_order(
            subproducts=[
                make_subproduct("Item One", machine_name="item-one"),
                make_subproduct("Item Two", machine_name="item-two"),
            ]
        ),
    )
    db.add(
        Download(
            gamekey="GK1", item_name="Item One", original_filename="book.epub",
            subproduct_index=1, status=STATUS_COMPLETED,
        )
    )
    db.add(
        Download(
            gamekey="GK1", item_name="Item Two", original_filename="book2.epub",
            subproduct_index=2, status=STATUS_FAILED,
        )
    )
    db.commit()

    progress = bundle_download_progress(db)
    assert progress["GK1"] == (1, 2)
