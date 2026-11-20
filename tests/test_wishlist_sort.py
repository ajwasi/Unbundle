"""Wishlist column headers sort the table: ascending, then descending, then
cleared back to the store's own order."""

import re
from datetime import datetime

from app.models.store_wishlist import SteamWishlistItem, SteamWishlistPrice


def _seed(db):
    now = datetime.utcnow()
    rows = [  # appid doubles as the store's own (priority) order: Mid, Zed, Alpha, Unpriced
        (1, "Mid", 20.0, 70, 1),
        (2, "Zed", 5.0, 90, 2),
        (3, "Alpha", 50.0, 50, 3),
        (4, "Unpriced", None, None, 4),
    ]
    for appid, name, price, meta, priority in rows:
        db.add(SteamWishlistItem(appid=appid, name=name, current_price=price, metacritic=meta,
                                 priority=priority, first_seen_at=now, last_seen_at=now))
    db.add(SteamWishlistPrice(appid=1, price=2.0, currency="USD", captured_at=now))
    db.add(SteamWishlistPrice(appid=2, price=4.0, currency="USD", captured_at=now))
    db.commit()


def _order(resp):
    return re.findall(r"<strong>(Mid|Zed|Alpha|Unpriced)</strong>", resp.text)


def test_default_order_is_the_stores_own(authed_client, db):
    _seed(db)
    assert _order(authed_client.get("/wishlist/steam")) == ["Mid", "Zed", "Alpha", "Unpriced"]


def test_ascending_and_descending_by_title(authed_client, db):
    _seed(db)
    assert _order(authed_client.get("/wishlist/steam?sort=title&dir=asc")) == ["Alpha", "Mid", "Unpriced", "Zed"]
    assert _order(authed_client.get("/wishlist/steam?sort=title&dir=desc")) == ["Zed", "Unpriced", "Mid", "Alpha"]


def test_missing_values_sort_last_in_both_directions(authed_client, db):
    _seed(db)
    assert _order(authed_client.get("/wishlist/steam?sort=price&dir=asc")) == ["Zed", "Mid", "Alpha", "Unpriced"]
    assert _order(authed_client.get("/wishlist/steam?sort=price&dir=desc")) == ["Alpha", "Mid", "Zed", "Unpriced"]
    assert _order(authed_client.get("/wishlist/steam?sort=rating&dir=desc"))[-1] == "Unpriced"


def test_lowest_seen_sorts_both_ways_with_unknown_last(authed_client, db):
    _seed(db)
    assert _order(authed_client.get("/wishlist/steam?sort=lowest&dir=asc"))[:2] == ["Mid", "Zed"]
    assert _order(authed_client.get("/wishlist/steam?sort=lowest&dir=desc"))[:2] == ["Zed", "Mid"]
    assert _order(authed_client.get("/wishlist/steam?sort=lowest&dir=desc"))[-1] in {"Alpha", "Unpriced"}


def test_header_click_cycles_asc_desc_clear(authed_client, db):
    _seed(db)
    def title_link(url):
        html = authed_client.get(url).text
        return re.search(r'<th[^>]*>\s*<a [^>]*hx-get="([^"]*)"[^>]*>\s*Title', html).group(1)

    assert title_link("/wishlist/steam").endswith("&sort=title&dir=asc")
    assert title_link("/wishlist/steam?sort=title&dir=asc").endswith("&sort=title&dir=desc")
    cleared = title_link("/wishlist/steam?sort=title&dir=desc")
    assert "sort=" not in cleared


def test_sorted_header_is_announced_and_marked(authed_client, db):
    _seed(db)
    html = authed_client.get("/wishlist/steam?sort=title&dir=desc").text
    assert 'aria-sort="descending"' in html
    assert "&#9660;" in html or "▼" in html
    assert "aria-sort" not in authed_client.get("/wishlist/steam").text


def test_sort_survives_search_and_sale_filter(authed_client, db):
    _seed(db)
    html = authed_client.get("/wishlist/steam?sort=price&dir=desc&q=a&deals_only=true").text
    assert 'id="wl-sort" name="sort" value="price"' in html
    assert 'id="wl-dir" name="dir" value="desc"' in html
    assert "q=a&amp;deals_only=true" in html or "q=a&deals_only=true" in html


def test_unknown_sort_falls_back_to_default_order(authed_client, db):
    _seed(db)
    assert _order(authed_client.get("/wishlist/steam?sort=bogus&dir=desc")) == ["Mid", "Zed", "Alpha", "Unpriced"]
    assert _order(authed_client.get("/wishlist/steam?sort=added")) == ["Mid", "Zed", "Alpha", "Unpriced"]


def test_gog_has_no_author_or_rating_columns_to_sort(authed_client):
    html = authed_client.get("/wishlist/gog").text
    assert "sort=author" not in html and "sort=rating" not in html
    assert "sort=title" in html and "sort=price" in html and "sort=lowest" in html


def _seed_discounts(db):
    now = datetime.utcnow()
    for appid, name, pct in [(1, "Mid", 25), (2, "Zed", 75), (3, "Alpha", 0), (4, "Unpriced", 50)]:
        db.add(SteamWishlistItem(appid=appid, name=name, discount_pct=pct, current_price=10.0, list_price=20.0, currency="USD",
                                 priority=appid, first_seen_at=now, last_seen_at=now))
    db.commit()


def test_discount_has_its_own_sortable_column(authed_client, db):
    _seed_discounts(db)
    html = authed_client.get("/wishlist/steam").text
    assert "sort=discount" in html
    assert 'badge badge-ok">-75%' in html
    # the percentage no longer rides inside the Price cell
    price_cell = re.search(r"10\.00 USD(.*?)</td>", html, re.S).group(1)
    assert "%" not in price_cell


def test_discount_sorts_both_ways_with_no_discount_last(authed_client, db):
    _seed_discounts(db)
    assert _order(authed_client.get("/wishlist/steam?sort=discount&dir=desc")) == ["Zed", "Unpriced", "Mid", "Alpha"]
    assert _order(authed_client.get("/wishlist/steam?sort=discount&dir=asc")) == ["Mid", "Unpriced", "Zed", "Alpha"]


def test_discount_sorts_on_stores_where_it_is_computed(authed_client, db):
    from app.models.store_wishlist import GogWishlistItem
    now = datetime.utcnow()
    for pid, title, cur in [("1", "Small", 18.0), ("2", "Big", 5.0), ("3", "Full price", 20.0)]:
        db.add(GogWishlistItem(product_id=pid, title=title, current_price=cur, list_price=20.0, first_seen_at=now, last_seen_at=now))
    db.commit()
    html = authed_client.get("/wishlist/gog?sort=discount&dir=desc").text
    assert html.index("Big") < html.index("Small") < html.index("Full price")
