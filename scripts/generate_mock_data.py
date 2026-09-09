"""One-time (re-runnable) extraction of a curated, privacy-scrubbed subset of the
real dev database into mock_api/data/*.json, feeding both the mock API server
(see app/config.py's DEMO_MODE) and pytest's mock-server integration tests.

Whitelists only the fields app/connectors/humble_connector.py:parse_bundle() (and
app/routers/bundles.py's redeem-link/expiry helpers) actually read — never a raw
copy of a real order/key JSON blob — and replaces every redeemed_key_val with a
synthesized fake in the same format. The selection deliberately targets a cross
section of this account's real data (every bundle category, redeemed/unredeemed/
Steam-owned/GOG-matched entitlement states, a key-only bundle, a multi-format
download item, real tag usage) rather than a random sample — see the plan.

Run manually (`python scripts/generate_mock_data.py`) against the real
./data/humble.db; the generated JSON is what's reviewed and committed, not this
script's live DB access. Safe to re-run — it only reads the database.
"""

import json
import random
import sqlite3
import string
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "humble.db"
OUT_DIR = Path(__file__).parent.parent / "mock_api" / "data"

# Never the real account's SteamID64 — a fake, well-formed 17-digit placeholder.
FAKE_STEAMID64 = "76500000000000001"

# Hand-picked via querying the real DB for specific real-world examples of each
# flag/state the app's features branch on (see the plan's Context section) —
# not a random sample. Comments note *why* each one earned its place.
SELECTED_GAMEKEYS = [
    "rEUw6fUwyWG3DfZH",  # bundle: small, clean multi-item game bundle
    "pHsSaHAsyKY7AHPD",  # storefront
    "yhqnTxdTmSUqShW7",  # subscriptioncontent
    "HuRHC7hnHwhK5n2Y",  # subscriptionplan
    "ezNfACM4F7xzCnwE",  # widget
    "8avFvGDh6eAF7uRp",  # key-only bundle; genuinely-unredeemed AND Steam-owned-unredeemed keys
    "4EETGuDqM32UKe24",  # key-only bundle; more genuinely-unredeemed variety
    "myTnxYs3M4WfEAZY",  # GOG-type keys whose titles exact-match real gog_game rows
    "BU7d57mDHhec",      # a fully-redeemed-on-Humble key, for contrast
    "D3BcVq577qZbNKMv",  # multi-format download item (EPUB+MOBI+PDF on "MOOCs")
    "sXYUP5Fz6DcEmcPx",  # a good target bundle to tag "Graphic Novels" (see NOTE below)
    "u2fcAm24WXwbnckF",  # a good target bundle to tag "AudioBook"
    "NCByfMveNqqGEVwc",  # a good target bundle to tag "Computer Books"
    "syuyPpHGKnzFCXue",  # manga bundle — a good target for an item-level tag
]

# NOTE on tags: Tag/BundleTag/ItemTag are a purely local app feature (never
# fetched from Humble/Steam/GOG), so they have no place in this HTTP-mock
# dataset at all — there's nothing to "mock" here. Tests that need a tagged
# bundle/item apply the tag directly via the DB after a mock sync (a few lines
# using the same Tag/BundleTag/ItemTag models tests already use elsewhere);
# the bundles above are chosen so a real, on-theme tag name is available when
# a test wants one, matching this account's real tag vocabulary
# ("Graphic Novels", "AudioBook", "Computer Books", "Manga", "Comic Books",
# "3d Printing"). A live demo starts with no tags applied, same as any new
# install — this app's own Tags page is how you'd create them regardless.


def fake_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    group = lambda: "".join(random.choice(alphabet) for _ in range(5))
    return f"{group()}-{group()}-{group()}"


def whitelist_key_entry(raw: dict) -> dict:
    entry = {
        "human_name": raw.get("human_name", "Unknown key"),
        "machine_name": raw.get("machine_name", ""),
        "keyindex": raw.get("keyindex", 0),
        "key_type": raw.get("key_type", ""),
    }
    if raw.get("steam_app_id"):
        entry["steam_app_id"] = raw["steam_app_id"]
    if raw.get("gog_id"):
        entry["gog_id"] = raw["gog_id"]
    if isinstance(raw.get("redeemed_key_val"), str):
        entry["redeemed_key_val"] = fake_key()
    # custom_html/custom_instructions_html sometimes embed a real, personally-
    # redeemable secret directly in a URL query param (confirmed: a real Unity
    # course coupon code was found this way, not caught by the redeemed_key_val
    # scrub above since it lives in a totally different field). Rather than try
    # to pattern-match every way a secret could hide in arbitrary real HTML,
    # replace the whole blob with a synthesized placeholder that still has a
    # real <a href> for _extract_redeem_link() (routers/bundles.py) to find —
    # that's the only thing this field is used for, so a fake link tests it
    # exactly as well as the real one would.
    for field in ("custom_html", "custom_instructions_html"):
        if raw.get(field):
            entry[field] = '<a href="https://example.com/redeem-demo-key">Click here to redeem</a>'
    for field in ("expiration_date", "expiry_date"):
        if raw.get(field):
            entry[field] = raw[field]
    return entry


def fake_download_url(real_url: str) -> str:
    """Real Humble CDN URLs are signed (st=/exp=/hmac= query params tied to a
    real account's real request — confirmed by inspection, not assumed) —
    even one whose exp= timestamp looks past isn't safe to publish verbatim;
    same reasoning as the coupon-code fix above: don't try to judge whether a
    specific secret is still "live", replace it. Keeps only the real
    filename, which is all parse_bundle() ever extracts from this field
    (url.rsplit("/", 1)[-1].split("?", 1)[0]) and all a demo actually needs —
    nothing in DEMO_MODE ever fetches this URL for real.
    """
    filename = real_url.rsplit("/", 1)[-1].split("?", 1)[0]
    return f"https://cdn.example.com/demo/{filename}"


def whitelist_order(order: dict) -> dict:
    product = order.get("product") or {}
    out = {
        "product": {"human_name": product.get("human_name", ""), "category": product.get("category", "")},
        "created": order.get("created"),
        "amount_spent": order.get("amount_spent", 0),
        "subproducts": [],
        "tpkd_dict": {"all_tpks": []},
    }
    for sp in order.get("subproducts") or []:
        downloads_out = []
        for dl in sp.get("downloads") or []:
            variants_out = [
                {
                    "name": v.get("name", ""),
                    "file_size": v.get("file_size", 0),
                    "url": {"web": fake_download_url((v.get("url") or {}).get("web", ""))},
                }
                for v in dl.get("download_struct") or []
                if (v.get("url") or {}).get("web")
            ]
            if variants_out:
                downloads_out.append({"download_struct": variants_out})
        out["subproducts"].append(
            {
                "human_name": sp.get("human_name", "Unknown item"),
                "machine_name": sp.get("machine_name", ""),
                "downloads": downloads_out,
            }
        )
    for key_entry in (order.get("tpkd_dict") or {}).get("all_tpks") or []:
        out["tpkd_dict"]["all_tpks"].append(whitelist_key_entry(key_entry))
    return out


def main() -> None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    orders: dict[str, dict] = {}
    steam_appids_seen: set[str] = set()
    gog_titles_seen: dict[str, int] = {}  # title -> a made-up product_id

    for gamekey in SELECTED_GAMEKEYS:
        row = conn.execute("SELECT raw_json FROM bundle WHERE gamekey = ?", (gamekey,)).fetchone()
        if row is None:
            raise SystemExit(f"gamekey {gamekey} not found in {DB_PATH} — was it renamed/removed since selection?")
        raw_order = json.loads(row["raw_json"])
        whitelisted = whitelist_order(raw_order)

        for key_entry in whitelisted["tpkd_dict"]["all_tpks"]:
            if key_entry.get("steam_app_id"):
                steam_appids_seen.add(str(key_entry["steam_app_id"]))
            if key_entry.get("key_type") == "gog":
                # Real product_id values aren't in bundle_entitlement's raw_json at
                # all (Humble never sends one for GOG keys — see gog_sync.py's
                # docstring) — the mock GOG game list just needs *a* stable id,
                # matched to the real DB's title for the name-matching path.
                gog_titles_seen.setdefault(key_entry["human_name"], 1000000 + len(gog_titles_seen))

        orders[gamekey] = whitelisted

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "humble_gamekeys.json").write_text(
        json.dumps([{"gamekey": g} for g in SELECTED_GAMEKEYS], indent=2), encoding="utf-8"
    )
    (OUT_DIR / "humble_orders.json").write_text(json.dumps(orders, indent=2), encoding="utf-8")

    # Steam: deliberately own only SOME of the appids seen across the selected
    # bundles (not all of them) so both cross-check outcomes survive in the mock
    # data — owning all of them would silently erase the "genuinely never
    # redeemed, not Steam-matched" case the key-only bundles above were chosen
    # for. Real appids/names are Steam's own public product info, not scrubbed.
    STEAM_OWNED_APPIDS = {
        "202530",  # Sonic the Hedgehog 4 - Episode I (from 8avFvGDh6eAF7uRp)
        "203650",  # Sonic the Hedgehog 4 - Episode II (from 8avFvGDh6eAF7uRp)
        "61510",   # Age of Wonders II: The Wizard's Throne (from 4EETGuDqM32UKe24)
    }
    assert STEAM_OWNED_APPIDS <= steam_appids_seen, "an owned appid isn't actually in the selected bundles"
    assert steam_appids_seen - STEAM_OWNED_APPIDS, "every appid seen would be owned — no unredeemed case left"
    appid_names = {
        r["appid"]: r["name"]
        for r in conn.execute(
            "SELECT DISTINCT steam_app_id as appid, key_name as name FROM bundle_entitlement WHERE steam_app_id IS NOT NULL"
        )
    }
    steam_games = [
        {"appid": int(appid), "name": appid_names.get(appid, f"App {appid}"), "playtime_forever": 0, "img_icon_url": ""}
        for appid in sorted(STEAM_OWNED_APPIDS)
    ]
    (OUT_DIR / "steam_games.json").write_text(
        json.dumps({"steamid64": FAKE_STEAMID64, "games": steam_games}, indent=2), encoding="utf-8"
    )

    gog_games = [{"id": product_id, "title": title, "image": ""} for title, product_id in gog_titles_seen.items()]
    (OUT_DIR / "gog_games.json").write_text(json.dumps(gog_games, indent=2), encoding="utf-8")

    conn.close()
    print(f"Wrote {len(orders)} bundles, {len(steam_games)} Steam games, {len(gog_games)} GOG games to {OUT_DIR}")


if __name__ == "__main__":
    main()
