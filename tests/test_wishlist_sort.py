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


# ---- Owned through a different service: the row is highlighted red ----------


def _wl(db, appid, name):
    now = datetime.utcnow()
    db.add(SteamWishlistItem(appid=appid, name=name, priority=appid, first_seen_at=now, last_seen_at=now))


def test_steam_wishlist_item_owned_on_gog_is_flagged(authed_client, db):
    from app.models.gog_game import GogGame

    _wl(db, 1, "Hollow Knight")
    _wl(db, 2, "Something Else")
    db.add(GogGame(product_id=9, title="Hollow  Knight!", content_type="game"))
    db.commit()
    html = authed_client.get("/wishlist/steam").text
    flagged = [r for r in html.split("<tr") if "Hollow Knight" in r][0]
    assert 'class="row-owned-elsewhere"' in flagged
    assert "Owned on GOG" in flagged
    other = [r for r in html.split("<tr") if "Something Else" in r][0]
    assert "row-owned-elsewhere" not in other and "Owned on" not in other


def test_unredeemed_humble_key_counts_as_owned(authed_client, db, make_bundle):
    from app.models.bundle_entitlement import BundleEntitlement

    _wl(db, 1, "Celeste")
    bundle = make_bundle()
    db.add(BundleEntitlement(gamekey=bundle.gamekey, key_name="Celeste", redeemed_on_humble=False))
    db.commit()
    html = authed_client.get("/wishlist/steam").text
    assert "Owned on Humble Bundle" in html and "row-owned-elsewhere" in html


def test_a_stores_own_library_does_not_count_as_elsewhere(authed_client, db):
    from app.models.steam_game import SteamGame

    _wl(db, 1, "Portal")
    db.add(SteamGame(appid=1, name="Portal"))
    db.commit()
    assert "row-owned-elsewhere" not in authed_client.get("/wishlist/steam").text


def test_audiobook_owned_on_another_service_is_flagged_and_movies_are_not_games(authed_client, db):
    from app.models.audible_wishlist import AudibleWishlistItem
    from app.models.chirp_audiobook import ChirpAudiobook

    now = datetime.utcnow()
    db.add(AudibleWishlistItem(asin="B1", title="Project Hail Mary", first_seen_at=now, last_seen_at=now))
    db.add(ChirpAudiobook(purchase_id="p1", title="Project Hail Mary"))
    db.commit()
    html = authed_client.get("/wishlist/audible").text
    assert "Owned on Chirp" in html and "row-owned-elsewhere" in html

    from app.models.gog_game import GogGame
    _wl(db, 7, "Blade Runner")
    db.add(GogGame(product_id=70, title="Blade Runner", content_type="movie"))
    db.commit()
    assert "row-owned-elsewhere" not in authed_client.get("/wishlist/steam").text
