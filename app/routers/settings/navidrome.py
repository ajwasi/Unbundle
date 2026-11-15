from fastapi import APIRouter

from app.connectors import navidrome_connector
from app.models.credential import SOURCE_NAVIDROME
from app.sync import navidrome_sync

from .media_server import Field, MediaServerSpec, register_media_server

router = APIRouter()

navidrome_context = register_media_server(
    router,
    MediaServerSpec(
        name="navidrome",
        source=SOURCE_NAVIDROME,
        connector=navidrome_connector,
        check_args=("base_url", "username", "password"),
        fields=(
            Field("base_url", required=True),
            Field("username", required=True),
            # Passwords are stored exactly as typed — surrounding spaces can be part of one.
            Field("password", secret=True, has_key="password", required=True, strip=False),
        ),
        required_message="Server URL, username, and password are all required.",
        refresh=lambda db: navidrome_sync.refresh_navidrome_library(db),
    ),
)
