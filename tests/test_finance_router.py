from app.models.tag import BundleTag, Tag
from tests.factories import make_order


def test_finance_requires_auth(client):
    resp = client.get("/finance", follow_redirects=False)
    assert resp.status_code == 303


def test_finance_page_shows_total_spent(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(amount_spent=10.0, created="2024-01-01T00:00:00"))
    make_bundle(gamekey="GK2", order=make_order(amount_spent=5.5, created="2024-02-01T00:00:00"))
    resp = authed_client.get("/finance")
    assert resp.status_code == 200
    assert "$15.50" in resp.text


def test_finance_year_filter(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(amount_spent=10.0, created="2023-06-01T00:00:00"))
    make_bundle(gamekey="GK2", order=make_order(amount_spent=20.0, created="2024-06-01T00:00:00"))
    resp = authed_client.get("/finance?year=2024")
    assert "$20.00" in resp.text
    assert "$10.00" not in resp.text


def test_finance_tag_filter(authed_client, make_bundle, db):
    tagged = make_bundle(gamekey="GK1", order=make_order(amount_spent=10.0, created="2024-01-01T00:00:00"))
    make_bundle(gamekey="GK2", order=make_order(amount_spent=5.5, created="2024-02-01T00:00:00"))
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=tagged.gamekey))
    db.commit()

    resp = authed_client.get(f"/finance?tag_id={tag.id}")
    assert "$10.00" in resp.text
    assert "$5.50" not in resp.text


def test_finance_tolerates_blank_tag_select(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(amount_spent=10.0, created="2024-01-01T00:00:00"))
    resp = authed_client.get("/finance?year=&month=&category=&tag_id=")
    assert resp.status_code == 200
    assert "$10.00" in resp.text


def test_finance_month_filter_matches_across_all_years(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(amount_spent=10.0, created="2022-12-01T00:00:00"))
    make_bundle(gamekey="GK2", order=make_order(amount_spent=20.0, created="2023-12-01T00:00:00"))
    make_bundle(gamekey="GK3", order=make_order(amount_spent=30.0, created="2023-06-01T00:00:00"))
    resp = authed_client.get("/finance?month=12")
    assert "$30.00" in resp.text  # combined December total across both years (10 + 20)


def test_finance_category_filter(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(amount_spent=10.0, category="bundle"))
    make_bundle(gamekey="GK2", order=make_order(amount_spent=20.0, category="subscriptioncontent"))
    resp = authed_client.get("/finance?category=bundle")
    assert "$10.00" in resp.text
    assert "$20.00" not in resp.text


def test_finance_granularity_is_year_without_year_filter(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(created="2023-01-01T00:00:00"))
    make_bundle(gamekey="GK2", order=make_order(created="2024-01-01T00:00:00"))
    resp = authed_client.get("/finance")
    assert resp.status_code == 200
    assert '"2023"' in resp.text or "2023" in resp.text
    assert '"2024"' in resp.text or "2024" in resp.text


def test_finance_granularity_is_month_with_year_filter(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(created="2024-03-01T00:00:00"))
    resp = authed_client.get("/finance?year=2024")
    assert "2024-03" in resp.text


def test_finance_htmx_request_returns_partial(authed_client, make_bundle):
    make_bundle(gamekey="GK1")
    resp = authed_client.get("/finance", headers={"HX-Request": "true"})
    assert "<html" not in resp.text.lower()


def test_finance_all_years_and_categories_lists_populate(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(created="2021-01-01T00:00:00", category="widget"))
    resp = authed_client.get("/finance")
    assert "2021" in resp.text
    assert "Widget" in resp.text
