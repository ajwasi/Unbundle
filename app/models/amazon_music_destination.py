from app.db import Base
from app.models.simple_destination import SimpleDestinationMixin


class AmazonMusicDestination(SimpleDestinationMixin, Base):
    """A named directory purchased tracks can be downloaded into.

    Deliberately simpler than DownloadDestination (Humble's format/tag routing
    rules): a track has one file format (mp3, confirmed against a real signed
    delivery URL) and no tag system of its own, so there is nothing to route
    *by* — this is just a list of places the user has told the app it may
    write to, with one flagged as the default. `is_compilation` already
    decides the "Various Artists" vs. per-artist folder *within* whichever
    destination is used (see downloader.py); it does not pick the destination
    itself.
    """

    __tablename__ = "amazon_music_destination"
