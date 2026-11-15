from app.db import Base
from app.models.simple_destination import SimpleDestinationMixin


class AudibleDownloadDestination(SimpleDestinationMixin, Base):
    """A named directory downloaded audiobooks (.m4b) can be written into.

    Same deliberately-simple shape as AmazonMusicDestination (see its own
    docstring for why this doesn't reuse Humble's format/tag-routing
    DownloadDestination): one content type, nothing to route by beyond "the
    one the user flagged as default". Scoped to the audiobook download
    (book_downloader.py) only, not the companion-PDF one — pdf_downloader.py
    keeps its existing hardcoded downloads_dir/Audible path unchanged, a
    deliberately narrower first cut rather than changing an already-working,
    unrelated download's behavior at the same time.
    """

    __tablename__ = "audible_download_destination"
