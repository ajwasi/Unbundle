"""Loads the curated, scrubbed JSON fixtures (see scripts/generate_mock_data.py)
once at import time — this process is short-lived, single-purpose, and the
dataset is small, so there's no reason to re-read from disk per request.
"""

import json
from pathlib import Path
from functools import lru_cache

DATA_DIR = Path(__file__).parent / "data"


@lru_cache
def humble_gamekeys() -> list[dict]:
    return json.loads((DATA_DIR / "humble_gamekeys.json").read_text(encoding="utf-8"))


@lru_cache
def humble_orders() -> dict:
    return json.loads((DATA_DIR / "humble_orders.json").read_text(encoding="utf-8"))


@lru_cache
def steam_data() -> dict:
    return json.loads((DATA_DIR / "steam_games.json").read_text(encoding="utf-8"))


@lru_cache
def gog_games() -> list[dict]:
    return json.loads((DATA_DIR / "gog_games.json").read_text(encoding="utf-8"))
