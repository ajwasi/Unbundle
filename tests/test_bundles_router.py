import re
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.models.tag import BundleTag, Tag
from tests.factories import make_order, make_subproduct


def _has_form_nested_inside_p(html: str) -> bool:
    """Real browsers force-close an open <p> the instant a <form> start tag
    appears (and silently take whatever was nested inside the <p> down with
    it) — the server's raw markup can look perfectly nested as a string while
    still parsing into a broken DOM. This mirrors that HTML5 rule so tests
    catch it without needing a real browser.
    """
    p_depth = 0
    for closing, name in re.findall(r"<(/?)(\w+)", html):
        name = name.lower()
        if name == "p":
            p_depth = max(p_depth - 1, 0) if closing else p_depth + 1
        elif name == "form" and not closing and p_depth > 0:
            return True
    return False


def test_list_bundles_requires_auth(client):
    resp = client.get("/bundles", follow_redirects=False)
    assert resp.status_code == 303


def test_refresh_rate_limited_after_too_many_requests(authed_client):
    with patch("app.routers.bundles.refresh.start_refresh", new=AsyncMock()):
        for _ in range(5):
            resp = authed_client.post("/bundles/refresh")
            assert resp.status_code == 200
        resp = authed_client.post("/bundles/refresh")
    assert resp.status_code == 429


def test_list_bundles_shows_seeded_bundle(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(name="Findable Bundle"))
    resp = authed_client.get("/bundles")
    assert resp.status_code == 200
    assert "Findable Bundle" in resp.text


def test_list_bundles_shows_never_refreshed_with_no_bundles(authed_client):
    resp = authed_client.get("/bundles")
    assert "Never refreshed" in resp.text


def test_list_bundles_shows_last_refreshed_time(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(name="Findable Bundle"), fetched_at=datetime.utcnow() - timedelta(hours=1))
    resp = authed_client.get("/bundles")
    assert "Last refreshed" in resp.text
    assert "1 hour ago" in resp.text


def test_list_bundles_defers_raw_json_column(authed_client, make_bundle, db):
    # Perf regression guard: raw_json can be tens of MB across a real library
    # and the list view never reads it — confirmed via a real 550-bundle
    # library that loading it here (vs. deferring) is the entire difference
    # between a ~50ms and a ~400ms query. Captures the actual SQL the real
    # route emits over a real HTTP request, rather than re-testing the query
    # helpers directly — those would stay green even if someone dropped the
    # defer() call from list_bundles() itself.
    from sqlalchemy import event

    make_bundle(gamekey="GK1", order=make_order(name="Findable Bundle"))

    statements = []
    engine = db.get_bind()
    listener = lambda conn, cursor, statement, *a: statements.append(statement)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        resp = authed_client.get("/bundles")
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert resp.status_code == 200
    bundle_selects = [s for s in statements if "FROM bundle" in s and "bundle_entitlement" not in s and "bundle_tag" not in s]
    assert bundle_selects, "expected at least one SELECT against the bundle table"
    assert all("raw_json" not in s for s in bundle_selects)


def test_list_bundles_search_filters_by_name(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(name="Zebra Bundle"))
    make_bundle(gamekey="GK2", order=make_order(name="Aardvark Bundle"))
    resp = authed_client.get("/bundles?q=Zebra")
    assert "Zebra Bundle" in resp.text
    assert "Aardvark Bundle" not in resp.text


def test_list_bundles_category_filter(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(name="Game Bundle", category="bundle"))
    make_bundle(gamekey="GK2", order=make_order(name="Sub Content", category="subscriptioncontent"))
    resp = authed_client.get("/bundles?category=subscriptioncontent")
    assert "Sub Content" in resp.text
    assert "Game Bundle" not in resp.text


def test_list_bundles_category_dropdown_lists_real_categories_only(authed_client, make_bundle):
    # Regression guard: the dropdown's option list is now derived from
    # category_breakdown (see list_bundles) instead of its own separate
    # query — confirms that still excludes an uncategorized bundle's
    # "(none)" substitution rather than offering it as a filterable value.
    make_bundle(gamekey="GK1", order=make_order(category="subscriptionplan"))
    make_bundle(gamekey="GK2", order=make_order(category=""))
    resp = authed_client.get("/bundles")
    assert 'value="subscriptionplan"' in resp.text
    assert 'value="(none)"' not in resp.text


def test_list_bundles_min_items_filter(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(name="Big Bundle", subproducts=[make_subproduct(f"Item {i}") for i in range(5)]))
    make_bundle(gamekey="GK2", order=make_order(name="Small Bundle", subproducts=[make_subproduct("Only Item")]))
    resp = authed_client.get("/bundles?min_items=3")
    assert "Big Bundle" in resp.text
    assert "Small Bundle" not in resp.text


def test_list_bundles_tag_filter(authed_client, make_bundle, db):
    tagged = make_bundle(gamekey="GK1", order=make_order(name="Tagged Bundle"))
    make_bundle(gamekey="GK2", order=make_order(name="Untagged Bundle"))
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=tagged.gamekey))
    db.commit()

    resp = authed_client.get(f"/bundles?tag_id={tag.id}")
    assert "Tagged Bundle" in resp.text
    assert "Untagged Bundle" not in resp.text


def test_list_bundles_search_tolerates_blank_select_and_number_fields(authed_client, make_bundle):
    """The search box's htmx trigger submits every field in the form together,
    including tag_id/min_items as empty strings when left at their "All"/blank
    state — that used to 422 (int | None can't parse ""), which made the
    search box look like it silently did nothing.
    """
    make_bundle(gamekey="GK1", order=make_order(name="Findable Bundle"))
    resp = authed_client.get("/bundles?sort=name&dir=asc&q=Findable&category=&min_items=&tag_id=&redeemed=")
    assert resp.status_code == 200
    assert "Findable Bundle" in resp.text


def test_list_bundles_shows_tag_chips(authed_client, make_bundle, db):
    bundle = make_bundle(gamekey="GK1")
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=bundle.gamekey))
    db.commit()

    resp = authed_client.get("/bundles")
    assert "Favorites" in resp.text


def test_list_bundles_shows_never_redeemed_badge_for_confirmed_unredeemed_key(authed_client, make_bundle, db):
    from app.models.bundle_entitlement import BundleEntitlement

    bundle = make_bundle(gamekey="GK1")
    db.add(BundleEntitlement(gamekey=bundle.gamekey, machine_name="m", keyindex=0, key_name="Some Game", steam_app_id="220", steam_owned=False))
    db.commit()

    resp = authed_client.get("/bundles")
    assert "Never redeemed" in resp.text


def test_list_bundles_omits_never_redeemed_badge_when_owned_or_unchecked(authed_client, make_bundle, db):
    from app.models.bundle_entitlement import BundleEntitlement

    owned = make_bundle(gamekey="OWNED", order=make_order(name="Owned Bundle"))
    db.add(BundleEntitlement(gamekey=owned.gamekey, machine_name="m", keyindex=0, key_name="Owned Game", steam_app_id="220", steam_owned=True))
    unchecked = make_bundle(gamekey="UNCHECKED", order=make_order(name="Unchecked Bundle"))
    db.add(BundleEntitlement(gamekey=unchecked.gamekey, machine_name="m", keyindex=0, key_name="Unchecked Game", steam_app_id=None, steam_owned=None))
    db.commit()

    resp = authed_client.get("/bundles")
    assert "Never redeemed" not in resp.text


def test_list_bundles_never_redeemed_badge_checks_gog_too(authed_client, make_bundle, db):
    from app.models.bundle_entitlement import BundleEntitlement

    bundle = make_bundle(gamekey="GK1")
    db.add(BundleEntitlement(gamekey=bundle.gamekey, machine_name="m", keyindex=0, key_name="Some GOG Game", gog_owned=False))
    db.commit()

    resp = authed_client.get("/bundles")
    assert "Never redeemed" in resp.text


def _seed_unredeemed_and_clean_bundles(db, make_bundle):
    from app.models.bundle_entitlement import BundleEntitlement

    unredeemed = make_bundle(gamekey="UNREDEEMED", order=make_order(name="Unredeemed Bundle"))
    db.add(BundleEntitlement(gamekey=unredeemed.gamekey, machine_name="m", keyindex=0, key_name="Some Game", steam_app_id="220", steam_owned=False))
    clean = make_bundle(gamekey="CLEAN", order=make_order(name="Clean Bundle"))
    db.add(BundleEntitlement(gamekey=clean.gamekey, machine_name="m", keyindex=0, key_name="Owned Game", steam_app_id="221", steam_owned=True))
    db.commit()
    return unredeemed, clean


def test_list_bundles_redeemed_filter_unredeemed_shows_only_flagged(authed_client, make_bundle, db):
    _seed_unredeemed_and_clean_bundles(db, make_bundle)

    resp = authed_client.get("/bundles?redeemed=unredeemed")
    assert "Unredeemed Bundle" in resp.text
    assert "Clean Bundle" not in resp.text


def test_list_bundles_redeemed_filter_redeemed_excludes_flagged(authed_client, make_bundle, db):
    _seed_unredeemed_and_clean_bundles(db, make_bundle)

    resp = authed_client.get("/bundles?redeemed=redeemed")
    assert "Clean Bundle" in resp.text
    assert "Unredeemed Bundle" not in resp.text


def test_list_bundles_redeemed_filter_blank_shows_both(authed_client, make_bundle, db):
    _seed_unredeemed_and_clean_bundles(db, make_bundle)

    resp = authed_client.get("/bundles")
    assert "Unredeemed Bundle" in resp.text
    assert "Clean Bundle" in resp.text


def test_list_bundles_sort_by_price_desc(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(name="Cheap", amount_spent=1.0))
    make_bundle(gamekey="GK2", order=make_order(name="Pricey", amount_spent=99.0))
    resp = authed_client.get("/bundles?sort=price&dir=desc")
    assert resp.text.index("Pricey") < resp.text.index("Cheap")


def test_list_bundles_htmx_request_returns_partial_only(authed_client, make_bundle):
    make_bundle(gamekey="GK1")
    resp = authed_client.get("/bundles", headers={"HX-Request": "true"})
    assert "<html" not in resp.text.lower()


def test_bundle_detail_404_for_unknown_gamekey(authed_client):
    resp = authed_client.get("/bundles/NOPE")
    assert resp.status_code == 404


def test_bundle_detail_shows_its_tags(authed_client, make_bundle, db):
    bundle = make_bundle(gamekey="GK1")
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=bundle.gamekey))
    db.commit()

    resp = authed_client.get(f"/bundles/{bundle.gamekey}")
    assert "Favorites" in resp.text


def test_bundle_detail_tag_forms_are_not_nested_inside_a_p_tag(authed_client, make_bundle, db):
    bundle = make_bundle(gamekey="GK1")
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=bundle.gamekey))
    db.commit()

    resp = authed_client.get(f"/bundles/{bundle.gamekey}")
    assert not _has_form_nested_inside_p(resp.text)


def test_bundle_detail_tag_form_posts_to_its_own_gamekey(authed_client, make_bundle):
    bundle = make_bundle(gamekey="GK1")
    resp = authed_client.get(f"/bundles/{bundle.gamekey}")
    assert f'hx-post="/bundles/{bundle.gamekey}/tags"' in resp.text
    assert f'id="bundle-tags-{bundle.gamekey}"' in resp.text


def test_add_bundle_tag_creates_tag_and_attaches_it(authed_client, make_bundle, db):
    bundle = make_bundle(gamekey="GK1")
    resp = authed_client.post(f"/bundles/{bundle.gamekey}/tags", data={"name": "New Tag"})
    assert resp.status_code == 200
    assert "New Tag" in resp.text
    tag = db.query(Tag).filter(Tag.name == "New Tag").one()
    assert db.query(BundleTag).filter(BundleTag.gamekey == bundle.gamekey, BundleTag.tag_id == tag.id).one_or_none() is not None


def test_add_bundle_tag_reuses_existing_tag_by_name(authed_client, make_bundle, db):
    bundle = make_bundle(gamekey="GK1")
    db.add(Tag(name="Existing"))
    db.commit()

    authed_client.post(f"/bundles/{bundle.gamekey}/tags", data={"name": "Existing"})
    assert db.query(Tag).filter(Tag.name == "Existing").count() == 1


def test_add_bundle_tag_twice_does_not_duplicate_the_association(authed_client, make_bundle, db):
    bundle = make_bundle(gamekey="GK1")
    authed_client.post(f"/bundles/{bundle.gamekey}/tags", data={"name": "Dup"})
    authed_client.post(f"/bundles/{bundle.gamekey}/tags", data={"name": "Dup"})
    tag = db.query(Tag).filter(Tag.name == "Dup").one()
    assert db.query(BundleTag).filter(BundleTag.gamekey == bundle.gamekey, BundleTag.tag_id == tag.id).count() == 1


def test_add_bundle_tag_404_for_unknown_gamekey(authed_client):
    resp = authed_client.post("/bundles/NOPE/tags", data={"name": "Whatever"})
    assert resp.status_code == 404


def test_remove_bundle_tag(authed_client, make_bundle, db):
    bundle = make_bundle(gamekey="GK1")
    tag = Tag(name="Removable")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=bundle.gamekey))
    db.commit()

    resp = authed_client.post(f"/bundles/{bundle.gamekey}/tags/{tag.id}/remove")
    assert resp.status_code == 200
    assert "Removable" not in resp.text
    assert db.query(BundleTag).filter(BundleTag.gamekey == bundle.gamekey, BundleTag.tag_id == tag.id).one_or_none() is None
    # The tag itself survives — removing it from one bundle isn't the same as deleting it.
    assert db.query(Tag).filter(Tag.id == tag.id).one_or_none() is not None


def test_bundle_detail_shows_item_and_price_summary(authed_client, make_bundle):
    make_bundle(
        gamekey="GK1",
        order=make_order(
            name="Detail Bundle",
            amount_spent=20.0,
            subproducts=[make_subproduct("Only Item")],
        ),
    )
    resp = authed_client.get("/bundles/GK1")
    assert resp.status_code == 200
    assert "Detail Bundle" in resp.text
    assert "Only Item" in resp.text
    assert "$20.00" in resp.text


def test_bundle_detail_shows_entitlements_separately_from_downloads(authed_client, make_bundle, db):
    # BundleEntitlement rows are populated by sync/refresh.py from a separate write
    # path (never derived from raw_json at render time, unlike downloads) — so the
    # fixture bundle alone isn't enough, a real entitlement row is also needed.
    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Real File")]))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="steamgame", keyindex=0, key_name="Steam Key Game"))
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "Real File" in resp.text
    assert "Steam Key Game" in resp.text


def test_bundle_detail_tables_share_a_leading_column_width_for_alignment(authed_client, make_bundle, db):
    # The "Downloadable items" and "Third-party keys" tables are two separate
    # <table> elements with a different column count/meaning, so nothing
    # forces their columns to line up with each other automatically — a
    # shared fixed-width leading column (checkbox in one, a blank spacer in
    # the other) is what makes Item/Key actually start at the same x
    # position down the page instead of each table auto-sizing independently.
    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Real File")]))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="steamgame", keyindex=0, key_name="Steam Key Game"))
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert resp.text.count('class="table-lead-col"') >= 2


def test_bundle_detail_shows_steam_platform_and_ownership(authed_client, make_bundle, db):
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Owned Game - Steam",
            steam_app_id="220", steam_owned=True, raw_json=json.dumps({"key_type": "steam"}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "Owned Game - Steam" in resp.text
    assert "steam" in resp.text.lower()


def test_bundle_detail_shows_never_redeemed_badge(authed_client, make_bundle, db):
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed Game - Steam",
            steam_app_id="999", steam_owned=False, raw_json=json.dumps({"key_type": "steam"}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "Never redeemed" in resp.text
    assert 'href="https://www.humblebundle.com/downloads?key=GK1"' in resp.text
    assert 'target="_blank"' in resp.text


def test_bundle_detail_key_name_links_to_humble_order_page_regardless_of_ownership(authed_client, make_bundle, db):
    """Every key row links to Humble's own order page for this bundle, not just
    never-redeemed ones — a general "go manage this key on Humble" convenience,
    separate from the ownership-specific "Never redeemed" badge/link.
    """
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Owned Game - Steam",
            steam_app_id="220", steam_owned=True, raw_json=json.dumps({"key_type": "steam"}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert 'href="https://www.humblebundle.com/downloads?key=GK1"' in resp.text
    assert ">Owned Game - Steam</a>" in resp.text


def test_bundle_detail_shows_na_for_non_steam_keys(authed_client, make_bundle, db):
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Origin Game",
            raw_json=json.dumps({"key_type": "origin"}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "n/a" in resp.text
    assert "origin" in resp.text.lower()


def test_bundle_detail_shows_gog_ownership_alongside_steam(authed_client, make_bundle, db):
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Owned Game - GOG",
            gog_id="1207660413", gog_owned=True, raw_json=json.dumps({"key_type": "gog"}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "Owned Game - GOG" in resp.text
    assert "GOG: Yes" in resp.text


def test_bundle_detail_shows_unknown_for_unmatched_steam_keys(authed_client, make_bundle, db):
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Not Yet Checked - Steam",
            steam_app_id=None, steam_owned=None, raw_json=json.dumps({"key_type": "steam"}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "unknown" in resp.text.lower()


def test_bundle_detail_shows_external_redeem_link_once_revealed(authed_client, make_bundle, db):
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Learn to Code Course",
            redeemed_on_humble=True,
            raw_json=json.dumps({
                "key_type": "external_key",
                "custom_html": '<a href="https://training.example.com/courses/1?coupon=ABC">Click here to claim</a>',
            }),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert 'href="https://training.example.com/courses/1?coupon=ABC"' in resp.text


def test_bundle_detail_omits_external_redeem_link_before_reveal(authed_client, make_bundle, db):
    import json

    from app.models.bundle_entitlement import BundleEntitlement

    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Learn to Code Course",
            redeemed_on_humble=False,
            raw_json=json.dumps({
                "key_type": "external_key",
                "custom_html": '<a href="https://training.example.com/courses/1?coupon=ABC">Click here to claim</a>',
            }),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "training.example.com" not in resp.text


def test_bundle_detail_shows_expiry_countdown_in_yellow_for_external_key(authed_client, make_bundle, db):
    import json
    from datetime import datetime, timedelta, timezone

    from app.models.bundle_entitlement import BundleEntitlement

    future = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0).isoformat().replace("+00:00", "")
    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Unity Asset",
            raw_json=json.dumps({"key_type": "external_key", "expiration_date": future}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "Expires in 5d" in resp.text or "Expires in 4d" in resp.text
    assert "badge-pending" in resp.text


def test_bundle_detail_shows_expired_badge_for_past_expiration(authed_client, make_bundle, db):
    import json
    from datetime import datetime, timedelta, timezone

    from app.models.bundle_entitlement import BundleEntitlement

    past = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat().replace("+00:00", "")
    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Unity Asset",
            raw_json=json.dumps({"key_type": "external_key", "expiration_date": past}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "Expired" in resp.text


def test_bundle_detail_expiry_badge_not_yellow_for_steam_keys(authed_client, make_bundle, db):
    import json
    from datetime import datetime, timedelta, timezone

    from app.models.bundle_entitlement import BundleEntitlement

    future = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0).isoformat().replace("+00:00", "")
    make_bundle(gamekey="GK1")
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Steam Game",
            steam_app_id="220", steam_owned=True,
            raw_json=json.dumps({"key_type": "steam", "expiration_date": future}),
        )
    )
    db.commit()

    resp = authed_client.get("/bundles/GK1")
    assert "badge-pending" not in resp.text
    assert "badge-not-configured" in resp.text


def test_trigger_download_404_for_unknown_gamekey(authed_client):
    resp = authed_client.post("/bundles/NOPE/download", data={})
    assert resp.status_code == 404


def test_trigger_download_starts_a_job(authed_client, make_bundle):
    make_bundle(gamekey="GK1")
    with patch("app.routers.bundles.worker.start_download", new=AsyncMock(return_value=1)) as mock_start:
        resp = authed_client.post("/bundles/GK1/download", data={"items": ["1"]})
    assert resp.status_code == 200
    mock_start.assert_awaited_once()
    args = mock_start.call_args[0]
    assert args[0] == "GK1"
    assert args[2] == [1]


def test_trigger_download_expands_canonical_format_keys_to_raw_variants(authed_client, make_bundle):
    order = make_order(
        subproducts=[
            make_subproduct("Item", downloads=[{"download_struct": [
                {"name": "Zip", "file_size": 1, "url": {"web": "https://x/a.zip"}},
                {"name": "ZIP", "file_size": 1, "url": {"web": "https://x/b.zip"}},
            ]}])
        ]
    )
    make_bundle(gamekey="GK1", order=order)
    with patch("app.routers.bundles.worker.start_download", new=AsyncMock(return_value=1)) as mock_start:
        resp = authed_client.post("/bundles/GK1/download", data={"formats": ["zip"]})
    assert resp.status_code == 200
    formats_arg = mock_start.call_args[0][3]
    assert sorted(formats_arg) == ["ZIP", "Zip"]


def test_trigger_item_download_with_format_passes_single_element_lists(authed_client, make_bundle):
    make_bundle(gamekey="GK1")
    with patch("app.routers.bundles.worker.start_download", new=AsyncMock(return_value=1)) as mock_start:
        resp = authed_client.post("/bundles/GK1/download/item/1", data={"format": "PDF"})
    assert resp.status_code == 200
    args = mock_start.call_args[0]
    assert args[2] == [1]
    assert args[3] == ["PDF"]


def test_trigger_item_download_without_format_downloads_whole_item(authed_client, make_bundle):
    make_bundle(gamekey="GK1")
    with patch("app.routers.bundles.worker.start_download", new=AsyncMock(return_value=1)) as mock_start:
        resp = authed_client.post("/bundles/GK1/download/item/1", data={})
    assert resp.status_code == 200
    args = mock_start.call_args[0]
    assert args[3] is None


def test_download_status_shows_message_after_completion(authed_client, make_bundle, db):
    from app.models.download_job import STATUS_COMPLETED, DownloadJob

    make_bundle(gamekey="GK1")
    db.add(DownloadJob(gamekey="GK1", status=STATUS_COMPLETED))
    db.commit()

    resp = authed_client.get("/bundles/GK1/download/status")
    assert resp.status_code == 200
    assert "complete" in resp.text.lower()


def test_download_status_shows_queued_when_waiting_for_a_slot(authed_client, make_bundle, db):
    # Multiple downloads can now be "started" at once (queued), not just
    # rejected outright — a bundle whose own job hasn't been picked up by the
    # dispatcher yet should show as queued, not silently blank.
    from app.models.download_job import STATUS_QUEUED, DownloadJob

    make_bundle(gamekey="GK1")
    db.add(DownloadJob(gamekey="GK1", status=STATUS_QUEUED))
    db.commit()

    resp = authed_client.get("/bundles/GK1/download/status")
    assert resp.status_code == 200
    assert "queued" in resp.text.lower()


def test_refresh_status_shows_failure_message(authed_client, db):
    from app.models.sync_run import STATUS_FAILED, SyncRun

    db.add(SyncRun(status=STATUS_FAILED, error_message="Something broke"))
    db.commit()

    resp = authed_client.get("/bundles/refresh/status")
    assert "Something broke" in resp.text
