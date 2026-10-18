from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import audible_connector
from app.connectors.audible_connector import AudibleBookData
from app.models.audible_author_book import AudibleAuthorBook
from app.models.audible_author_profile import AudibleAuthorProfile
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


def test_parse_author_book_item_drops_a_non_english_edition():
    """Regression test for a real account: Brandon Sanderson's German,
    Spanish, French and Portuguese editions all showed up as "missing"
    alongside the real English gaps — the author catalogue search isn't
    scoped to one language.
    """
    item = _product("B1", "Legion – Die vielen Leben des Stephen Leeds", language="german")
    assert audible_connector.parse_author_book_item(item, "Dennis E. Taylor") is None


def test_parse_author_book_item_keeps_an_explicit_english_edition():
    item = _product("B1", "X", language="english")
    assert audible_connector.parse_author_book_item(item, "Dennis E. Taylor") is not None


def test_parse_author_book_item_keeps_an_item_with_no_language_field_at_all():
    # _product's own default shape has no "language" key — absent reads as
    # English rather than being excluded (see _is_english's own docstring).
    assert audible_connector.parse_author_book_item(_product("B1", "X"), "Dennis E. Taylor") is not None


def test_parse_author_book_item_captures_the_matching_authors_own_asin():
    item = _product("B1", "X")
    item["authors"] = [{"name": "Someone Else", "asin": "OTHER"}, {"name": "Dennis E. Taylor", "asin": "B010ETTBJC"}]
    parsed = audible_connector.parse_author_book_item(item, "Dennis E. Taylor")
    assert parsed.author_asin == "B010ETTBJC"


def test_parse_author_book_item_leaves_author_asin_blank_when_the_credit_has_none():
    # _product's own author entries never carry an asin — confirms this
    # doesn't crash and just defaults to "" rather than raising.
    parsed = audible_connector.parse_author_book_item(_product("B1", "X"), "Dennis E. Taylor")
    assert parsed.author_asin == ""


def _audnexus_response(status=200, **body):
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://api.audnex.us/authors/X"))


async def test_fetch_author_profile_reads_name_image_and_bio():
    resp = _audnexus_response(asin="B010ETTBJC", name="Dennis E. Taylor", image="https://example.com/a.jpg", description="A bio.")
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        profile = await audible_connector.fetch_author_profile("B010ETTBJC")

    assert profile.author_asin == "B010ETTBJC"
    assert profile.name == "Dennis E. Taylor"
    assert profile.image_url == "https://example.com/a.jpg"
    assert profile.bio == "A bio."


async def test_fetch_author_profile_strips_html_from_the_bio():
    resp = _audnexus_response(asin="B1", name="X", image="", description="<p>Bio <b>text</b>.</p>")
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        profile = await audible_connector.fetch_author_profile("B1")
    assert profile.bio == "Bio text."


async def test_fetch_author_profile_returns_none_for_a_malformed_asin():
    # A malformed asin reliably 400s on the real API (confirmed live) rather
    # than ever 404ing — treated as "no profile" either way.
    resp = _audnexus_response(status=400, error={"code": "BAD_REQUEST", "message": "Bad ASIN"})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        assert await audible_connector.fetch_author_profile("ZZZZZZZZZZ") is None


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


def _author_book_item_with_asin(asin, title, author_asin="B010ETTBJC"):
    item = _product(asin, title)
    item["authors"] = [{"name": "Dennis E. Taylor", "asin": author_asin}]
    return item


async def test_refresh_stores_author_profiles_from_audnexus(db):
    _connect_audible(db)
    found = [audible_connector.parse_author_book_item(_author_book_item_with_asin("B2", "Outland"), "Dennis E. Taylor")]
    profile = audible_connector.AudibleAuthorProfileData(
        author_asin="B010ETTBJC", name="Dennis E. Taylor", image_url="https://example.com/a.jpg", bio="A bio."
    )

    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=[_owned()])),
        patch("app.sync.audible_sync.audible_connector.fetch_author_books", new=AsyncMock(return_value=found)),
        patch("app.sync.audible_sync.audible_connector.fetch_author_profile", new=AsyncMock(return_value=profile)),
    ):
        await audible_sync.refresh_audible_library(db)

    row = db.query(AudibleAuthorProfile).one()
    assert row.author_asin == "B010ETTBJC"
    assert row.image_url == "https://example.com/a.jpg"
    assert row.bio == "A bio."


async def test_an_author_profile_that_fails_to_load_does_not_fail_the_refresh(db):
    _connect_audible(db)
    found = [audible_connector.parse_author_book_item(_author_book_item_with_asin("B2", "Outland"), "Dennis E. Taylor")]

    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=[_owned()])),
        patch("app.sync.audible_sync.audible_connector.fetch_author_books", new=AsyncMock(return_value=found)),
        patch("app.sync.audible_sync.audible_connector.fetch_author_profile", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        count = await audible_sync.refresh_audible_library(db)

    assert count == 1
    assert db.query(AudibleAuthorBook).count() == 1  # the author-books refresh itself still succeeded
    assert db.query(AudibleAuthorProfile).count() == 0


async def test_fetch_author_profiles_deduplicates_by_author_asin():
    """Two missing books by the same author share one author_asin — only
    one Audnexus lookup should fire for it, not one per missing book.
    """
    now = datetime.utcnow()
    rows = [
        AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", author_asin="B010ETTBJC", fetched_at=now),
        AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B3", author_asin="B010ETTBJC", fetched_at=now),
    ]
    fetch = AsyncMock(return_value=None)
    with patch("app.sync.audible_sync.audible_connector.fetch_author_profile", new=fetch):
        await audible_sync._fetch_author_profiles(rows)
    fetch.assert_awaited_once_with("B010ETTBJC")


async def test_fetch_author_profiles_fetches_distinct_authors_concurrently_and_tolerates_one_failure():
    """Author profiles are fetched with bounded concurrency (asyncio.gather),
    not one at a time — a single author's lookup failing must not cancel or
    drop the others, same "never fail the whole refresh over one bad lookup"
    posture as the strictly-sequential fetches elsewhere in this module.
    """
    now = datetime.utcnow()
    rows = [
        AudibleAuthorBook(author_name="A", asin="B2", author_asin="AASIN", fetched_at=now),
        AudibleAuthorBook(author_name="B", asin="B3", author_asin="BASIN", fetched_at=now),
        AudibleAuthorBook(author_name="C", asin="B4", author_asin="CASIN", fetched_at=now),
    ]

    async def fake_fetch(author_asin):
        if author_asin == "BASIN":
            raise RuntimeError("boom")
        return audible_connector.AudibleAuthorProfileData(author_asin=author_asin, name=author_asin, image_url="", bio="")

    with patch("app.sync.audible_sync.audible_connector.fetch_author_profile", new=AsyncMock(side_effect=fake_fetch)):
        result = await audible_sync._fetch_author_profiles(rows)

    assert {r.author_asin for r in result} == {"AASIN", "CASIN"}


async def test_fetch_author_profiles_skips_rows_with_no_captured_asin():
    now = datetime.utcnow()
    rows = [AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", author_asin="", fetched_at=now)]
    fetch = AsyncMock(return_value=None)
    with patch("app.sync.audible_sync.audible_connector.fetch_author_profile", new=fetch):
        result = await audible_sync._fetch_author_profiles(rows)
    fetch.assert_not_awaited()
    assert result == []


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


def test_missing_authors_tab_count_is_distinct_authors_not_book_count(authed_client, db):
    """Regression test: the badge must count authors with something
    missing, not the missing books themselves — two missing books by the
    same author is still "1" author, not "2".
    """
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B3", title="For We Are Many", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible")

    assert ">1/2</span>" in resp.text.split("Missing by author")[1][:100]


def test_missing_authors_search_box_filters_groups_but_tab_count_stays_stable(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.add(AudibleAuthorBook(author_name="Someone Else", asin="B4", title="Other Author Book", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible", params={"view": "missing_authors", "author_q": "taylor"})
    assert "Dennis E. Taylor" in resp.text
    assert "Someone Else" not in resp.text
    # The tab badge is unaffected by the search box's own filter.
    assert ">2/2</span>" in resp.text.split("Missing by author")[1][:200]
    assert 'value="taylor"' in resp.text


def test_missing_authors_search_box_with_no_match_shows_an_empty_state(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible", params={"view": "missing_authors", "author_q": "nonexistent"})
    assert 'No authors match "nonexistent".' in resp.text
    assert "Outland" not in resp.text


def test_missing_authors_hx_request_returns_just_that_fragment(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible?view=missing_authors", headers={"HX-Request": "true"})
    assert 'id="audible-missing-authors"' in resp.text
    assert "Outland" in resp.text
    assert "<h1>Audible Library</h1>" not in resp.text


def test_missing_authors_tab_author_name_links_to_the_author_page(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible?view=missing_authors")

    assert 'href="/audible/author/Dennis%20E.%20Taylor"' in resp.text


def test_missing_authors_tab_shows_a_thumbnail_when_a_profile_is_cached(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", author_asin="B010ETTBJC", title="Outland", fetched_at=now))
    db.add(AudibleAuthorProfile(author_asin="B010ETTBJC", name="Dennis E. Taylor", image_url="https://example.com/a.jpg", bio="A bio.", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible?view=missing_authors")

    assert 'src="https://example.com/a.jpg"' in resp.text


def test_missing_authors_tab_shows_no_thumbnail_without_a_cached_profile(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", author_asin="B010ETTBJC", title="Outland", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible?view=missing_authors")

    assert "<img" not in resp.text


def test_author_detail_page_shows_photo_and_bio_when_a_profile_is_cached(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", author_asin="B010ETTBJC", title="Outland", fetched_at=now))
    db.add(AudibleAuthorProfile(author_asin="B010ETTBJC", name="Dennis E. Taylor", image_url="https://example.com/a.jpg", bio="A real bio.", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible/author/Dennis E. Taylor")

    assert 'src="https://example.com/a.jpg"' in resp.text
    assert "A real bio." in resp.text


def test_author_detail_page_lists_owned_and_missing_books(authed_client, db):
    now = _seed(db)
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="For We Are Many", series_title="Bobiverse", series_sequence="2", current_price=13.96, list_price=19.95, currency="USD", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible/author/Dennis E. Taylor")

    assert resp.status_code == 200
    assert "Dennis E. Taylor" in resp.text
    assert "We Are Legion" in resp.text
    assert "For We Are Many" in resp.text
    assert resp.text.count(">Owned<") == 1
    assert resp.text.count(">Missing<") == 1
    assert 'href="/audible/B01L082HJ2"' in resp.text
    assert 'href="https://www.audible.com/pd/B2"' in resp.text
    assert "Bobiverse #2" in resp.text
    assert "1 owned" in resp.text
    assert "1 missing" in resp.text
    assert "<img" not in resp.text  # no author_asin captured on this row, so no profile lookup at all


def test_author_detail_page_does_not_match_a_different_author_with_a_similar_name(authed_client, db):
    # "Ann" must not match "Annette" — exact, casefolded membership only.
    now = _seed(db)
    db.add(AudibleBook(asin="B9", title="Unrelated", author="Annette Someone", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible/author/Ann")

    assert resp.status_code == 404


def test_author_detail_page_404s_for_an_unknown_author(authed_client):
    resp = authed_client.get("/audible/author/Nobody%20At%20All")
    assert resp.status_code == 404


def test_author_detail_page_does_not_double_list_a_book_bought_since_the_last_refresh(authed_client, db):
    now = _seed(db)
    # "We Are Legion" is already owned (seeded above) — a stale
    # AudibleAuthorBook row for it (as if bought after the last author
    # refresh) must not also show it as Missing.
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B01L082HJ2", title="We Are Legion", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible/author/Dennis E. Taylor")

    assert resp.text.count("We Are Legion") == 1
    assert resp.text.count(">Owned<") == 1
    assert resp.text.count(">Missing<") == 0


def test_a_book_bought_since_the_last_refresh_drops_off_the_missing_authors_tab(authed_client, db):
    now = _seed(db)
    db.add(AudibleBook(asin="B2", title="Outland", author="Dennis E. Taylor", runtime_minutes=1, cover_url="", fetched_at=now))
    db.add(AudibleAuthorBook(author_name="Dennis E. Taylor", asin="B2", title="Outland", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible?view=missing_authors")

    assert "Nothing missing" in resp.text
    assert ">0/0</span>" in resp.text.split("Missing by author")[1][:200]
