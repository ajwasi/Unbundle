"""Hand-built order/bundle JSON matching Humble's real (undocumented) API shape,
based on the exact field names app/connectors/humble_connector.py:parse_bundle()
reads. No live-captured fixture exists in this repo, so these deliberately cover
every field parse_bundle touches (product.human_name/category, created,
amount_spent, subproducts[].human_name/downloads[].download_struct[]..., and
tpkd_dict.all_tpks[] for third-party keys) so a fixture drifting out of sync with
the real parser would show up as a test failure, not a silent gap.
"""


def make_download_variant(name="EPUB", filename="book.epub", size=1024, url=None, gamekey="x"):
    url = url or f"https://dl.humble.com/{filename}?gamekey={gamekey}"
    return {"name": name, "file_size": size, "url": {"web": url}}


def make_subproduct(human_name="Test Book", machine_name=None, downloads=None):
    if downloads is None:
        downloads = [{"download_struct": [make_download_variant()]}]
    return {
        "human_name": human_name,
        "machine_name": machine_name or human_name.lower().replace(" ", ""),
        "downloads": downloads,
    }


def make_tpk(human_name="Test Game - Steam", machine_name="testgame", keyindex=0, redeemed=False, steam_app_id=None):
    entry = {"human_name": human_name, "machine_name": machine_name, "keyindex": keyindex}
    if redeemed:
        entry["redeemed_key_val"] = "FAKE-STEAM-KEY-1234"
    if steam_app_id:
        entry["steam_app_id"] = steam_app_id
    return entry


def make_order(
    name="Test Bundle",
    category="bundle",
    created="2024-01-15T00:00:00",
    amount_spent=10.0,
    subproducts=None,
    tpks=None,
):
    return {
        "product": {"human_name": name, "category": category},
        "created": created,
        "amount_spent": amount_spent,
        "subproducts": subproducts if subproducts is not None else [make_subproduct()],
        "tpkd_dict": {"all_tpks": tpks if tpks is not None else []},
    }
