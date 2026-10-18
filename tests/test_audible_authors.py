from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.connectors import audible_connector
from app.connectors.audible_connector import AudibleBookData
from app.models.audible_author_book import AudibleAuthorBook
from app.models.audible_book import AudibleBook
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import audible_sync


def _product(asin, title, authors=("Dennis E. Taylor",), **extra):
    return {
        "asin": asin,
        "title": title,
        "authors": [{"name": a} for a in authors],
        "narrators": [{"name": "Ray Porter"}],
        "release_date": "2020-05-05",
        "product_images": {"500": "https://example.com/c.jpg"},
        "price": {"list_price": {"base": 19.95, "currency_code": "USD"}, "lowest_price": {"base": 13.96, "currency_code": "USD"}},
        **extra,
    }


# ----------------------------------------------------------------- connector


def test_parse_author_book_item_reads_series_and_release_date():
    item = _product("B1", "Heaven's River", series=[{"asin": "S1", "sequence": "4", "title": "Bobiverse"}])
    parsed = audible_connector.parse_author_book_item(item, "Dennis E. Taylor")

    assert parsed.title == "Heaven's River"
    assert parsed.series_title == "Bobiverse"
    assert parsed.series_sequence == "4"
    assert parsed.release_date == "2020-05-05"
    assert parsed.current_price == pytest.approx(13.96)


def test_parse_author_book_item_drops_fuzzy_search_matches_by_other_authors():
    item = _product("B1", "Not Theirs", authors=("Dennis Taylor",))
    assert audible_connector.parse_author_book_item(item, "Dennis E. Taylor") is None


def test_parse_author_book_item_matches_case_insensitively():
    assert audible_connector.parse_author_book_item(_product("B1", "X"), "dennis e. taylor") is not None


async def test_fetch_author_books_pages_until_a_short_page():
    full = [_product(f"B{i}", f"Book {i}") for i in range(audible_connector.AUTHOR_PAGE_SIZE)]
    pages = [{"products": full}, {"products": [_product("LAST", "Last")]}]
    client = AsyncMock()
    client.get = AsyncMock(side_effect=pages)
    client.__aenter__.return_value = client

    with patch.object(audible_connector.audible, "AsyncClient", return_value=client):
        books = await audible_connector.fetch_author_books(auth=None, author_name="Dennis E. Taylor")

    assert len(books) == audible_connector.AUTHOR_PAGE_SIZE + 1
    assert client.get.await_count == 2


# ------------------------------------------------------------------------- sync


class _FakeAuth:
    def to_dict(self):
        return {"access_token": "AT", "locale_code": "us"}


def _connect_audible(db):
    db.add(Credential(source=SOURCE_AUDIBLE, encrypted_payload=encrypt_json({"access_token": "AT", "locale_code": "us"})))
    db.commit()


def _owned(asin="B01L082HJ2", names=("Dennis E. Taylor",)):
    return AudibleBookData(
        asin=asin, title="We Are Legion", author=", ".join(names), runtime_minutes=596, cover_url="", author_names=list(names)
    )


async def test_refresh_stores_unowned_books_by_owned_authors(db):
    _connect_audible(db)
    books = [_owned()]
    found = [
        audible_connector.parse_author_book_item(_product("B01L082HJ2", "We Are Legion"), "Dennis E. Taylor"),
        audible_connector.parse_author_book_item(_product("B2", "Outland"), "Dennis E. Taylor"),
    ]

    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)),
        patch("app.sync.audible_sync.audible_connector.fetch_author_books", new=AsyncMock(return_value=found)),
    ):
        await audible_sync.refresh_audible_library(db)

    rows = db.query(AudibleAuthorBook).all()
    assert {(r.author_name, r.asin) for r in rows} == {("Dennis E. Taylor", "B2")}


async def test_an_author_that_fails_to_load_does_not_fail_the_refresh(db):
    _connect_audible(db)
    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=[_owned()])),
        patch("app.sync.audible_sync.audible_connector.fetch_author_books", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        count = await audible_sync.refresh_audible_library(db)

    assert count == 1
    assert db.query(AudibleAuthorBook).count() == 0


def test_is_contributor_credit_flags_a_role_suffixed_name():
    # Real shape from a captured catalog/products response (2026-10-18):
    # Audible's own `authors` array carries non-author contributors with
    # the role appended straight into the name string, no separate field.
    assert audible_sync._is_contributor_credit("Richard Howard - translator") is True
    assert audible_sync._is_contributor_credit("Annette Lavers - translator") is True


def test_is_contributor_credit_leaves_real_names_alone():
    assert audible_sync._is_contributor_credit("Dennis E. Taylor") is False
    assert audible_sync._is_contributor_credit("Jean-Paul Sartre") is False


async def test_refresh_never_searches_audible_for_a_contributor_credit(db):
    """Regression test for the Missing by Author tab coming back empty on a
    real account: a translator credit like "Richard Howard - translator"
    was being searched as if it were a real author, and since Audible's own
    search for that exact credit mostly just finds the same already-owned
    book back, the result was always zero new rows — not because nothing
    was missing, but because the "author" was never a real author.
    """
    _connect_audible(db)
    books = [_owned(names=("Roland Barthes", "Richard Howard - translator"))]
    fetch = AsyncMock(return_value=[])
    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)),
        patch("app.sync.audible_sync.audible_connector.fetch_author_books", new=fetch),
    ):
        await audible_sync.refresh_audible_library(db)

    assert [c.args[1] for c in fetch.await_args_list] == ["Roland Barthes"]


async def test_each_co_author_is_looked_up_once_even_across_books(db):
    _connect_audible(db)
    books = [_owned("A1", ("Ann", "Bob")), _owned("A2", ("ann",))]
    fetch = AsyncMock(return_value=[])
    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)),
        patch("app.sync.audible_sync.audible_connector.fetch_author_books", new=fetch),
    ):
        await audible_sync.refresh_audible_library(db)

    assert [c.args[1] for c in fetch.await_args_list] == ["Ann", "Bob"]


# ----------------------------------------------------------------------- router


def _seed(db):
    now = datetime.utcnow()
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK, encrypted_payload=encrypt_json({})))
    db.add(AudibleBook(asin="B01L082HJ2", title="We Are Legion", author="Dennis E. Taylor", runtime_minutes=596, cover_url="", fetched_at=now))
    return now


def test_missing_authors_tab_lists_books_grouped_by_author_newest_first(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Older Book", release_date="2018-01-01", fetched_at=now))
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B3", title="Newer Book", release_date="2022-01-01", series_title="Bobiverse", series_sequence="5", current_price=9.99, list_price=19.99, currency="USD", fetched_at=now))
    db.add(AudibleAuthorBook(author_name="Someone Else", asin="B4", title="Other Author Book", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible?view=missing_authors")

    assert resp.status_code == 200
    assert resp.text.count('class="disclosure series-group"') == 2
    assert resp.text.index("Newer Book") < resp.text.index("Older Book")
    assert "Bobiverse #5" in resp.text
    assert "-50%" in resp.text
    assert "We Are Legion" not in resp.text


def test_missing_authors_tab_count_badge_renders_on_every_view(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible")

    assert "Missing by author" in resp.text
    assert "Outland" not in resp.text


def test_a_book_bought_since_the_last_refresh_drops_off_the_missing_authors_tab(authed_client, db):
    now = _seed(db)
    db.add(AudibleBook(asin="B2", title="Outland", author="Dennis E. Taylor", runtime_minutes=1, cover_url="", fetched_at=now))
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible?view=missing_authors")

    assert "Nothing missing" in resp.text
    assert ">0</span>" in resp.text.split("Missing by author")[1][:200]
