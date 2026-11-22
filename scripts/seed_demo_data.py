"""Populates a database with purely synthetic, fictional demo data across
every feature area, for screenshots and manual exploration without any real
account, credential, or personal library data involved anywhere.

Every title, author, username, and account identifier below is invented for
this script and does not describe anyone's real purchases or library.

Unlike mock_api/ (which fakes the Humble/Steam/GOG *HTTP APIs* so the app's
real connectors/sync logic can be exercised end to end, see
scripts/generate_mock_data.py), this script inserts rows directly into the
database — faster to maintain across nine feature areas, at the cost of not
exercising connector code. Use mock_api/DEMO_MODE for testing the Humble/
Steam/GOG sync flow itself; use this for a fully-populated app to look at,
screenshot, or click around in.

Usage (against a throwaway/dev database only — this is destructive-additive,
not idempotent, and will create duplicate rows if run twice against the same
DB):

    DATABASE_URL=sqlite:///./demo.db python -m scripts.seed_demo_data

Safe to point DATABASE_URL at any fresh SQLite file; never run this against
a database holding real connected-account data.
"""

import json
from datetime import datetime, timedelta

from app.db import SessionLocal
from app.models.amazon_music_track import AmazonMusicTrack
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import AudibleBookDownload
from app.models.audible_wishlist import AudibleWishlistItem
from app.models.audiobookshelf_item import AudiobookShelfItem
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.credential import SOURCE_AMAZON_MUSIC, SOURCE_AUDIBLE, SOURCE_AUDIOBOOKSHELF
from app.models.credential import SOURCE_CHIRP, SOURCE_GOG, SOURCE_HUMBLE, SOURCE_NAVIDROME
from app.models.credential import SOURCE_PLEX, SOURCE_STEAM, STATUS_OK, Credential
from app.models.download import LOCATION_LOCAL, Download
from app.models.download import STATUS_COMPLETED as DL_COMPLETED
from app.models.download import STATUS_FAILED as DL_FAILED
from app.models.download import STATUS_QUEUED as DL_QUEUED
from app.models.download import STATUS_RUNNING as DL_RUNNING
from app.models.download_job import DownloadJob
from app.models.gog_game import GogGame
from app.models.navidrome_album import NavidromeAlbum
from app.models.plex_audiobook_item import PlexAudiobookItem
from app.models.steam_game import SteamGame
from app.models.store_wishlist import ChirpWishlistItem, GogWishlistItem, SteamWishlistItem
from app.models.tag import BundleTag, ItemTag, Tag
from app.security import encrypt_json

NOW = datetime.utcnow()


def days_ago(n):
    return NOW - timedelta(days=n)


def seed(db):
    # ---------------------------------------------------------- Credentials
    # status="ok" with a plausible-looking payload so Settings cards render
    # as "Connected" with populated fields, rather than blank/not-configured.
    creds = [
        (SOURCE_HUMBLE, {"session_cookie": "demo-session-cookie-not-real"}),
        (SOURCE_STEAM, {"api_key": "DEMOAPIKEY00000000000000000000", "steamid64": "76561190000000001"}),
        (SOURCE_GOG, {"refresh_token": "demo-refresh-token"}),
        (SOURCE_AUDIBLE, {"access_token": "demo-access-token", "locale_code": "us"}),
        (SOURCE_AMAZON_MUSIC, {}),
        (SOURCE_CHIRP, {"cookie": "demo-cookie"}),
        (SOURCE_AUDIOBOOKSHELF, {"base_url": "https://audiobookshelf.demo.local", "api_token": "demo-token", "library_id": ""}),
        (SOURCE_PLEX, {"base_url": "http://plex.demo.local:32400", "token": "demo-token", "library_id": "3"}),
        (SOURCE_NAVIDROME, {"base_url": "https://navidrome.demo.local", "username": "demo", "password": "demo-password"}),
    ]
    for source, payload in creds:
        db.add(Credential(source=source, status=STATUS_OK, encrypted_payload=encrypt_json(payload), updated_at=days_ago(1)))

    # ----------------------------------------------------------------- Tags
    tag_favorite = Tag(name="Favorite", created_at=days_ago(60))
    tag_comic = Tag(name="Comic", created_at=days_ago(55))
    db.add(tag_favorite)
    db.add(tag_comic)
    db.flush()

    # -------------------------------------------------------------- Bundles
    def subproduct(human_name, machine_name, filename, size):
        return {
            "human_name": human_name,
            "machine_name": machine_name,
            "downloads": [{"download_struct": [{"url": {"web": f"https://dl.example.invalid/{filename}"}, "name": filename, "file_size": size}]}],
        }

    bundle_indie = Bundle(
        gamekey="demogk001", name="Indie Game Collection Vol. 3", category="games", machine_name="indie_games_vol3",
        subproduct_count=2, downloadable_item_count=0, key_count=2, purchased_at=days_ago(90), amount_spent=12.0,
        raw_json="{}", fetched_at=days_ago(1),
    )
    bundle_books = Bundle(
        gamekey="demogk002", name="Learn Python & Data Craft", category="books", machine_name="learn_python_data_craft",
        subproduct_count=2, downloadable_item_count=2, key_count=0, purchased_at=days_ago(70), amount_spent=18.0,
        raw_json=json.dumps({"subproducts": [
            subproduct("Python for Tinkerers", "python_for_tinkerers", "python_for_tinkerers.pdf", 4_200_000),
            subproduct("Data Craft Handbook", "data_craft_handbook", "data_craft_handbook.epub", 2_100_000),
        ]}),
        fetched_at=days_ago(1),
    )
    bundle_retro = Bundle(
        gamekey="demogk003", name="Retro Shooter Megapack", category="games", machine_name="retro_shooter_megapack",
        subproduct_count=3, downloadable_item_count=0, key_count=3, purchased_at=days_ago(40), amount_spent=15.0,
        raw_json="{}", fetched_at=days_ago(1),
    )
    bundle_comics = Bundle(
        gamekey="demogk004", name="Comic Universe Bundle", category="books", machine_name="comic_universe_bundle",
        subproduct_count=1, downloadable_item_count=1, key_count=0, purchased_at=days_ago(20), amount_spent=10.0,
        raw_json=json.dumps({"subproducts": [subproduct("Nebula Rangers Omnibus", "nebula_rangers_omnibus", "nebula_rangers_omnibus.pdf", 15_800_000)]}),
        fetched_at=days_ago(1),
    )
    for b in (bundle_indie, bundle_books, bundle_retro, bundle_comics):
        db.add(b)
    db.flush()

    db.add(BundleTag(tag_id=tag_favorite.id, gamekey=bundle_indie.gamekey))
    db.add(ItemTag(tag_id=tag_comic.id, machine_name="nebula_rangers_omnibus"))

    entitlements = [
        BundleEntitlement(gamekey="demogk001", machine_name="lantern_keepers_trial", keyindex=0, key_name="Lantern Keeper's Trial - Steam",
                           redeemed_on_humble=True, steam_app_id="1000001", steam_owned=True, raw_json='{"key_type": "steam"}'),
        BundleEntitlement(gamekey="demogk001", machine_name="gearwright_skies", keyindex=0, key_name="Gearwright Skies - Steam",
                           redeemed_on_humble=False, steam_app_id="1000002", steam_owned=False, raw_json='{"key_type": "steam"}'),
        BundleEntitlement(gamekey="demogk003", machine_name="chrono_vendetta", keyindex=0, key_name="Chrono Vendetta - Steam",
                           redeemed_on_humble=False, steam_app_id="1000003", steam_owned=None, raw_json='{"key_type": "steam"}'),
        BundleEntitlement(gamekey="demogk003", machine_name="iron_tide_assault", keyindex=0, key_name="Iron Tide Assault - GOG",
                           redeemed_on_humble=True, gog_id="2000001", gog_owned=True, raw_json='{"key_type": "gog"}'),
        BundleEntitlement(gamekey="demogk003", machine_name="starfall_requiem", keyindex=0, key_name="Starfall Requiem - GOG",
                           redeemed_on_humble=False, gog_id="2000002", gog_owned=False, raw_json='{"key_type": "gog"}'),
    ]
    db.add_all(entitlements)

    db.add(SteamGame(appid=1000001, name="Lantern Keeper's Trial", playtime_forever_minutes=640, img_icon_url="", fetched_at=days_ago(1)))
    db.add(SteamGame(appid=1000003, name="Chrono Vendetta: Redux", playtime_forever_minutes=0, img_icon_url="", fetched_at=days_ago(1)))
    db.add(SteamGame(appid=1000099, name="Driftwood Carnival", playtime_forever_minutes=120, img_icon_url="", fetched_at=days_ago(1)))

    db.add(GogGame(product_id=2000001, title="Iron Tide Assault", content_type="game", purchased_at=None, paid_price=None, fetched_at=days_ago(1)))
    db.add(GogGame(product_id=2000099, title="Moss & Mortar", content_type="game", purchased_at=days_ago(200), paid_price=9.99, fetched_at=days_ago(1)))

    # ---------------------------------------------------- Downloads (Humble)
    db.add(Download(
        gamekey="demogk002", bundle_name=bundle_books.name, item_name="Python for Tinkerers", subproduct_index=0,
        file_format="pdf", original_filename="python_for_tinkerers.pdf", source_url="https://dl.example.invalid/python_for_tinkerers.pdf",
        expected_size_bytes=4_200_000, status=DL_COMPLETED, progress_bytes=4_200_000, current_location_type=LOCATION_LOCAL,
        current_location_path="/data/downloads/Learn Python & Data Craft/Python for Tinkerers/python_for_tinkerers.pdf",
        queued_at=days_ago(2), started_at=days_ago(2), completed_at=days_ago(2),
    ))
    db.add(Download(
        gamekey="demogk002", bundle_name=bundle_books.name, item_name="Data Craft Handbook", subproduct_index=1,
        file_format="epub", original_filename="data_craft_handbook.epub", source_url="https://dl.example.invalid/data_craft_handbook.epub",
        expected_size_bytes=2_100_000, status=DL_FAILED, error_message="Connection reset while downloading.",
        current_location_type=LOCATION_LOCAL, queued_at=days_ago(1), started_at=days_ago(1),
    ))
    db.add(Download(
        gamekey="demogk004", bundle_name=bundle_comics.name, item_name="Nebula Rangers Omnibus", subproduct_index=0,
        file_format="pdf", original_filename="nebula_rangers_omnibus.pdf", source_url="https://dl.example.invalid/nebula_rangers_omnibus.pdf",
        expected_size_bytes=15_800_000, status=DL_RUNNING, progress_bytes=6_000_000, current_location_type=LOCATION_LOCAL,
        queued_at=days_ago(0), started_at=days_ago(0),
    ))
    db.add(DownloadJob(gamekey="demogk004", bundle_name=bundle_comics.name, requested_indices="", requested_formats="pdf",
                        status=DL_RUNNING, queued_at=days_ago(0), started_at=days_ago(0)))
    db.add(DownloadJob(gamekey="demogk002", bundle_name=bundle_books.name, requested_indices="", requested_formats="",
                        status="completed", queued_at=days_ago(2), started_at=days_ago(2), finished_at=days_ago(2)))

    # ------------------------------------------------------------- Audible
    books = [
        ("DEMOASIN01", "The Clockwork Orchard", "Mira Halloway", "Of Grove and Gear", "1", "LIBRARY", 612, 4.6, True, 100.0),
        ("DEMOASIN02", "Letters to a Distant Harbor", "Theo Brandt", "", "", "LIBRARY", 480, 4.2, False, 35.0),
        ("DEMOASIN03", "Beneath a Tin Sky", "Mira Halloway", "Of Grove and Gear", "2", "LIBRARY", 598, 4.7, False, 0.0),
        ("DEMOASIN04", "The Quiet Algorithm", "Priya Okonkwo", "", "", "AYCL", 540, 4.0, False, 0.0),
        ("DEMOASIN05", "Saltwater Ledger", "Owen Castellan", "", "", "LIBRARY", 715, 4.4, False, 62.0),
        ("DEMOASIN06", "Hollow Market Blues", "Priya Okonkwo", "", "", "AYCL", 390, 3.9, True, 100.0),
        ("DEMOASIN07", "A Cartographer's Apology", "Nadia Furst", "", "", "LIBRARY", 660, 4.8, False, 10.0),
    ]
    for asin, title, author, series, seq, benefit, runtime, rating, finished, pct in books:
        db.add(AudibleBook(
            asin=asin, title=title, author=author, narrator="Jordan Vane", runtime_minutes=runtime,
            cover_url="", fetched_at=days_ago(1), purchase_date=days_ago(30), price_amount=19.99, price_currency="USD",
            series_title=series, series_sequence=seq, series_asin="DEMOSERIES01" if series else "",
            rating_average=rating, description="A demo description for a fictional audiobook.", is_finished=finished,
            percent_complete=pct, pdf_url="", benefit_id=benefit,
        ))
    db.add(AudibleBookDownload(asin="DEMOASIN01", status="completed", progress_bytes=58_000_000, expected_size_bytes=58_000_000,
                                downloaded_path="/data/downloads/Audible/Mira Halloway/The Clockwork Orchard/The Clockwork Orchard.m4b",
                                queued_at=days_ago(5), started_at=days_ago(5), completed_at=days_ago(5)))
    db.add(AudibleBookDownload(asin="DEMOASIN04", status="failed",
                                error_message="Audible denied the license for this title (Membership: Customer is not part of any plans).",
                                queued_at=days_ago(1), started_at=days_ago(1), completed_at=days_ago(1)))

    db.add(AudiobookShelfItem(library_item_id="demoabs001", asin="DEMOASIN01", title="The Clockwork Orchard", author="Mira Halloway", fetched_at=days_ago(1)))
    db.add(AudiobookShelfItem(library_item_id="demoabs002", asin="DEMOASIN03", title="Beneath a Tin Sky", author="Mira Halloway", fetched_at=days_ago(1)))
    db.add(AudiobookShelfItem(library_item_id="demoabs003", asin="DEMOASIN05", title="Saltwater Ledger", author="Owen Castellan", fetched_at=days_ago(1)))

    db.add(PlexAudiobookItem(rating_key="demoplex001", title="Letters to a Distant Harbor", author="Theo Brandt", fetched_at=days_ago(1)))
    db.add(PlexAudiobookItem(rating_key="demoplex002", title="A Cartographer's Apology", author="Nadia Furst", fetched_at=days_ago(1)))

    db.add(AudibleWishlistItem(asin="DEMOWISH01", title="The Last Lighthouse Keeper", authors="Owen Castellan", narrators="Jordan Vane",
                                runtime_minutes=520, current_price=14.99, list_price=24.99, currency="USD", rating=4.5, rating_count=312,
                                added_at=days_ago(15), first_seen_at=days_ago(15), last_seen_at=days_ago(1)))
    db.add(AudibleWishlistItem(asin="DEMOWISH02", title="Iron and Ivy", authors="Nadia Furst", narrators="Jordan Vane",
                                runtime_minutes=480, current_price=19.99, list_price=19.99, currency="USD", rating=4.1, rating_count=88,
                                added_at=days_ago(8), first_seen_at=days_ago(8), last_seen_at=days_ago(1)))

    # --------------------------------------------------------- Amazon Music
    tracks = [
        ("demotrk001", "DEMOTA01", "Harbor Lights", "The Quiet Engines", "DEMOAL01", "Ambient Departures", 1, "3:42", 222, False),
        ("demotrk002", "DEMOTA01", "Static Bloom", "The Quiet Engines", "DEMOAL01", "Ambient Departures", 2, "4:10", 250, False),
        ("demotrk003", "DEMOTA01", "Low Tide Radio", "The Quiet Engines", "DEMOAL01", "Ambient Departures", 3, "3:58", 238, False),
        ("demotrk004", "DEMOTA02", "Copper Wire Waltz", "Felsen & the Drift", "DEMOAL02", "Copper Wire Waltz", 1, "3:15", 195, False),
        ("demotrk005", "DEMOTA02", "Tin Roof Serenade", "Felsen & the Drift", "DEMOAL02", "Copper Wire Waltz", 2, "4:02", 242, False),
        ("demotrk006", "DEMOTA03", "Midnight Exit", "Various Artists", "DEMOAL03", "Night Shift Vol. 2", 1, "3:30", 210, True),
        ("demotrk007", "DEMOTA03", "Neon Residue", "Various Artists", "DEMOAL03", "Night Shift Vol. 2", 2, "3:48", 228, True),
    ]
    for download_id, track_asin, title, artist, album_asin, album, track_no, dur_display, dur_sec, is_comp in tracks:
        db.add(AmazonMusicTrack(
            download_id=download_id, track_asin=track_asin, title=title, artist=artist, artist_asin="",
            album=album, album_asin=album_asin, track_number=track_no, duration_display=dur_display,
            duration_seconds=dur_sec, cover_url="", first_seen_at=days_ago(20), last_seen_at=days_ago(1), is_compilation=is_comp,
        ))

    db.add(NavidromeAlbum(album_id="demond001", name="Ambient Departures", artist="The Quiet Engines", fetched_at=days_ago(1)))
    db.add(NavidromeAlbum(album_id="demond002", name="Night Shift Vol. 2", artist="Various Artists", fetched_at=days_ago(1)))

    # -------------------------------------------------------------- Chirp
    chirp_books = [
        ("demochirp001", "The Hollow Compass", "Reema Doyle", "Alex Marchetti", "finished", 100, True, "", ""),
        ("demochirp002", "Granite and Smoke", "Petra Lindqvist", "Alex Marchetti", "in_progress", 42, True, "", ""),
        ("demochirp003", "Whistlestop Requiem", "Caz Iwerebor", "Sam Okafor", "not_started", 0, True, "Border Towns", "3"),
        ("demochirp004", "The Cartology Wars", "Reema Doyle", "Alex Marchetti", "not_started", 0, True, "", ""),
    ]
    for pid, title, authors, narrators, status, pct, playable, series, seq in chirp_books:
        db.add(ChirpAudiobook(
            purchase_id=pid, audiobook_id=pid.replace("purchase", "book"), title=title, authors=authors, narrators=narrators,
            url_path=f"/library/{pid}", cover_url="", progress_status=status, position_percent=pct, playable=playable,
            series_name=series, series_number=seq, listing_price=24.99, discount_price=9.99,
            purchased_at=days_ago(25).date(), paid_price=9.99, fetched_at=days_ago(1),
        ))

    # -------------------------------------------------------------- Wishlists
    db.add(SteamWishlistItem(appid=1000100, name="Orchard of Rust", header_image="", developers="Pale Lantern Games",
                              priority=1, added_at=days_ago(10), current_price=14.99, list_price=19.99, currency="USD",
                              discount_pct=25, metacritic=81, first_seen_at=days_ago(10), last_seen_at=days_ago(1)))
    db.add(SteamWishlistItem(appid=1000101, name="Fathom & Ferrous", header_image="", developers="Driftwood Collective",
                              priority=2, added_at=days_ago(4), current_price=24.99, list_price=24.99, currency="USD",
                              discount_pct=0, metacritic=None, first_seen_at=days_ago(4), last_seen_at=days_ago(1)))
    db.add(GogWishlistItem(product_id=2000100, title="Verdigris Station", cover_url="", current_price=11.99, list_price=19.99,
                            currency="USD", first_seen_at=days_ago(12), last_seen_at=days_ago(1)))
    db.add(ChirpWishlistItem(url_path="/library/demo-wish-chirp-1", title="The Unmapped Coastline", authors="Petra Lindqvist",
                              cover_url="", current_price=12.99, list_price=19.99, currency="USD",
                              first_seen_at=days_ago(6), last_seen_at=days_ago(1)))

    db.commit()


def main():
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
    print("Demo data seeded.")


if __name__ == "__main__":
    main()
