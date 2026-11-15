from fastapi import APIRouter

from app.connectors import plex_connector
from app.models.credential import SOURCE_PLEX
from app.sync import plex_sync

from .media_server import Field, MediaServerSpec, register_media_server

router = APIRouter()

plex_context = register_media_server(
    router,
    MediaServerSpec(
        name="plex",
        source=SOURCE_PLEX,
        connector=plex_connector,
        check_args=("base_url", "token"),
        fields=(
            Field("base_url", required=True),
            Field("token", secret=True, has_key="token", required=True),
            Field("library_id", keep_when_blank=False),
        ),
        required_message="Server URL and token are both required.",
        refresh=lambda db: plex_sync.refresh_plex_library(db),
    ),
)
