from app.models.tag import ItemTag, Tag
from app.routers.catalog import _avg_item_value, _build_catalog, _rows_from_catalog
from tests.factories import make_order, make_subproduct


def test_build_catalog_keys_by_machine_name(db, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("A Book", machine_name="abook")]))
    catalog = _build_catalog(db)
    assert "abook" in catalog
    assert catalog["abook"]["item_name"] == "A Book"


def test_build_catalog_detects_duplicate_purchase_across_bundles(db, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Same Book", machine_name="samebook")]))
    make_bundle(gamekey="GK2", order=make_order(subproducts=[make_subproduct("Same Book", machine_name="samebook")]))
    catalog = _build_catalog(db)
    assert len(catalog["samebook"]["bundles"]) == 2


def test_build_catalog_includes_item_count_per_bundle(db, make_bundle):
    make_bundle(
        gamekey="GK1",
        order=make_order(subproducts=[make_subproduct("A", machine_name="a"), make_subproduct("B", machine_name="b")]),
    )
    catalog = _build_catalog(db)
    assert catalog["a"]["bundles"][0]["item_count"] == 2


def test_build_catalog_skips_items_with_no_machine_name(db, make_bundle):
    order = make_order(subproducts=[{"human_name": "No Machine Name", "downloads": []}])
    make_bundle(gamekey="GK1", order=order)
    catalog = _build_catalog(db)
    assert catalog == {}


def test_build_catalog_is_cached_across_calls(db, make_bundle):
    from sqlalchemy import event

    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("A Book", machine_name="abook")]))
    _build_catalog(db)  # warm the cache

    statements = []
    engine = db.get_bind()
    listener = lambda conn, cursor, statement, *a: statements.append(statement)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        result = _build_catalog(db)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert result["abook"]["item_name"] == "A Book"
    bundle_selects = [s for s in statements if "raw_json" in s and "FROM bundle" in s]
    assert not bundle_selects, "expected the cache hit to skip re-querying/re-parsing bundle rows"


def test_build_catalog_cache_invalidates_when_a_bundle_is_added(db, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("First Book", machine_name="first")]))
    first = _build_catalog(db)
    assert "second" not in first

    make_bundle(gamekey="GK2", order=make_order(subproducts=[make_subproduct("Second Book", machine_name="second")]))
    second = _build_catalog(db)
    assert "second" in second


def test_build_catalog_cache_does_not_leak_between_tests_a(db, make_bundle):
    # Paired with the "_b" test below — together they prove the autouse
    # _fresh_catalog_cache fixture actually resets the module-level cache,
    # not just that caching works within one test.
    make_bundle(gamekey="ONLY_IN_A", order=make_order(subproducts=[make_subproduct("Only In A", machine_name="onlyina")]))
    assert "onlyina" in _build_catalog(db)


def test_build_catalog_cache_does_not_leak_between_tests_b(db, make_bundle):
    make_bundle(gamekey="ONLY_IN_B", order=make_order(subproducts=[make_subproduct("Only In B", machine_name="onlyinb")]))
    catalog = _build_catalog(db)
    assert "onlyinb" in catalog
    assert "onlyina" not in catalog


def test_avg_item_value_averages_per_bundle_effective_price():
    bundles = [{"amount_spent": 18.0, "item_count": 29}, {"amount_spent": 20.0, "item_count": 29}, {"amount_spent": 18.0, "item_count": 32}]
    result = _avg_item_value(bundles)
    expected = (18.0 / 29 + 20.0 / 29 + 18.0 / 32) / 3
    assert abs(result - expected) < 1e-9


def test_avg_item_value_returns_none_for_empty_list():
    assert _avg_item_value([]) is None


def _seed_many_items(make_bundle, count, prefix="Item"):
    subproducts = [make_subproduct(f"{prefix} {i:03d}", machine_name=f"{prefix.lower()}{i:03d}") for i in range(count)]
    make_bundle(gamekey="GK1", order=make_order(subproducts=subproducts))


def test_catalog_page_shows_only_first_page_with_sentinel(authed_client, make_bundle):
    _seed_many_items(make_bundle, 150)
    resp = authed_client.get("/catalog")
    assert resp.status_code == 200
    assert "Item 000" in resp.text
    assert "Item 099" in resp.text
    assert "Item 100" not in resp.text
    assert "Item 149" not in resp.text
    assert 'id="catalog-sentinel"' in resp.text
    assert "Showing 100 of 150" in resp.text


def test_catalog_page_no_sentinel_when_under_page_size(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Solo Item", machine_name="solo")]))
    resp = authed_client.get("/catalog")
    assert 'id="catalog-sentinel"' not in resp.text
    assert "Showing 1 of 1" in resp.text


def test_catalog_rows_returns_the_next_batch(authed_client, make_bundle):
    _seed_many_items(make_bundle, 150)
    resp = authed_client.get("/catalog/rows", params={"offset": 100})
    assert resp.status_code == 200
    assert "Item 100" in resp.text
    assert "Item 149" in resp.text
    assert "Item 000" not in resp.text
    assert "<thead>" not in resp.text  # a row batch, not a full table re-render


def test_catalog_rows_response_has_no_out_of_band_swap(authed_client, make_bundle):
    # Regression: an hx-swap-oob element here previously broke htmx's swap
    # entirely (confirmed live: htmx:swapError, "e.querySelectorAll is not a
    # function") — htmx parses an outerHTML response targeting a <tr> using
    # special table-context wrapping, and mixing in a non-table OOB element
    # breaks that parsing. The request itself always succeeded; only the
    # swap failed, so no earlier test caught this.
    _seed_many_items(make_bundle, 150)
    resp = authed_client.get("/catalog/rows", params={"offset": 100})
    assert "hx-swap-oob" not in resp.text


def test_catalog_rows_omits_sentinel_on_the_last_batch(authed_client, make_bundle):
    _seed_many_items(make_bundle, 150)
    resp = authed_client.get("/catalog/rows", params={"offset": 100})
    assert 'id="catalog-sentinel"' not in resp.text  # 150 items: batch 2 (rows 100-149) is the last one


def test_catalog_rows_includes_sentinel_when_more_batches_remain(authed_client, make_bundle):
    _seed_many_items(make_bundle, 250)
    resp = authed_client.get("/catalog/rows", params={"offset": 100})
    assert 'id="catalog-sentinel"' in resp.text
    assert "offset=200" in resp.text


def test_catalog_rows_respects_search_filter_across_pages(authed_client, make_bundle):
    subproducts = [make_subproduct(f"Match {i:03d}", machine_name=f"match{i:03d}") for i in range(150)]
    subproducts.append(make_subproduct("Different Thing", machine_name="different"))
    make_bundle(gamekey="GK1", order=make_order(subproducts=subproducts))

    resp = authed_client.get("/catalog/rows", params={"q": "Match", "offset": 100})
    assert "Match 149" in resp.text
    assert "Different Thing" not in resp.text


def test_avg_item_value_ignores_zero_item_count_bundles():
    bundles = [{"amount_spent": 10.0, "item_count": 0}, {"amount_spent": 20.0, "item_count": 2}]
    assert _avg_item_value(bundles) == 10.0


def test_rows_from_catalog_computes_first_purchased_as_earliest():
    items = {
        "x": {
            "item_name": "X",
            "bundles": [
                {"purchased_at": None, "amount_spent": 0, "item_count": 1},
            ],
        }
    }
    rows = _rows_from_catalog(items)
    assert rows[0]["first_purchased"] is None


def test_catalog_page_requires_auth(client):
    resp = client.get("/catalog", follow_redirects=False)
    assert resp.status_code == 303


def test_catalog_page_shows_items(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Unique Item")]))
    resp = authed_client.get("/catalog")
    assert resp.status_code == 200
    assert "Unique Item" in resp.text


def test_catalog_page_dupes_only_filter(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Solo Item", machine_name="solo")]))
    make_bundle(gamekey="GK2", order=make_order(subproducts=[make_subproduct("Dupe Item", machine_name="dupe")]))
    make_bundle(gamekey="GK3", order=make_order(subproducts=[make_subproduct("Dupe Item", machine_name="dupe")]))
    resp = authed_client.get("/catalog?dupes_only=true")
    assert "Dupe Item" in resp.text
    assert "Solo Item" not in resp.text


def test_catalog_page_search(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Findme Book", machine_name="findme")]))
    make_bundle(gamekey="GK2", order=make_order(subproducts=[make_subproduct("Other Book", machine_name="other")]))
    resp = authed_client.get("/catalog?q=Findme")
    assert "Findme Book" in resp.text
    assert "Other Book" not in resp.text


def test_catalog_page_tag_filter(authed_client, make_bundle, db):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Tagged Item", machine_name="tagged")]))
    make_bundle(gamekey="GK2", order=make_order(subproducts=[make_subproduct("Untagged Item", machine_name="untagged")]))
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(ItemTag(tag_id=tag.id, machine_name="tagged"))
    db.commit()

    resp = authed_client.get(f"/catalog?tag_id={tag.id}")
    assert "Tagged Item" in resp.text
    assert "Untagged Item" not in resp.text


def test_catalog_page_search_tolerates_blank_tag_select(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Findable Item", machine_name="findable")]))
    resp = authed_client.get("/catalog?q=Findable&tag_id=")
    assert resp.status_code == 200
    assert "Findable Item" in resp.text


def test_catalog_page_shows_item_tag_chips(authed_client, make_bundle, db):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Item A", machine_name="a")]))
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(ItemTag(tag_id=tag.id, machine_name="a"))
    db.commit()

    resp = authed_client.get("/catalog")
    assert "Favorites" in resp.text


def test_catalog_rows_shows_item_tag_chips(authed_client, make_bundle, db):
    # Regression guard: catalog_rows tags only its own page slice (see
    # _tag_rows), not the full filtered set — confirms that refactor didn't
    # drop tags from the infinite-scroll batch endpoint specifically.
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Item A", machine_name="a")]))
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(ItemTag(tag_id=tag.id, machine_name="a"))
    db.commit()

    resp = authed_client.get("/catalog/rows", params={"offset": 0})
    assert "Favorites" in resp.text


def test_add_item_tag_creates_tag_and_attaches_it(authed_client, make_bundle, db):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Item A", machine_name="a")]))
    resp = authed_client.post("/catalog/item/a/tags", data={"name": "New Tag"})
    assert resp.status_code == 200
    assert "New Tag" in resp.text
    tag = db.query(Tag).filter(Tag.name == "New Tag").one()
    assert db.query(ItemTag).filter(ItemTag.machine_name == "a", ItemTag.tag_id == tag.id).one_or_none() is not None


def test_add_item_tag_twice_does_not_duplicate_the_association(authed_client, db):
    authed_client.post("/catalog/item/a/tags", data={"name": "Dup"})
    authed_client.post("/catalog/item/a/tags", data={"name": "Dup"})
    tag = db.query(Tag).filter(Tag.name == "Dup").one()
    assert db.query(ItemTag).filter(ItemTag.machine_name == "a", ItemTag.tag_id == tag.id).count() == 1


def test_remove_item_tag(authed_client, db):
    tag = Tag(name="Removable")
    db.add(tag)
    db.commit()
    db.add(ItemTag(tag_id=tag.id, machine_name="a"))
    db.commit()

    resp = authed_client.post(f"/catalog/item/a/tags/{tag.id}/remove")
    assert resp.status_code == 200
    assert "Removable" not in resp.text
    assert db.query(ItemTag).filter(ItemTag.machine_name == "a", ItemTag.tag_id == tag.id).one_or_none() is None
    assert db.query(Tag).filter(Tag.id == tag.id).one_or_none() is not None


def test_item_bundles_modal_shows_per_bundle_and_average(authed_client, make_bundle):
    make_bundle(
        gamekey="GK1",
        order=make_order(
            amount_spent=18.0,
            subproducts=[make_subproduct("Repeated Item", machine_name="repeated") for _ in range(1)],
        ),
    )
    make_bundle(
        gamekey="GK2",
        order=make_order(
            amount_spent=20.0,
            subproducts=[make_subproduct("Repeated Item", machine_name="repeated")],
        ),
    )
    resp = authed_client.get("/catalog/item/repeated/bundles")
    assert resp.status_code == 200
    assert "Repeated Item" in resp.text
    assert "Average price per item across these 2 bundles" in resp.text


def test_item_bundles_modal_no_average_line_for_single_bundle(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Solo", machine_name="solo")]))
    resp = authed_client.get("/catalog/item/solo/bundles")
    assert "Average price per item" not in resp.text


def test_item_bundles_modal_handles_unknown_machine_name(authed_client):
    resp = authed_client.get("/catalog/item/does-not-exist/bundles")
    assert resp.status_code == 200
    assert "No bundles found" in resp.text
