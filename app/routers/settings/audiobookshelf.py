from fastapi import APIRouter

from app.connectors import audiobookshelf_connector
from app.models.credential import SOURCE_AUDIOBOOKSHELF
from app.sync import audiobookshelf_sync

from .media_server import Field, MediaServerSpec, register_media_server

router = APIRouter()

audiobookshelf_context = register_media_server(
    router,
    MediaServerSpec(
        name="audiobookshelf",
        source=SOURCE_AUDIOBOOKSHELF,
        connector=audiobookshelf_connector,
        check_args=("base_url", "api_token"),
        fields=(
            Field("base_url", required=True),
            Field("api_token", secret=True, has_key="token", required=True),
            Field("library_id", keep_when_blank=False),
        ),
        required_message="Server URL and API token are both required.",
        refresh=lambda db: audiobookshelf_sync.refresh_audiobookshelf_library(db),
    ),
)
