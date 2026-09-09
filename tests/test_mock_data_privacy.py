"""Guards the committed mock_api/data/*.json fixtures against ever regaining
real personal data if scripts/generate_mock_data.py is re-run and re-committed
without this same scrutiny — this data is extracted from a real account and
published in a public repo, so these checks are load-bearing, not incidental.

Each check here corresponds to a real thing found by manual audit while
building this dataset (see scripts/generate_mock_data.py's comments) — this
file exists so the next regeneration doesn't have to rediscover them by hand.
"""

import json
import re
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "mock_api" / "data"

_SAFE_REDEEM_LINK = '<a href="https://example.com/redeem-demo-key">Click here to redeem</a>'
_FAKE_KEY_RE = re.compile(r"^[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$")
_FAKE_STEAMID64 = "76500000000000001"


def _raw_text() -> str:
    return "\n".join((DATA_DIR / name).read_text(encoding="utf-8") for name in DATA_DIR.glob("*.json"))


def _humble_orders() -> dict:
    return json.loads((DATA_DIR / "humble_orders.json").read_text(encoding="utf-8"))


def test_no_email_addresses_anywhere():
    assert not re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", _raw_text())


def test_no_signed_or_real_cdn_urls_only_the_synthetic_placeholder_domain():
    # Real Humble CDN download URLs are signed (st=/exp=/hmac= query params
    # tied to the real account that requested them) — confirmed by inspection
    # against this account's real data, not a hypothetical risk. Every URL in
    # the committed dataset must point at the synthetic placeholder domain
    # instead (see scripts/generate_mock_data.py:fake_download_url), never a
    # real humble/steam/gog host.
    urls = re.findall(r"https?://[a-zA-Z0-9.-]+", _raw_text())
    assert urls, "expected at least one URL in the dataset"
    assert set(urls) <= {"https://cdn.example.com", "https://example.com"}


def test_no_17_digit_sequences_except_the_one_known_fake_steamid():
    # A real SteamID64 is 17 digits — this catches both the actual real
    # account's SteamID and any other stray 17-digit token (the original bug:
    # real signed-URL HMAC fragments coincidentally containing 17-digit runs).
    found = set(re.findall(r"\b\d{17}\b", _raw_text()))
    assert found == {_FAKE_STEAMID64}


def test_every_redeemed_key_value_matches_the_synthesized_fake_format():
    for order in _humble_orders().values():
        for key_entry in order["tpkd_dict"]["all_tpks"]:
            val = key_entry.get("redeemed_key_val")
            if val is not None:
                assert _FAKE_KEY_RE.match(val), f"unexpected key format: {val!r}"


def test_every_custom_html_field_is_the_one_known_safe_placeholder():
    # custom_html/custom_instructions_html sometimes embed a real redeemable
    # secret directly in a URL query param (confirmed: a real course coupon
    # code was found this way) — the whole field gets replaced, not scrubbed
    # in place, so there should never be more than this one literal value.
    for order in _humble_orders().values():
        for key_entry in order["tpkd_dict"]["all_tpks"]:
            for field in ("custom_html", "custom_instructions_html"):
                val = key_entry.get(field)
                if val is not None:
                    assert val == _SAFE_REDEEM_LINK, f"unexpected {field}: {val!r}"


def test_steam_games_have_no_playtime_or_icon_data():
    steam_data = json.loads((DATA_DIR / "steam_games.json").read_text(encoding="utf-8"))
    assert steam_data["steamid64"] == _FAKE_STEAMID64
    for game in steam_data["games"]:
        assert game["playtime_forever"] == 0
        assert game["img_icon_url"] == ""


# Every one of these is a real field confirmed present in the source account's
# raw order/key JSON (see scripts/generate_mock_data.py's whitelist functions)
# that is NOT in the whitelist and must never appear in the committed output —
# `uid` (a real per-order account identifier), `payee`/`icon`/subproduct `url`
# (harmless on their own, but not vetted for inclusion), `md5`/`sha1` (file
# hashes — not personal, but likewise never vetted), `instructions_html`/
# `preinstruction_text`/`third_party_product_id`/`is_giftee`/`options_dict`/
# `download_identifier` and the rest. whitelist_order()/whitelist_key_entry()
# build brand-new dicts field-by-field with no raw copy/spread of any kind, so
# none of these can appear today — this test guards against a future edit
# accidentally introducing one (e.g. a careless `**raw` or `dict(raw)`).
_FIELDS_THAT_MUST_NEVER_APPEAR = (
    "uid", "is_giftee", "claimed", "currency", "missed_credit", "path_ids",
    "choices_remaining", "total_choices", "payee", "icon", "library_family_name",
    "custom_download_page_box_html", "custom_download_page_box_css", "display_item",
    "download_identifier", "options_dict", "platform", "desktop_app_only",
    "download_version_number", "md5", "sha1", "build_version", "builds",
    "human_size", "uploaded_at", "uses_kindle_sender", "instructions_html",
    "preinstruction_text", "third_party_product_id", "sold_out", "direct_redeem",
    "disallowed_countries", "disclaimer", "exclusive_countries", "is_gift",
    "auto_expand", "empty_tpkds", "partial_gift_enabled", "post_purchase_text",
    "subscription_credits",
)


def test_known_unwhitelisted_fields_never_appear_as_a_json_key():
    for order in _humble_orders().values():
        assert not (set(order.keys()) & set(_FIELDS_THAT_MUST_NEVER_APPEAR))
        assert not (set(order["product"].keys()) & set(_FIELDS_THAT_MUST_NEVER_APPEAR))
        for sp in order["subproducts"]:
            assert not (set(sp.keys()) & set(_FIELDS_THAT_MUST_NEVER_APPEAR))
            for dl in sp["downloads"]:
                assert not (set(dl.keys()) & set(_FIELDS_THAT_MUST_NEVER_APPEAR))
                for v in dl["download_struct"]:
                    assert not (set(v.keys()) & set(_FIELDS_THAT_MUST_NEVER_APPEAR))
        for key_entry in order["tpkd_dict"]["all_tpks"]:
            assert not (set(key_entry.keys()) & set(_FIELDS_THAT_MUST_NEVER_APPEAR))
