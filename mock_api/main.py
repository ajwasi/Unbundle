"""Standalone fake backend for Humble/Steam/GOG, serving the curated dataset in
mock_api/data/ (see scripts/generate_mock_data.py) in the same shapes the real
APIs return. No auth, no validation — every route accepts any credentials and
always succeeds; the point is realistic *data*, not testing credential checks
(the existing unittest.mock-based connector tests already cover those).

Run directly (`python -m mock_api.main`) or under uvicorn
(`uvicorn mock_api.main:app`) — both work, see docker-compose.yml's `demo`
profile and tests/mock_server.py.
"""

from fastapi import FastAPI, Request

from mock_api.data_loader import gog_games, humble_gamekeys, humble_orders, steam_data

app = FastAPI(title="Humble Tracker Mock API")


@app.get("/")
def root():
    return {"status": "ok", "service": "humble-tracker-mock-api"}


# --- Humble --------------------------------------------------------------


@app.get("/humble/api/v1/user/order")
def humble_user_order():
    return humble_gamekeys()


@app.get("/humble/api/v1/orders")
def humble_orders_batch(request: Request):
    requested = request.query_params.getlist("gamekeys")
    orders = humble_orders()
    return {gk: orders[gk] for gk in requested if gk in orders}


# --- Steam -----------------------------------------------------------------


@app.get("/steam/ISteamUser/ResolveVanityURL/v0001/")
def steam_resolve_vanity():
    return {"response": {"success": 1, "steamid": steam_data()["steamid64"]}}


@app.get("/steam/ISteamUser/GetPlayerSummaries/v0002/")
def steam_player_summaries(request: Request):
    steamids = request.query_params.get("steamids", steam_data()["steamid64"])
    first_id = steamids.split(",")[0]
    return {
        "response": {
            "players": [
                {"steamid": first_id, "communityvisibilitystate": 3, "personaname": "Demo Player"}
            ]
        }
    }


@app.get("/steam/IPlayerService/GetOwnedGames/v0001/")
def steam_owned_games():
    return {"response": {"games": steam_data()["games"]}}


# --- GOG ---------------------------------------------------------------


@app.get("/gog/token")
def gog_token():
    # Real endpoint distinguishes authorization_code vs refresh_token grants —
    # this mock doesn't need to, since both just need *some* valid-looking
    # token pair back, accepted unconditionally either way.
    return {"access_token": "demo-access-token", "refresh_token": "demo-refresh-token"}


@app.get("/gog/account/getFilteredProducts")
def gog_products():
    products = [{"id": g["id"], "title": g["title"], "isGame": True, "image": g.get("image", "")} for g in gog_games()]
    return {"products": products, "totalPages": 1}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)
