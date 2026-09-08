"""Import every model module here so Base.metadata is fully populated for
Alembic's autogenerate and for Base.metadata.create_all() in tests.
"""

from app.models.backup_settings import BackupSettings
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import Credential
from app.models.download import Download
from app.models.download_job import DownloadJob
from app.models.gog_game import GogGame
from app.models.steam_game import SteamGame
from app.models.sync_run import SyncRun

__all__ = [
    "BackupSettings", "Bundle", "BundleEntitlement", "Credential", "Download", "DownloadJob",
    "GogGame", "SteamGame", "SyncRun",
]
